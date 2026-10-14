#!/usr/bin/env python3
"""Offline, resumable parallel routed ALU landscape. Existing runs remain read-only.

Prepare new probe/full workspaces, repeat all nine previously routed configurations
and both raw goldens, then gate full enumeration on exact decision-metric agreement.
Every new evaluation runs the production pipeline including TB/SAT, GDS and DRC/LVS.
"""
import argparse
from concurrent.futures import ProcessPoolExecutor, FIRST_COMPLETED, wait
import fcntl
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import sys
import time

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from s2g import alu, pipeline, workspace
from s2g.common import GateError, digest, files_manifest, identity, read, require, stamp, tree_files, verify_manifest, write

SCHEMA = 's2g.routed-alu-landscape.v1'
BUDGET_KEYS = {'candidate_budget', 'tool_run_budget'}
GOLD_KEY = '__golden__'


def metric(ev, key):
    m = (ev or {}).get('metrics', {}).get(key) or {}
    return m.get('value') if m.get('status') == 'VALID' else None


def collect(ws, cid, run):
    folder, rec, led = pipeline.run_evidence(ws, cid, run)
    ev = read(folder / 'synthesis_evidence.json') if (folder / 'synthesis_evidence.json').is_file() else None
    risk = read(folder / 'risk.json') if (folder / 'risk.json').is_file() else {}
    acc = read(folder / 'acceptance.json')
    jobs = [read(p) for p in folder.rglob('*.job.json')]
    timed_out = any(j.get('timed_out') for j in jobs)
    out = {'candidate': cid, 'run': run, 'workspace': str(Path(ws).resolve()), 'status': led['status'],
           'rtl_sha256': rec['rtl_hash'], 'functional': led.get('functional', {}).get('status'),
           'mapped_proof': led.get('synthesis', {}).get('mapped_proof'),
           'area_um2': metric(ev, 'mapped_area'), 'setup_ws_ns': metric(ev, 'setup_ws'),
           'logic_depth': metric(ev, 'logic_depth'), 'cells': metric(ev, 'cell_count'),
           'fingerprint': (ev or {}).get('structure', {}).get('fingerprint'),
           'mapped_netlist_sha256': led.get('synthesis', {}).get('netlist_sha256'),
           'routed': {'status': led['status'], 'acceptance': acc['status'], 'gates': acc['gates'],
                      'dataset_gates': acc.get('dataset_gates'),
                      'area_um2': risk.get('area_um2'), 'setup_ws_ns': risk.get('setup_ws'),
                      'hold_ws_ns': risk.get('hold_ws'), 'setup_tns_ns': risk.get('setup_tns'),
                      'hold_tns_ns': risk.get('hold_tns'),
                      'electrical': {k: risk.get(k) for k in ('slew_violations', 'cap_violations', 'fanout_violations')},
                      'repair_net_edits': risk.get('repair', {}).get('net_edits'),
                      'repair_events': [{k: e.get(k) for k in ('command', 'inserted', 'removed', 'master_changed', 'return_code')}
                                        for e in risk.get('repair', {}).get('events', [])],
                      'parasitics': risk.get('parasitics', {}).get('provenance'),
                      'timed_out': timed_out,
                      'outcome_known': not timed_out and led['status'] == 'COMPLETE',
                      'error': led.get('error')},
           'elapsed_s': led['elapsed_s'], 'tool_invocations': len(jobs),
           'failed_invocations': sum(j.get('exit_code') != 0 or j.get('timed_out') for j in jobs),
           'evidence_sha256': digest(folder / 'evidence.json')}
    out['decision_signature'] = signature(out)
    return out


def signature(row):
    """Exact scalar decisions/repair operations; wall times and file timestamps excluded."""
    p = row['routed']
    return {'synthesis': {k: row.get(k) for k in ('area_um2', 'setup_ws_ns', 'logic_depth', 'cells', 'fingerprint')},
            'physical': {k: p.get(k) for k in ('status', 'acceptance', 'gates', 'area_um2', 'setup_ws_ns',
                         'hold_ws_ns', 'setup_tns_ns', 'hold_tns_ns', 'electrical', 'repair_net_edits',
                         'repair_events', 'parasitics')}}


def check_physical_profile(old_ws, new_inputs):
    # v1/v2 static syntax permissions differ on the riscv golden qualification;
    # actual RTL, TB, spec, SDC, physical settings and process assets are identical.
    a, b = read(old_ws / 'frozen/context.json'), read(new_inputs / 'context.json')
    ignore = BUDGET_KEYS | {'rtl_policy'}
    require({k: v for k, v in a.items() if k not in ignore} == {k: v for k, v in b.items() if k not in ignore},
            'historical route used a different physical profile')
    for fn in ('reference.v', 'tb.v', 'spec.json', 'constraints.sdc'):
        require(digest(old_ws / 'frozen' / fn) == digest(new_inputs / fn), 'historical route task differs: ' + fn)


def bulk_import_new(ws, entries):
    """One checked bootstrap into an empty workspace, without repeated PDK hashing.

    Candidate and pool records have the workspace API's exact bindings/statuses.
    This imports RTL only; production evaluation still runs every mandatory gate.
    """
    ws = Path(ws)
    spec, ctx, freeze = workspace.verify(ws)
    require(len(entries) <= ctx['candidate_budget'], 'bulk import exceeds candidate budget')
    prepared = []
    for key, entry in entries:
        src = Path(entry['rtl'])
        require(digest(src) == entry['rtl_sha256'], 'import source changed')
        prepared.append((key, entry, src.read_bytes()))
    with workspace.locked(ws):
        state = read(ws / 'state.json')
        require(not state['attempts'], 'bulk import requires an empty workspace')
        records = {}
        sha_seen = {}
        for i, (key, entry, data) in enumerate(prepared):
            cid = f'c{i:04d}'
            folder = ws / 'candidates' / cid; folder.mkdir(parents=True)
            (folder / 'rtl.v').write_bytes(data)
            sha = digest(folder / 'rtl.v')
            rec = {'schema': 's2g.candidate.v1', 'id': cid, 'design_family': spec['design_family'],
                   'spec_hash': freeze['spec_hash'], 'context_hash': freeze['context_hash'],
                   'freeze_hash': digest(ws / 'freeze.json'), 'parent': None,
                   'action': {'op': 'routed_landscape_import', 'config_key': key, 'config': entry['config'],
                              'source': entry['rtl']}, 'status': 'SUBMITTED', 'created_at': stamp(),
                   'rtl_hash': sha, 'source_duplicates': list(sha_seen.get(sha, []))}
            sha_seen.setdefault(sha, []).append(cid)
            write(folder / 'record.json', rec)
            state['attempts'].append(cid)
            records[key] = cid
        write(ws / 'state.json', state)
    workspace.verify(ws)
    return records


def prepare(root):
    root = Path(root).resolve()
    require(root.is_relative_to(BASE.parents[0]), 'run artifacts must stay under S2G')
    require(not root.exists(), 'landscape root must be new')
    root.mkdir(parents=True)
    plans, expectations, sources, design_info = [], {}, [], {}
    for design, example, old_golden in (
            ('riscv32', 'riscv_alu32', 'riscv-alu32-screen-v2'),
            ('kleine32', 'kleine_alu32', 'kleine-alu32-qual-v1')):
        source = BASE / 'runs' / ('alu-landscape-' + design + '-v1')
        old_ws = source / 'workspace'
        workspace.verify(old_ws)
        old_index, proto = read(source / 'index.json'), read(source / 'protocol.json')
        ir, vocab = proto['ir'], proto['vocabulary']
        require(digest(BASE / 'examples' / example / 'alu_ir.json') == proto['ir_sha256'], 'semantic map changed')
        require(digest(BASE / 'configs/alu-actions-v1.json') == proto['vocabulary_sha256'], 'vocabulary changed')
        keys = [GOLD_KEY] + [alu.config_id(c) for c in alu.space(ir, vocab)]
        require(set(keys) == set(old_index), 'source synthesis table is incomplete')
        inp = root / 'inputs' / design; inp.mkdir(parents=True)
        for fn in ('spec.json', 'reference.v', 'tb.v', 'constraints.sdc', 'context.json', 'review.json'):
            shutil.copyfile(old_ws / 'frozen' / fn, inp / fn)
            sources.append(old_ws / 'frozen' / fn)
        entries = {}
        for key in keys:
            e = old_index[key]; cid = e['candidate']
            rec = read(old_ws / 'candidates' / cid / 'record.json')
            path = old_ws / 'candidates' / cid / 'rtl.v'
            require(digest(path) == rec['rtl_hash'], 'source RTL changed: ' + key)
            require(e['status'] == 'COMPLETE' and e['functional'] == e['mapped_proof'] == 'PASS', 'unqualified synthesis source')
            entries[key] = {'rtl': str(path), 'rtl_sha256': rec['rtl_hash'], 'config': e['config']}
            sources.append(path)
        selected = [key for key in keys if old_index[key].get('routed')]
        golden_ws = BASE / 'runs' / old_golden / 'workspace'
        check_physical_profile(golden_ws, inp)
        expected = {GOLD_KEY: collect(golden_ws, 'c0000', 'run-001')}
        for key in selected:
            e = old_index[key]
            expected[key] = collect(old_ws, e['candidate'], e['routed']['run'])
        expectations[design] = expected
        design_info[design] = {'ir': ir, 'vocabulary': vocab, 'origin_config': ir['origin_config'],
                               'source_landscape': str(source), 'source_index_sha256': digest(source / 'index.json')}
        for phase, phase_keys in (('probe', [GOLD_KEY] + selected), ('full', keys)):
            d = root / phase / design
            ctx = read(inp / 'context.json')
            ctx.update(candidate_budget=len(phase_keys), tool_run_budget=len(phase_keys))
            write(d / 'context.json', ctx)
            ws = d / 'workspace'
            workspace.init(ws, inp / 'spec.json', inp / 'reference.v', inp / 'tb.v', inp / 'constraints.sdc',
                           d / 'context.json', inp / 'review.json')
            cids = bulk_import_new(ws, [(k, entries[k]) for k in phase_keys])
            for key in phase_keys:
                plans.append({'id': f'{phase}:{design}:{key}', 'phase': phase, 'design': design, 'key': key,
                              'workspace': str(ws), 'candidate': cids[key], 'config': entries[key]['config'],
                              'rtl_sha256': entries[key]['rtl_sha256']})
            print(f'Prepared {phase}/{design}: {len(phase_keys)} independent full-flow jobs', flush=True)
        sources += [source / 'index.json', source / 'protocol.json']
        for row in expected.values():
            run = Path(row['workspace']) / 'candidates' / row['candidate'] / row['run']
            sources += [run / n for n in ('evidence.json', 'ledger.json', 'risk.json', 'acceptance.json')]
    write(root / 'expectations.json', expectations)
    write(root / 'jobs.json', plans)
    code = tree_files(BASE / 's2g') + [Path(__file__)]
    code_snapshot = root / 'code_snapshot'; code_snapshot.mkdir()
    shutil.copytree(BASE / 's2g', code_snapshot / 's2g', ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copyfile(__file__, code_snapshot / Path(__file__).name)
    man = {'schema': SCHEMA, 'created_at': stamp(), 'model_calls': 0, 'designs': design_info,
           'source_files': files_manifest(sources), 'runtime_code': files_manifest(code),
           'prepared_inputs': files_manifest(tree_files(root / 'inputs') + [root / 'jobs.json', root / 'expectations.json']
                                            + [root / p / d / 'context.json' for p in ('probe', 'full') for d in design_info]),
           'jobs': len(plans), 'workers_cap': 30, 'cores_per_worker': 4, 'min_free_gib': 40,
           'determinism': 'exact synthesis and routed decision metrics, acceptance gates and repair operations; '
                          'wall time and timestamped file bytes excluded',
           'scope': 'current frozen contract only; seed 17; one corner; no paid model/stressed-contract experiment'}
    man['manifest_sha256'] = identity(man)
    write(root / 'manifest.json', man)
    write(root / 'progress.json', {'status': 'PREPARED', 'completed': 0, 'total': len(plans), 'at': stamp()})
    return man


def verify(root):
    root = Path(root)
    man = read(root / 'manifest.json')
    require(identity({k: v for k, v in man.items() if k != 'manifest_sha256'}) == man['manifest_sha256'], 'manifest changed')
    for key in ('source_files', 'runtime_code', 'prepared_inputs'):
        verify_manifest(man[key])
    for phase in ('probe', 'full'):
        for design in man['designs']:
            workspace.verify(root / phase / design / 'workspace')
    return man


def worker_init(cpu_queue):
    os.sched_setaffinity(0, cpu_queue.get())


def evaluate_job(root, descriptor):
    man = read(Path(root) / 'manifest.json')
    verify_manifest(man['runtime_code'])
    ws, cid = descriptor['workspace'], descriptor['candidate']
    rec = read(Path(ws) / 'candidates' / cid / 'record.json')
    require(rec['status'] == 'SUBMITTED', 'candidate not fresh: ' + descriptor['id'])
    result = pipeline.evaluate(ws, cid, 'finish')
    row = collect(ws, cid, Path(result['run']).name)
    row.update(config=descriptor['config'], key=descriptor['key'], worker_affinity=sorted(os.sched_getaffinity(0)))
    return row


def recover_job(descriptor, previous=None):
    ws, cid = Path(descriptor['workspace']), descriptor['candidate']
    rec = read(ws / 'candidates' / cid / 'record.json')
    require(rec['status'] != 'RUNNING', 'interrupted RUNNING job requires a new workspace: ' + descriptor['id'])
    if rec['status'] in ('EVALUATED', 'FAILED'):
        row = collect(ws, cid, rec['last_run'])
        row.update(config=descriptor['config'], key=descriptor['key'])
        if previous:
            require(row['evidence_sha256'] == previous['evidence_sha256'], 'completed evidence changed')
            row['worker_affinity'] = previous.get('worker_affinity')
        return row
    require(previous is None, 'saved result without terminal candidate')
    require(rec['status'] == 'SUBMITTED', 'candidate bootstrap incomplete')
    return None


def compare_probe(root, rows):
    expected = read(Path(root) / 'expectations.json')
    cases = []
    for design, table in expected.items():
        for key, old in table.items():
            new = rows.get(f'probe:{design}:{key}')
            different = [] if new and old['decision_signature'] == new['decision_signature'] else [
                k for k in ('synthesis', 'physical') if not new or
                old['decision_signature'][k] != new['decision_signature'][k]]
            cases.append({'design': design, 'key': key, 'match': new is not None and not different,
                          'different_sections': different, 'old': old, 'new': new})
    out = {'status': 'PASS' if all(c['match'] for c in cases) else 'FAIL', 'cases': cases,
           'matched': sum(c['match'] for c in cases), 'total': len(cases), 'at': stamp(),
           'scope': 'empirical repeatability of 9 selected configurations and 2 raw goldens; '
                    'does not prove all configurations are deterministic'}
    write(Path(root) / 'determinism.json', out)
    return out


def run(root, workers=30, phase='all'):
    root = Path(root).resolve()
    lock = (root / 'runner.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as e:
        raise GateError('another runner is already active') from e
    man = verify(root)
    require(1 <= workers <= man['workers_cap'], 'worker count outside frozen cap')
    cpus = sorted(os.sched_getaffinity(0)); per = man['cores_per_worker']
    require(workers * per <= len(cpus), 'insufficient available CPUs for bounded affinity')
    require(shutil.disk_usage(root).free / 2 ** 30 >= man['min_free_gib'], 'disk headroom below frozen minimum')
    descriptors = read(root / 'jobs.json')
    path = root / 'results.json'
    rows = read(path) if path.exists() else {}
    start = time.monotonic()
    for current_phase in (('probe', 'full') if phase == 'all' else (phase,)):
        if current_phase == 'full':
            # A mutable checkpoint is not evidence. Revalidate probe artifacts even
            # for a separately resumed --phase full invocation before opening the gate.
            for j in (j for j in descriptors if j['phase'] == 'probe'):
                recovered = recover_job(j, rows.get(j['id']))
                require(recovered is not None, 'probe is incomplete: ' + j['id'])
                rows[j['id']] = recovered
            write(path, rows)
            d = compare_probe(root, rows)
            require(d['status'] == 'PASS', 'determinism gate failed or is incomplete; full routing not launched')
        jobs = [j for j in descriptors if j['phase'] == current_phase]
        pending = []
        for j in jobs:
            recovered = recover_job(j, rows.get(j['id']))
            if recovered:
                rows[j['id']] = recovered
            else:
                pending.append(j)
        write(path, rows)
        print(f'{current_phase}: {len(jobs) - len(pending)}/{len(jobs)} already terminal; launching {len(pending)}', flush=True)
        write(root / 'progress.json', {'status': 'RUNNING', 'phase': current_phase, 'completed': len(rows),
                                      'total': len(descriptors), 'phase_completed': len(jobs) - len(pending),
                                      'phase_total': len(jobs), 'workers': min(workers, len(pending)), 'at': stamp()})
        if pending:
            ctx = multiprocessing.get_context('fork')
            queue = ctx.Queue()
            count = min(workers, len(pending))
            for n in range(count):
                queue.put(cpus[n * per:(n + 1) * per])
            with ProcessPoolExecutor(count, mp_context=ctx, initializer=worker_init, initargs=(queue,)) as pool:
                todo = iter(pending)
                futures = {pool.submit(evaluate_job, str(root), j): j for j in [next(todo) for _ in range(count)]}
                while futures:
                    done, _ = wait(futures, return_when=FIRST_COMPLETED)
                    f = next(iter(done))
                    j = futures.pop(f)
                    try:
                        row = f.result()
                    except Exception as e:
                        write(root / 'runner_error.json', {'at': stamp(), 'job': j, 'error': repr(e)})
                        for other in futures:
                            other.cancel()
                        raise
                    rows[j['id']] = row
                    write(path, rows)
                    phase_done = sum(k.startswith(current_phase + ':') for k in rows)
                    elapsed = time.monotonic() - start
                    progress = {'status': 'RUNNING', 'phase': current_phase, 'completed': len(rows),
                                'total': len(descriptors), 'phase_completed': phase_done, 'phase_total': len(jobs),
                                'elapsed_s': round(elapsed, 1), 'workers': count, 'at': stamp()}
                    write(root / 'progress.json', progress)
                    print(f'{current_phase} {phase_done}/{len(jobs)} {j["design"]} {j["key"]} '
                          f'{row["status"]}/{row["routed"]["acceptance"]} '
                          f'area={row["routed"]["area_um2"]} ws={row["routed"]["setup_ws_ns"]}', flush=True)
                    if shutil.disk_usage(root).free / 2 ** 30 < man['min_free_gib']:
                        for other in futures:
                            other.cancel()
                        raise GateError('disk headroom below minimum; only the bounded active window can finish')
                    next_job = next(todo, None)
                    if next_job:
                        futures[pool.submit(evaluate_job, str(root), next_job)] = next_job
        if current_phase == 'probe':
            d = compare_probe(root, rows)
            print(f'Determinism {d["status"]}: {d["matched"]}/{d["total"]}', flush=True)
            require(d['status'] == 'PASS', 'determinism mismatch; inspect determinism.json before full routing')
    full_done = sum(k.startswith('full:') for k in rows)
    full_total = sum(j['phase'] == 'full' for j in descriptors)
    write(root / 'progress.json', {'status': 'COMPLETE' if full_done == full_total else 'PROBE_COMPLETE',
                                 'completed': len(rows), 'total': len(descriptors), 'full_completed': full_done,
                                 'full_total': full_total, 'elapsed_s': round(time.monotonic() - start, 1), 'at': stamp()})
    lock.close()
    return rows


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=('prepare', 'run', 'verify', 'status'))
    p.add_argument('--root', required=True)
    p.add_argument('--workers', type=int, default=30)
    p.add_argument('--phase', choices=('all', 'probe', 'full'), default='all')
    a = p.parse_args()
    try:
        if a.command == 'prepare':
            m = prepare(a.root); print(m['manifest_sha256'])
        elif a.command == 'run':
            run(a.root, a.workers, a.phase)
        elif a.command == 'verify':
            m = verify(a.root); print('PASS', m['manifest_sha256'])
        else:
            print(json.dumps(read(Path(a.root) / 'progress.json'), indent=2))
    except (GateError, OSError, KeyError, ValueError) as e:
        print(str(e), file=sys.stderr); return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
