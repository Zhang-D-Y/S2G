"""Registered arm comparison (s2g.experiment.v1): independent repeated runs per arm, each in its
own workspace, all sharing one frozen policy and one currency ledger.

Runs are interleaved (A1-r1, A2-r1, A1-r2, ...) and executed sequentially; `run` is resumable.
The primary metric is the final *confirmed* gain of every run (never the best run per arm);
failed, zero-gain and stopped runs all count.
"""
import statistics
import time
from pathlib import Path
from types import SimpleNamespace
from .common import *
from . import controller, cost, provider as pv, workspace

SCHEMA = 's2g.experiment.v1'
PRIMARY = 'confirmed_synthesis_area_gain_rel (baseline-relative; 0 when the original is retained)'


def _transport(mode, policy):
    if mode == 'live':
        return pv.OpenAICompatible(policy['provider'])
    require(mode.startswith('scripted:'), "mode must be 'live' or 'scripted:<file>'")
    return pv.Scripted(read(mode.split(':', 1)[1])['responses'])


def init(exp_root, spec, reference, tb, sdc, context, arms, repeats, policy_path, ceiling, pricing, mode):
    root = Path(exp_root).resolve()
    require(not root.exists(), 'experiment root must be new')
    require(arms and all(a in controller.ARMS for a in arms) and repeats > 0, 'invalid arms/repeats')
    policy = read(policy_path)
    identity_ = pv.live_identity(policy['provider']) if mode == 'live' else _transport(mode, policy).identity()
    root.mkdir(parents=True)
    cost.create(root / 'cost_ledger.json', ceiling, pricing)
    order = [(arm, k) for k in range(1, repeats + 1) for arm in arms]
    runs = []
    for arm, k in order:
        rid = f'{arm}-r{k}'
        ws = root / 'runs' / rid
        workspace.init(ws, spec, reference, tb, sdc, context)
        controller.init(ws, 'main', arm, policy_path, SimpleNamespace(identity=lambda: identity_),
                        cost_ledger=root / 'cost_ledger.json')
        runs.append({'run': rid, 'arm': arm, 'repeat': k, 'workspace': str(ws)})
    man = {'schema': SCHEMA, 'arms': list(arms), 'repeats': repeats, 'runs': runs, 'mode': mode,
           'policy': policy, 'policy_sha256': identity(policy), 'provider': identity_,
           'inputs': {name: digest(p) for name, p in (('spec', spec), ('reference', reference), ('tb', tb),
                                                       ('sdc', sdc), ('context', context))},
           'ceiling': ceiling, 'pricing': pricing,
           'primary_metric': policy.get('experiment', {}).get('primary_metric', PRIMARY), 'created_at': stamp()}
    man['experiment_sha256'] = identity(man)
    write(root / 'experiment.json', man)
    return {'experiment': str(root), 'runs': [r['run'] for r in runs], 'experiment_sha256': man['experiment_sha256']}


def preflight(exp_root):
    """Free checks before any paid call: key present, frozen model id served, balance >= headroom."""
    root = Path(exp_root).resolve()
    man = read(root / 'experiment.json')
    led = cost.Ledger(root / 'cost_ledger.json')
    out = {'at': stamp(), 'problems': [], 'headroom': led.headroom()}
    try:
        t = pv.OpenAICompatible(man['policy']['provider'])
        ids = [m.get('id') for m in t.get('/models').get('data', [])]
        out['served_models'] = ids
        if man['policy']['provider']['model'] not in ids:
            out['problems'].append(f"frozen model {man['policy']['provider']['model']} not served")
        bal = t.get('/user/balance')
        row = next((b for b in bal.get('balance_infos', []) if b.get('currency') == man['pricing']['currency']), None)
        out['balance'] = float(row['total_balance']) if row else None
        if not bal.get('is_available') or out['balance'] is None or out['balance'] < out['headroom']:
            out['problems'].append(f"balance {out['balance']} {man['pricing']['currency']} < remaining ceiling {out['headroom']}")
    except (GateError, OSError, ValueError, KeyError) as e:
        out['problems'].append(f'{type(e).__name__}: {str(e)[:200]}')
    out['ok'] = not out['problems']
    out['status'] = 'PASS' if out['ok'] else 'FAILED'
    write(root / 'preflight.json', out)
    return out


def run(exp_root, mode=None):
    root = Path(exp_root).resolve()
    man = read(root / 'experiment.json')
    require(identity({k: v for k, v in man.items() if k != 'experiment_sha256'}) == man['experiment_sha256'],
            'experiment manifest was modified')
    mode = mode or man['mode']
    require(mode == man['mode'], 'run mode differs from the frozen mode')
    if mode == 'live':
        pf = preflight(root)
        require(pf['ok'], 'preflight failed: ' + '; '.join(pf['problems']))
    status = []
    for r in man['runs']:
        loop = controller.Loop(r['workspace'], 'main', _transport(mode, man['policy']))
        st = loop.run()
        status.append({'run': r['run'], 'state': st['state'], 'stop_reason': st.get('stop_reason')})
    return {'runs': status, 'report': report(root)}


def _phys(ws, cid, run_name):
    run = Path(ws) / 'candidates' / cid / run_name
    risk = read(run / 'risk.json') if (run / 'risk.json').is_file() else {}
    rep = risk.get('repair') or {}
    return {'area_um2': risk.get('area_um2'), 'setup_ws_ns': worst_slack(risk, 'setup') if risk else None,
            'repair_net_edits': rep.get('net_edits'),
            'repair_s': round(sum(e['elapsed_ms'] for e in rep.get('events', [])) / 1000, 3) if rep.get('valid') else None,
            'implementation_flow_s': read(run / 'ledger.json').get('elapsed_s') if (run / 'ledger.json').is_file() else None,
            'parasitics': (risk.get('parasitics') or {}).get('provenance')}


def routed_gain(final, phys):
    """Arm-selected routed gain only after complete final confirmation.

    A physically rejected or unconfirmed synthesis winner has unknown physical
    gain, even if its area was measured. Optional routed-pool selection must be frozen
    in the controller policy before search, never added by the report after a run.
    """
    if final.get('unmet_requirements') or final.get('label') not in ('ACCEPTED_IMPLEMENTATION', 'ORIGINAL_RETAINED'):
        return None
    baseline, winner = phys.get('baseline', {}), phys.get('winner', {})
    a0, a1 = baseline.get('area_um2'), winner.get('area_um2')
    if a0 is None or a1 is None or a0 <= 0:
        return None
    if any(p.get('parasitics') != 'detailed_route_extraction' for p in (baseline, winner)):
        return None
    return round((a0 - a1) / a0, 6)


def run_metrics(r, ledger_entries):
    d = Path(r['workspace']) / 'loops/main'
    s = read(d / 'summary.json') if (d / 'summary.json').is_file() else None
    if s is None:
        return {'run': r['run'], 'arm': r['arm'], 'state': read(d / 'state.json')['state'], 'complete': False}
    st = read(d / 'state.json')
    man = read(d / 'manifest.json')
    final = s.get('final') or {}
    base, win = s['baseline'], s['final_incumbent']
    reps = final.get('repeats', {})
    unmet = final.get('unmet_requirements') or []
    gain, why = None, None
    if final.get('label') == 'ORIGINAL_RETAINED':
        gain = 0.0
    elif final and not any(m.startswith('repeat') for m in unmet) and reps.get(win) and reps.get(base):
        a0, a1 = reps[base][0]['area'], reps[win][0]['area']
        gain = round((a0 - a1) / a0, 6)
    else:
        why = 'winner not reproducibly confirmed' if final else f"run ended {s['state']}: {s.get('stop_reason')}"
    reqs = [q for a in st['attempts'] for q in a['requests']]
    first_gain, n = None, 0
    for a in st['attempts']:
        n += len(a['requests'])
        if a.get('promoted'):
            first_gain = n; break
    valid = sum(1 for a in s['attempts'] if str(a.get('correctness')).startswith('functional PASS; mapped proof PASS'))
    proposals = sum(1 for a in s['attempts'] if a.get('outcome') != 'MODEL_STOPPED')  # an explicit decline is not a candidate
    spent = [e for e in ledger_entries.values() if e['owner'] == man['manifest_sha256']]
    phys = {role: _phys(r['workspace'], cid, final['physical'][cid]['run'])
            for role, cid in (('baseline', base), ('winner', win)) if final.get('physical', {}).get(cid, {}).get('run')}
    return {'run': r['run'], 'arm': r['arm'], 'complete': True, 'state': s['state'], 'stop_reason': s.get('stop_reason'),
            'label': final.get('label'), 'unmet_requirements': unmet, 'winner': win,
            'confirmed_gain_rel': gain, 'gain_unconfirmed_reason': why,
            'confirmed_routed_gain_rel': routed_gain(final, phys),
            'submissions': len(s['attempts']), 'candidate_proposals': proposals, 'valid_candidates': valid,
            'valid_candidate_rate': round(valid / proposals, 4) if proposals else None,
            'promotions': sum(bool(a.get('promoted')) for a in s['attempts']),
            'model_requests_to_first_gain': first_gain,
            'outcomes': [a.get('outcome') for a in s['attempts']],
            'model_requests': len(reqs), 'transport_requests': sum(q['transport_requests'] for q in reqs),
            'prompt_tokens': sum(q['prompt_tokens'] for q in reqs),
            'completion_tokens': sum(q['completion_tokens'] for q in reqs),
            'response_model_mismatches': sum(1 for q in reqs for m in (q.get('response_models') or [])
                                             if m and m != man['provider'].get('model') and man['provider'].get('paid')),
            'reasoning_content_responses': sum(bool(q.get('reasoning_content')) for q in reqs),
            'cost_settled': round(sum(e['actual'] for e in spent if e['status'] == 'SETTLED'), 6),
            'cost_unknown_kept': round(sum(e['reserved'] for e in spent if e['status'] != 'SETTLED'), 6),
            'eda': s['costs'], 'physical_evaluations': s['counters']['physical_evaluations'], 'routed': phys}


def _stats(xs):
    xs = [x for x in xs if x is not None]
    return {'n': len(xs), 'mean': round(statistics.mean(xs), 6) if xs else None,
            'median': round(statistics.median(xs), 6) if xs else None, 'values': xs}


def report(exp_root):
    root = Path(exp_root).resolve()
    man = read(root / 'experiment.json')
    led = read(root / 'cost_ledger.json')
    rows = [run_metrics(r, led['entries']) for r in man['runs']]
    arms = {}
    for arm in man['arms']:
        rs = [x for x in rows if x['arm'] == arm and x['complete']]
        arms[arm] = {'runs_complete': len(rs), 'runs_planned': sum(r['arm'] == arm for r in man['runs']),
                     'confirmed_gain_rel': _stats([x['confirmed_gain_rel'] for x in rs]),
                     'confirmed_routed_gain_rel': _stats([x['confirmed_routed_gain_rel'] for x in rs]),
                     'runs_routed_unconfirmed': sum(x['confirmed_routed_gain_rel'] is None for x in rs),
                     'runs_unconfirmed': sum(x['confirmed_gain_rel'] is None for x in rs),
                     'runs_with_positive_confirmed_gain': sum((x['confirmed_gain_rel'] or 0) > 0 for x in rs),
                     'accepted_implementations': sum(x['label'] == 'ACCEPTED_IMPLEMENTATION' for x in rs),
                     'valid_candidate_rate': _stats([x['valid_candidate_rate'] for x in rs]),
                     'model_requests_to_first_gain': [x['model_requests_to_first_gain'] for x in rs],
                     'model_requests': sum(x['model_requests'] for x in rs),
                     'prompt_tokens': sum(x['prompt_tokens'] for x in rs),
                     'completion_tokens': sum(x['completion_tokens'] for x in rs),
                     'cost_settled': round(sum(x['cost_settled'] for x in rs), 6),
                     'eda_tool_invocations': sum(x['eda']['tool_invocations'] for x in rs),
                     'eda_tool_wall_s': round(sum(x['eda']['tool_wall_s'] for x in rs), 3),
                     'eda_failed_invocations': sum(x['eda']['failed_tool_invocations'] for x in rs)}
    out = {'schema': 's2g.experiment-report.v1', 'experiment_sha256': man['experiment_sha256'], 'generated_at': stamp(),
           'primary_metric': man.get('primary_metric', PRIMARY), 'arms': arms, 'runs': rows,
           'ledger': {'ceiling': led['ceiling'], 'committed': cost.Ledger.committed(led), 'currency': led['pricing']['currency'],
                      'pricing_basis': 'peak prices (upper bound)'},
           'scope': 'single design, DEV; per-run results are primary; no best-of-arm selection; '
                    'a null or negative difference is reported as is'}
    write(root / 'report.json', out)
    return out
