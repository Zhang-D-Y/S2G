"""s2g.feedback.v2: bounded, candidate-bound feedback packets and detail queries.

Snapshots are read only through verified run evidence (hash- and identity-checked), so
stale or foreign artifacts are rejected. The full issue inventory is kept; prompt-size
limits apply only to how many examples are shown, never to whether a blocking category
is disclosed.
"""
import json
import re
from pathlib import Path
from .common import *
from .pipeline import run_evidence

SCHEMA = 's2g.feedback.v2'
CATEGORIES = ('functional', 'mapping', 'constraint', 'timing', 'electrical', 'physical', 'structure', 'frontend')
SEVERITY = {'blocking': 0, 'violation': 1, 'warning': 2, 'info': 3}
EXPOSURE = {'A1': ('contract', 'correctness'), 'A2': ('contract', 'correctness', 'synthesis'),
            'A3': ('contract', 'correctness', 'synthesis', 'physical')}
SYNTH_CATEGORIES = {'mapping', 'constraint', 'timing', 'electrical', 'structure', 'frontend'}
QUERY_TYPES = {'path_detail': 'synthesis', 'rtl_region': 'contract', 'cone': 'synthesis'}


def read_feedback(path):
    """Version dispatch. v1 files (search.feedback) are wrapped read-only, never rewritten."""
    data = read(path)
    if data.get('schema') == SCHEMA:
        return data
    require(data.get('schema') == 's2g.feedback.v1', 'unknown feedback schema: ' + str(data.get('schema')))
    return {'schema': 's2g.feedback.v1', 'legacy': True, 'candidate_id': data['candidate'], 'run': data['run'],
            'rtl_sha256': data['rtl_hash'], 'stage_status': data['status'],
            'functional_status': (data.get('functional') or {}).get('status'), 'issues': None,
            'note': 'legacy v1 report: no issue inventory, units or truncation fields', 'raw': data}


def snapshot(root, cid, run_name=None):
    """Verified evidence of one terminal run; raises GateError on any identity mismatch."""
    run, rec, ledger = run_evidence(root, cid, run_name)
    # The pipeline records a stage only after it passes; a FAILED run without it failed there.
    failed = ledger['status'] == 'FAILED'
    func = (ledger.get('functional') or {}).get('status') or ('FAIL' if failed else 'NOT_RUN')
    mapped = (ledger.get('synthesis') or {}).get('mapped_proof') or ('FAIL' if failed and func == 'PASS' else 'NOT_RUN')
    error = ledger.get('error') and str(ledger['error']).replace(str(Path(root).resolve()) + '/', '')
    snap = {'candidate': cid, 'run': run.name, 'rtl_sha256': rec['rtl_hash'], 'status': ledger['status'],
            'error': error, 'functional': func, 'mapped_proof': mapped, 'synthesis': None, 'physical': None,
            'run_dir': str(run)}
    ev_path = run / 'synthesis_evidence.json'
    if ev_path.is_file():
        ev = read(ev_path)
        require(ev['rtl_sha256'] == rec['rtl_hash'] and ev['candidate'] == cid and ev['run'] == run.name,
                'synthesis evidence does not belong to this candidate/run')
        snap['synthesis'] = ev
    if (run / 'risk.json').is_file():
        require('finish' in ledger.get('stages', {}), 'physical metrics without a recorded finish stage')
        snap['physical'] = {'risk': read(run / 'risk.json'), 'acceptance': read(run / 'acceptance.json')}
    snap['physical_failure'] = None
    if failed and func == 'PASS' and mapped == 'PASS' and not snap['physical'] and (run / 'backend').is_dir():
        snap['physical_failure'] = early_physical_failure(run, ledger)
    return snap


def early_physical_failure(run, ledger):
    """Bounded diagnostic from a terminal run's registered failed backend job.

    Missing routed measurements stay unknown. A job receipt cannot redirect a
    feedback read to a foreign or unregistered log.
    """
    registered = read(run / 'evidence.json')['files']
    stage_names = {'place': 'placement', 'floorplan': 'floorplan', 'cts': 'cts',
                   'route': 'route', 'finish': 'finish'}
    for receipt in sorted((run / 'backend').glob('*.log.job.json')):
        if str(receipt.resolve()) not in registered:
            continue
        j = read(receipt)
        if j.get('exit_code') == 0 and not j.get('timed_out'):
            continue
        log = Path(j['log']).resolve()
        require(log.is_relative_to(run.resolve()), 'failed backend log is outside this run')
        require(str(log) in registered, 'failed backend log is not registered evidence')
        lines = log.read_text(errors='replace').splitlines()
        first = next((n for n, line in enumerate(lines) if re.search(r'\[ERROR\b|^Error:', line)), None)
        reason = '\n'.join(lines[first:first + 6] if first is not None else lines[-6:])
        if j.get('timed_out'):
            reason = 'tool timed out; ' + reason
        reason = reason.replace(str(run.resolve()) + '/', '')[:600]
        target = receipt.name.removesuffix('.log.job.json')
        return {'stage': stage_names.get(target, target), 'reason': reason or str(ledger.get('error'))[:600],
                'artifact_ref': str(log.relative_to(run.resolve())), 'status': 'FAILED'}
    return {'stage': 'backend', 'reason': str(ledger.get('error') or 'physical flow failed')[:600],
            'artifact_ref': 'ledger.json', 'status': 'FAILED'}


def functional_issues(snap):
    if snap['functional'] == 'PASS' and snap['mapped_proof'] == 'PASS':
        return []
    reason = (snap['error'] or 'functional/mapped-proof gate did not pass')[:400]
    cat = 'functional' if snap['functional'] != 'PASS' else 'mapping'
    return [{'issue_id': 'gate-001', 'category': cat, 'evidence_kind': 'observation', 'severity': 'blocking',
             'summary': reason, 'object_refs': [], 'artifact_ref': 'ledger.json', 'rtl_mapping_status': 'not_applicable',
             'rtl_refs': []}]


def physical_issues(snap):
    if not snap['physical']:
        failure = snap.get('physical_failure')
        if not failure:
            return []
        return [{'issue_id': 'phy-flow-001', 'category': 'physical', 'evidence_kind': 'observation',
                 'severity': 'blocking',
                 'summary': f"{failure['stage']} physical flow failed before final-route measurements: {failure['reason']}",
                 'object_refs': [], 'artifact_ref': failure['artifact_ref'],
                 'rtl_mapping_status': 'not_applicable', 'rtl_refs': []}]
    risk, acc = snap['physical']['risk'], snap['physical']['acceptance']
    out = []
    stage = 'final route (' + str(risk.get('parasitics', {}).get('provenance')) + ')'
    # A failed routed acceptance gate blocks implementation acceptance, so it is never truncated away.
    for gate, ok in acc['gates'].items():
        if not ok:
            out.append(('physical', 'blocking', f'{stage}: acceptance gate {gate} not met'))
    ws = worst_slack(risk, 'setup')
    if ws is not None:
        out.append(('physical', 'violation' if ws < 0 else 'info', f'{stage}: setup worst slack {ws} ns, TNS {risk.get("setup_tns")} ns, '
                    f'area {risk.get("area_um2")} um^2, repair net edits {risk.get("repair", {}).get("net_edits")}'))
    return [{'issue_id': f'phy-{i:03d}', 'category': c, 'evidence_kind': 'observation', 'severity': s, 'summary': t,
             'object_refs': [], 'artifact_ref': 'risk.json', 'rtl_mapping_status': 'not_applicable', 'rtl_refs': []}
            for i, (c, s, t) in enumerate(out, 1)]


def inventory(snap, exposure):
    """Complete issue inventory visible to this arm (never truncated)."""
    issues = functional_issues(snap)
    if 'synthesis' in exposure and snap['synthesis']:
        issues += snap['synthesis']['issues']
    if 'physical' in exposure:
        issues += physical_issues(snap)
    return sorted(issues, key=lambda i: (SEVERITY[i['severity']], CATEGORIES.index(i['category']), i['issue_id']))


def select(issues, max_examples):
    """All blocking issues always; then round-robin across categories, skipping timing paths
    whose RTL cone was already shown, until max_examples. Deterministic (inventory order breaks ties)."""
    chosen = [i for i in issues if i['severity'] == 'blocking']
    rest = [i for i in issues if i['severity'] != 'blocking']
    by_cat = {c: [i for i in rest if i['category'] == c] for c in CATEGORIES}
    seen = set()
    while len(chosen) < max_examples and any(by_cat.values()):
        for c in CATEGORIES:
            while by_cat[c] and len(chosen) < max_examples:
                i = by_cat[c].pop(0)
                # timing paths sharing one RTL cone are near-duplicates; the rest stay queryable
                key = (c, tuple(i['rtl_refs'])) if c == 'timing' and i['rtl_refs'] else None
                if key is not None and key in seen:
                    continue
                seen.add(key); chosen.append(i)
                break
    return chosen


def metrics_view(snap, exposure):
    if 'synthesis' not in exposure or not snap or not snap['synthesis']:
        return None
    m = snap['synthesis']['metrics']
    view = {k: {'value': m[k]['value'], 'unit': m[k]['unit'], 'status': m[k]['status']}
            for k in ('mapped_area', 'cell_count', 'logic_depth', 'setup_ws', 'setup_tns', 'unconstrained_endpoints')}
    view['fingerprint'] = snap['synthesis']['structure']['fingerprint']
    if 'physical' in exposure and snap['physical']:
        r = snap['physical']['risk']
        view['final_route'] = {'setup_ws_ns': worst_slack(r, 'setup'), 'area_um2': r.get('area_um2'),
                               'parasitics': r.get('parasitics', {}).get('provenance'),
                               'acceptance': snap['physical']['acceptance']['status']}
    return view


def delta(a, b):
    out = {}
    for k in ('mapped_area', 'logic_depth', 'setup_ws'):
        x, y = (a or {}).get(k, {}), (b or {}).get(k, {})
        ok = x.get('status') == y.get('status') == 'VALID' and x.get('unit') == y.get('unit')
        out[k] = {'value': round(y['value'] - x['value'], 6) if ok else None, 'unit': y.get('unit'),
                  'status': 'VALID' if ok else 'INCOMPATIBLE_OR_UNKNOWN'}
    return out


def section(snap, exposure, caps, reference=None):
    """Exposure-filtered evidence of one candidate: complete counts, bounded examples."""
    issues = inventory(snap, exposure)
    shown = select(issues, caps['max_issue_examples'])
    counts = {}
    for i in issues:
        counts.setdefault(i['category'], {}).setdefault(i['severity'], 0)
        counts[i['category']][i['severity']] += 1
    m = metrics_view(snap, exposure)
    return {'candidate_id': snap['candidate'], 'run': snap['run'], 'rtl_sha256': snap['rtl_sha256'],
            'stage': 'physical' if 'physical' in exposure and (snap['physical'] or snap.get('physical_failure')) else 'synthesis',
            'stage_status': snap['status'], 'functional_status': snap['functional'], 'metrics': m,
            'delta_vs_reference': delta(reference, m) if reference and m else None,
            'issues': shown, 'issue_counts': counts,
            'blocking_categories': sorted({i['category'] for i in issues if i['severity'] == 'blocking'}),
            'detail_selection': {'truncated': len(shown) < len(issues), 'omitted_issue_count': len(issues) - len(shown),
                                 'policy': 'all blocking; then category round-robin with RTL-cone diversity'}}


def catalog_for(snap, exposure):
    out = [{'evidence_id': 'rtl', 'query_type': 'rtl_region', 'candidate': snap['candidate']}]
    if 'synthesis' in exposure and snap['synthesis']:
        out += [{'evidence_id': p['path_id'], 'query_type': t, 'candidate': snap['candidate']}
                for p in snap['synthesis']['paths'] for t in ('path_detail', 'cone')]
    return out


def build_packet(*, loop, arm, incumbent, baseline, recent, budget, caps, recent_candidate=None):
    """Mandatory evidence for the incumbent and, when one exists, the most recently evaluated
    non-incumbent candidate, so failed attempts (including routed failures in A3) feed back."""
    exposure = EXPOSURE[arm]
    base_m = metrics_view(baseline, exposure)
    inc = section(incumbent, exposure, caps, base_m)
    rec = section(recent_candidate, exposure, caps, inc['metrics']) if recent_candidate else None
    catalog = catalog_for(incumbent, exposure) + (catalog_for(recent_candidate, exposure) if recent_candidate else [])
    return {'schema': SCHEMA, 'loop_id': loop['loop_id'], 'arm': arm, 'exposure': list(exposure),
            'manifest_sha256': loop['manifest_sha256'], 'toolchain_id': loop['toolchain_id'],
            'policy_version': loop['policy']['policy_version'], 'corner_id': loop['corner'],
            **{k: v for k, v in inc.items() if k not in ('metrics', 'delta_vs_reference')},
            'metrics': {'incumbent': inc['metrics'], 'baseline': base_m, 'delta_vs_baseline': inc['delta_vs_reference']},
            'recent_candidate': rec, 'recent_attempts': recent, 'remaining_budget': budget, 'query_catalog': catalog}


def _mandatory(title, sec):
    lines = [f"{title} {sec['candidate_id']} (rtl {sec['rtl_sha256'][:12]}): stage {sec['stage']}, status {sec['stage_status']}, "
             f"functional {sec['functional_status']}, blocking categories: {sec['blocking_categories'] or 'none'}.",
             'Issue counts (complete inventory): ' + json.dumps(sec['issue_counts'], sort_keys=True)]
    lines += [_issue_line(i) for i in sec['issues'] if i['severity'] == 'blocking']
    if sec['detail_selection']['truncated']:
        lines.append(f"truncated=true: {sec['detail_selection']['omitted_issue_count']} lower-priority issue(s) omitted; "
                     'counts above are complete.')
    return lines


def render(packet, cap):
    """Prompt text for a packet. The mandatory part (identity, status, complete counts and every
    blocking issue of each section) must fit `cap`; otherwise raise rather than drop gate failures."""
    head = [f"Evidence pushed by the controller; corner {packet['corner_id']}, policy {packet['policy_version']}; "
            f"information exposed: {', '.join(packet['exposure'])}."]
    head += _mandatory('Incumbent', packet)
    rec = packet.get('recent_candidate')
    if rec:
        head += _mandatory('Most recent evaluated candidate (not promoted)', rec)
    mandatory = '\n'.join(head)
    require(len(mandatory) <= cap, f'mandatory feedback ({len(mandatory)} chars) exceeds cap {cap}')
    body = []
    if packet['metrics']['incumbent']:
        body.append('Metrics (incumbent): ' + json.dumps(packet['metrics']['incumbent'], sort_keys=True))
        body.append('Metrics (original baseline): ' + json.dumps(packet['metrics']['baseline'], sort_keys=True))
        body.append('Delta incumbent - baseline: ' + json.dumps(packet['metrics']['delta_vs_baseline'], sort_keys=True))
    if rec and rec['metrics']:
        body.append(f"Metrics (candidate {rec['candidate_id']}): " + json.dumps(rec['metrics'], sort_keys=True))
        body.append(f"Delta candidate {rec['candidate_id']} - incumbent: " + json.dumps(rec['delta_vs_reference'], sort_keys=True))
    body += [_issue_line(i) for i in packet['issues'] if i['severity'] != 'blocking']
    if rec:
        body += [f"({rec['candidate_id']}) " + _issue_line(i) for i in rec['issues'] if i['severity'] != 'blocking']
    out, dropped = [mandatory], 0
    for line in body:
        if sum(len(x) + 1 for x in out) + len(line) > cap:
            dropped += 1; continue
        out.append(line)
    if dropped:
        out.append(f'[{dropped} optional line(s) omitted to fit the {cap}-char packet cap]')
    return '\n'.join(out), dropped


def _issue_line(i):
    refs = f" refs={i['object_refs']}" if i['object_refs'] else ''
    rtl = f" rtl={i['rtl_refs']} ({i['rtl_mapping_status']})" if i['rtl_refs'] else ''
    return f"- [{i['severity']}/{i['category']}/{i['evidence_kind']}] {i['issue_id']}: {i['summary']}{refs}{rtl}"


def query(root, snap, allowed_candidates, exposure, evidence_id, query_type, limit):
    """Bounded read-only detail page from an already delivered, candidate-bound snapshot."""
    require(snap['candidate'] in allowed_candidates, 'query target is not an evaluated candidate of this loop')
    require(query_type in QUERY_TYPES, 'unknown query type')
    require(QUERY_TYPES[query_type] in exposure, 'query type not exposed to this arm')
    run = Path(snap['run_dir'])
    require(run.resolve().is_relative_to(Path(root).resolve()), 'query outside this task workspace')
    if query_type == 'rtl_region':
        m = re.fullmatch(r'rtl(?::(\d+)-(\d+))?', evidence_id)
        require(m is not None, 'rtl evidence id must be rtl or rtl:<first>-<last>')
        lines = (run.parent / 'rtl.v').read_text().splitlines()
        a, b = (int(m.group(1)), int(m.group(2))) if m.group(1) else (1, len(lines))
        text = '\n'.join(f'{n}: {lines[n - 1]}' for n in range(max(a, 1), min(b, len(lines)) + 1))
    else:
        require(snap['synthesis'] is not None, 'no synthesis evidence for this candidate')
        path = next((p for p in snap['synthesis']['paths'] if p['path_id'] == evidence_id), None)
        require(path is not None, 'unknown evidence id ' + evidence_id)
        if query_type == 'cone':
            text = json.dumps(path['rtl'], sort_keys=True)
        else:
            raw = read(run / 'sta/paths.json')['checks'][int(evidence_id.split('-')[1]) - 1]
            pins = [f"{p['pin']} arr={p['arrival'] * 1e9:.4f}ns slew={p.get('slew', 0) * 1e9:.4f}ns"
                    for p in raw.get('source_path', [])]
            text = json.dumps({k: path[k] for k in ('startpoint', 'endpoint', 'slack_ns', 'masters')}) + '\n' + '\n'.join(pins)
    truncated = len(text) > limit
    return {'candidate': snap['candidate'], 'rtl_sha256': snap['rtl_sha256'], 'evidence_id': evidence_id,
            'query_type': query_type, 'text': text[:limit], 'truncated': truncated, 'chars': min(len(text), limit)}
