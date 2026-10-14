#!/usr/bin/env python3
"""Offline stress-contract pilot for the ALU landscapes (no model calls).

prepare: freeze a seeded configuration sample (from the current-contract tables) and, for every
         contract in configs/alu-stress-contracts-v1.json, new workspaces whose context differs
         from the frozen one only in core/die area and placement density. Plus repeat workspaces.
run    : every job through the full production flow in parallel (bounded workers, 4 CPUs each);
         resumable; completed evidence is re-read, never re-run; RUNNING candidates stop recovery.
report : per contract and design, routed outcomes vs the current contract, rejection rate among
         synthesis-promotable configurations, causes, repair edits, repeatability, and the result
         of the predeclared selection rule.
Existing landscape roots, route_alu_landscape.py and the hybrid registration stay read-only.
"""
import argparse
import fcntl
import json
import multiprocessing
import os
import random
import shutil
import statistics
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / 'scripts'))
import route_alu_landscape as R  # noqa: E402  (reused helpers; that script is not modified)
from s2g import pipeline, workspace  # noqa: E402
from s2g.common import GateError, digest, files_manifest, identity, read, require, stamp, tree_files, verify_manifest, write  # noqa: E402

SCHEMA = 's2g.alu-stress-pilot.v1'
DESIGNS = {'riscv32': 'alu-landscape-riscv32-v1', 'kleine32': 'alu-landscape-kleine32-v1'}
CURRENT = BASE / 'runs/alu-routed-landscape-current-v1'
FLOORPLAN = ('core_area', 'die_area', 'place_density')


def promotable(row, golden):
    return (row['functional'] == row['mapped_proof'] == 'PASS' and row['area_um2'] is not None
            and row['setup_ws_ns'] is not None and row['setup_ws_ns'] >= 0 and row['fingerprint'] != golden['fingerprint']
            and (golden['area_um2'] - row['area_um2']) / golden['area_um2'] >= 0.005)


def current_tables():
    rows = read(CURRENT / 'results.json')
    out = {d: {} for d in DESIGNS}
    for jid, row in rows.items():
        phase, design, key = jid.split(':', 2)
        if phase == 'full':
            out[design][key] = row
    for d, t in out.items():
        require(len(t) == 433, f'current routed table incomplete for {d}')
    return out


def choose_sample(table, spec, rng):
    golden = table[R.GOLD_KEY]
    accepted = {k: r for k, r in table.items() if k != R.GOLD_KEY and r['routed']['acceptance'] == 'ACCEPTED'}
    optimum = min(accepted, key=lambda k: (accepted[k]['routed']['area_um2'], k))
    prom = sorted(k for k, r in table.items() if k != R.GOLD_KEY and promotable(r, golden))
    rule_best = min(prom, key=lambda k: (table[k]['area_um2'], k))
    chosen = list(dict.fromkeys([R.GOLD_KEY, optimum, rule_best]))  # optimum may equal the rule best
    pool = [k for k in prom if k not in chosen]
    chosen += sorted(rng.sample(pool, spec['promotable_random']))
    neg = sorted(k for k, r in table.items() if k != R.GOLD_KEY and r['setup_ws_ns'] is not None
                 and r['setup_ws_ns'] < 0 and k not in chosen)
    chosen += sorted(rng.sample(neg, spec['negative_synthesis_slack_random']))
    roles = {R.GOLD_KEY: 'golden', optimum: 'routed_optimum_C0', rule_best: 'synthesis_rule_best_C0'}
    if optimum == rule_best:
        roles[optimum] = 'routed_optimum_C0+synthesis_rule_best_C0'
    require(len(set(chosen)) == len(chosen), 'duplicate sample keys')
    return chosen, roles, {'routed_optimum_C0': optimum, 'synthesis_rule_best_C0': rule_best}


def prepare(root, contracts_path):
    root = Path(root).resolve()
    require(root.is_relative_to(BASE.parents[0]) and not root.exists(), 'root must be new and under S2G')
    cfg = read(contracts_path)
    require(cfg.get('schema') == 's2g.alu-stress-contracts.v1', 'unknown contracts schema')
    tables = current_tables()
    root.mkdir(parents=True)
    rng = random.Random(cfg['sample']['seed'])
    jobs, sample, sources = [], {}, [CURRENT / 'results.json', Path(contracts_path)]
    for design, src_name in DESIGNS.items():
        src = BASE / 'runs' / src_name
        index = read(src / 'index.json')
        keys, roles, named = choose_sample(tables[design], cfg['sample'], rng)
        repeats = [named.get(k, k) for k in cfg['sample']['repeats']]
        sample[design] = {'keys': keys, 'roles': roles, 'repeats': repeats}
        old = src / 'workspace'
        entries = {}
        for k in set(keys) | set(repeats):
            cid = index[k]['candidate']
            path = old / 'candidates' / cid / 'rtl.v'
            rec = read(old / 'candidates' / cid / 'record.json')
            require(digest(path) == rec['rtl_hash'] == tables[design][k]['rtl_sha256'], 'source RTL changed: ' + k)
            entries[k] = {'rtl': str(path), 'rtl_sha256': rec['rtl_hash'], 'config': index[k].get('config')}
            sources.append(path)
        base_ctx = read(old / 'frozen/context.json')
        require({k: base_ctx[k] for k in FLOORPLAN} == {k: cfg['reference_contract'][k] for k in FLOORPLAN},
                'reference floorplan differs from the frozen current contract')
        for c in cfg['contracts']:
            inp = root / c['name'] / design / 'inputs'
            inp.mkdir(parents=True)
            for fn in ('spec.json', 'reference.v', 'tb.v', 'constraints.sdc', 'review.json'):
                shutil.copyfile(old / 'frozen' / fn, inp / fn)
                sources.append(old / 'frozen' / fn)
            for group, gkeys in (('main', keys), ('repeat', repeats)):
                ctx = dict(base_ctx, **{k: c[k] for k in FLOORPLAN})
                ctx.update(candidate_budget=len(gkeys), tool_run_budget=len(gkeys))
                d = root / c['name'] / design / group
                write(d / 'context.json', ctx)
                ws = d / 'workspace'
                workspace.init(ws, inp / 'spec.json', inp / 'reference.v', inp / 'tb.v', inp / 'constraints.sdc',
                               d / 'context.json', inp / 'review.json')
                cids = R.bulk_import_new(ws, [(k, entries[k]) for k in gkeys])
                jobs += [{'id': f"{c['name']}:{design}:{group}:{k}", 'contract': c['name'], 'design': design,
                          'group': group, 'key': k, 'workspace': str(ws), 'candidate': cids[k]} for k in gkeys]
    write(root / 'sample.json', sample)
    write(root / 'jobs.json', jobs)
    man = {'schema': SCHEMA, 'created_at': stamp(), 'model_calls': 0, 'contracts': cfg,
           'contracts_sha256': digest(contracts_path), 'source_files': files_manifest(sources),
           'runtime_code': files_manifest(tree_files(BASE / 's2g') + [Path(__file__), Path(R.__file__)]),
           'prepared': files_manifest([root / 'sample.json', root / 'jobs.json']), 'jobs': len(jobs),
           'workers_cap': 30, 'cores_per_worker': 4, 'min_free_gib': 40}
    man['manifest_sha256'] = identity(man)
    write(root / 'manifest.json', man)
    return {'root': str(root), 'jobs': len(jobs), 'sample': {d: len(s['keys']) for d, s in sample.items()}}


def verify(root):
    man = read(Path(root) / 'manifest.json')
    require(identity({k: v for k, v in man.items() if k != 'manifest_sha256'}) == man['manifest_sha256'], 'manifest changed')
    for k in ('source_files', 'runtime_code', 'prepared'):
        verify_manifest(man[k])
    return man


def _evaluate(root, job):
    verify_manifest(read(Path(root) / 'manifest.json')['runtime_code'])
    rec = read(Path(job['workspace']) / 'candidates' / job['candidate'] / 'record.json')
    require(rec['status'] == 'SUBMITTED', 'candidate not fresh: ' + job['id'])
    r = pipeline.evaluate(job['workspace'], job['candidate'], 'finish')
    return R.collect(job['workspace'], job['candidate'], Path(r['run']).name)


def _recover(job):
    rec = read(Path(job['workspace']) / 'candidates' / job['candidate'] / 'record.json')
    require(rec['status'] != 'RUNNING', 'interrupted RUNNING job requires a new root: ' + job['id'])
    return R.collect(job['workspace'], job['candidate'], rec['last_run']) if rec['status'] in ('EVALUATED', 'FAILED') else None


def run(root, workers):
    root = Path(root).resolve()
    lock = (root / 'runner.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as e:
        raise GateError('another runner is active') from e
    man = verify(root)
    cpus, per = sorted(os.sched_getaffinity(0)), man['cores_per_worker']
    require(1 <= workers <= man['workers_cap'] and workers * per <= len(cpus), 'worker count outside bounds')
    require(shutil.disk_usage(root).free / 2 ** 30 >= man['min_free_gib'], 'disk headroom too low')
    rows = read(root / 'results.json') if (root / 'results.json').exists() else {}
    pending = []
    for j in read(root / 'jobs.json'):
        r = _recover(j)
        if r:
            rows[j['id']] = r
        else:
            pending.append(j)
    write(root / 'results.json', rows)
    print(f'{len(rows)} terminal, launching {len(pending)}', flush=True)
    if pending:
        ctx = multiprocessing.get_context('fork')
        queue = ctx.Queue()
        n = min(workers, len(pending))
        for i in range(n):
            queue.put(cpus[i * per:(i + 1) * per])
        start = time.monotonic()
        with ProcessPoolExecutor(n, mp_context=ctx, initializer=R.worker_init, initargs=(queue,)) as pool:
            todo = iter(pending)
            futures = {pool.submit(_evaluate, str(root), j): j for j in [next(todo) for _ in range(n)]}
            while futures:
                done, _ = wait(futures, return_when=FIRST_COMPLETED)
                f = next(iter(done)); j = futures.pop(f)
                try:
                    rows[j['id']] = f.result()
                except Exception as e:
                    write(root / 'runner_error.json', {'at': stamp(), 'job': j, 'error': repr(e)})
                    for o in futures:
                        o.cancel()
                    raise
                write(root / 'results.json', rows)
                p = rows[j['id']]['routed']
                print(f"{len(rows)} {j['id']} {p['acceptance']} area={p['area_um2']} ws={p['setup_ws_ns']} "
                      f"edits={p['repair_net_edits']} t={time.monotonic() - start:.0f}s", flush=True)
                nxt = next(todo, None)
                if nxt:
                    futures[pool.submit(_evaluate, str(root), nxt)] = nxt
    lock.close()
    return {'terminal': len(rows)}


def _failed_gates(p):
    return sorted(g for g, ok in (p.get('gates') or {}).items() if not ok)


def report(root):
    root = Path(root).resolve()
    man = verify(root)
    cfg, sample = man['contracts'], read(root / 'sample.json')
    rows, cur = read(root / 'results.json'), current_tables()
    out = {'schema': 's2g.alu-stress-pilot-report.v1', 'generated_at': stamp(), 'model_calls': 0,
           'manifest_sha256': man['manifest_sha256'], 'contracts': {}}
    for c in cfg['contracts']:
        res = {}
        for design, s in sample.items():
            golden_c0 = cur[design][R.GOLD_KEY]
            got = {k: rows.get(f"{c['name']}:{design}:main:{k}") for k in s['keys']}
            reps = {k: rows.get(f"{c['name']}:{design}:repeat:{k}") for k in s['repeats']}
            known = {k: r for k, r in got.items() if r and r['routed']['outcome_known']}
            synth_same = all(r['decision_signature']['synthesis'] == cur[design][k]['decision_signature']['synthesis']
                             for k, r in known.items())
            prom = [k for k in s['keys'] if k != R.GOLD_KEY and promotable(cur[design][k], golden_c0)]
            rej = [k for k in prom if k in known and known[k]['routed']['acceptance'] != 'ACCEPTED']
            g = known.get(R.GOLD_KEY)
            edits = lambda rs: [r['routed']['repair_net_edits'] for r in rs if r['routed']['repair_net_edits'] is not None]  # noqa: E731
            acc = [r for k, r in known.items() if r['routed']['acceptance'] == 'ACCEPTED']
            best = min(acc, key=lambda r: r['routed']['area_um2']) if acc else None
            res[design] = {
                'jobs': len(got), 'known': len(known), 'unknown': sorted(k for k in got if k not in known),
                'synthesis_metrics_match_C0': synth_same,
                'golden': None if not g else {'acceptance': g['routed']['acceptance'], 'area_um2': g['routed']['area_um2'],
                                              'setup_ws_ns': g['routed']['setup_ws_ns'], 'repair_net_edits': g['routed']['repair_net_edits'],
                                              'parasitics': g['routed']['parasitics'], 'failed_gates': _failed_gates(g['routed'])},
                'accepted': len(acc), 'rejected': len(known) - len(acc),
                'promotable_sampled': len(prom), 'promotable_rejected': len(rej),
                'promotable_rejection_rate': round(len(rej) / len(prom), 4) if prom else None,
                'rejected_causes': {k: _failed_gates(known[k]['routed']) for k in known if known[k]['routed']['acceptance'] != 'ACCEPTED'},
                'repair_edits': {'C0': statistics.median(edits([cur[design][k] for k in known])) if known else None,
                                 'contract': statistics.median(edits(known.values())) if known else None,
                                 'contract_range': [min(edits(known.values()), default=None), max(edits(known.values()), default=None)]},
                'best_accepted': None if not best else {'key': next(k for k, r in known.items() if r is best),
                                                        'area_um2': best['routed']['area_um2'],
                                                        'gain_vs_golden': round((g['routed']['area_um2'] - best['routed']['area_um2'])
                                                                                / g['routed']['area_um2'], 6) if g and g['routed']['area_um2'] else None},
                'repeats_match': {k: bool(reps[k] and got.get(k) and reps[k]['decision_signature'] == got[k]['decision_signature'])
                                  for k in s['repeats']},
                'rows': {k: {'role': s['roles'].get(k, 'promotable' if k in prom else 'negative_synthesis_slack'),
                             'C0': {x: cur[design][k]['routed'][x] for x in ('acceptance', 'area_um2', 'setup_ws_ns', 'repair_net_edits')},
                             'contract': None if not r else {x: r['routed'][x] for x in ('acceptance', 'area_um2', 'setup_ws_ns', 'repair_net_edits')}
                             | {'failed_gates': _failed_gates(r['routed'])}} for k, r in got.items()}}
        lo, hi = 0.15, 0.60
        conds = {'goldens_accepted_extracted': all(v['golden'] and v['golden']['acceptance'] == 'ACCEPTED'
                                                   and v['golden']['parasitics'] == 'detailed_route_extraction' for v in res.values()),
                 'repeats_exact': all(all(v['repeats_match'].values()) for v in res.values()),
                 'promotable_rejection_in_range': all(v['promotable_rejection_rate'] is not None and lo <= v['promotable_rejection_rate'] <= hi
                                                      for v in res.values()),
                 'no_unknown_outcomes': all(not v['unknown'] for v in res.values())}
        out['contracts'][c['name']] = {'floorplan': {k: c[k] for k in FLOORPLAN}, 'designs': res,
                                       'conditions': conds, 'qualifies': all(conds.values())}
    order = cfg['selection_rule']['order']
    chosen = next((n for n in order if out['contracts'].get(n, {}).get('qualifies')), None)
    out['selection'] = {'rule': cfg['selection_rule'], 'chosen': chosen,
                        'note': None if chosen else 'no contract satisfies the predeclared rule'}
    write(root / 'report.json', out)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('command', choices=('prepare', 'run', 'report'))
    ap.add_argument('--root', required=True)
    ap.add_argument('--contracts', default=str(BASE / 'configs/alu-stress-contracts-v1.json'))
    ap.add_argument('--workers', type=int, default=30)
    a = ap.parse_args()
    r = prepare(a.root, a.contracts) if a.command == 'prepare' else run(a.root, a.workers) if a.command == 'run' else report(a.root)
    print(json.dumps(r if a.command != 'report' else {n: {'qualifies': v['qualifies'], 'conditions': v['conditions']}
                                                       for n, v in r['contracts'].items()} | {'chosen': r['selection']['chosen']},
                     indent=2))


if __name__ == '__main__':
    main()
