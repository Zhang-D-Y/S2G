#!/usr/bin/env python3
"""Offline landscape of the frozen ALU action space (no model calls).

prepare : new workspace from frozen task inputs; budgets sized to the action space (recorded)
evaluate: every configuration through the full synthesis-stage gate (TB, RTL SAT, synthesis,
          mapped SAT, STA); resumable, completed candidates are never re-run
route   : full physical evaluation for named configurations (e.g. the synthesis Pareto set)
report  : landscape.json plus non-LLM search baselines over the measured table
"""
import argparse
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from s2g import alu, baselines, pipeline, workspace
from s2g.common import digest, files_manifest, identity, read, require, stamp, tree_files, write


def prepare(root, inputs, ir_path, vocab_path, context_path, route_quota):
    root, inputs = Path(root).resolve(), Path(inputs)
    require(not root.exists(), 'root must be new')
    ir, vocab = alu.validate_ir(read(ir_path)), alu.load_vocabulary(vocab_path)
    configs = alu.space(ir, vocab)
    ctx = read(context_path)
    ctx.update(candidate_budget=len(configs) + 1, tool_run_budget=len(configs) + 1 + route_quota)
    root.mkdir(parents=True)
    write(root / 'context.json', ctx)
    workspace.init(root / 'workspace', inputs / 'spec.json', inputs / 'golden.v', inputs / 'tb.v',
                   inputs / 'constraints.sdc', root / 'context.json', inputs / 'spec.json.review.json')
    rec = workspace.add(root / 'workspace', inputs / 'golden.v')
    write(root / 'protocol.json', {
        'schema': 's2g.alu-landscape.v1', 'created_at': stamp(), 'model_calls': 0,
        'inputs': files_manifest([inputs / n for n in ('spec.json', 'golden.v', 'tb.v', 'constraints.sdc')]),
        'ir': read(ir_path), 'ir_sha256': digest(ir_path), 'vocabulary': vocab, 'vocabulary_sha256': digest(vocab_path),
        'code': files_manifest(tree_files(BASE / 's2g') + [Path(__file__)]),
        'budget_override': {'candidate_budget': ctx['candidate_budget'], 'tool_run_budget': ctx['tool_run_budget'],
                            'reason': 'offline enumeration of the whole action space'},
        'golden_candidate': rec['id'], 'configurations': len(configs)})
    return {'root': str(root), 'configurations': len(configs)}


def _row(ws, cid, run):
    led = read(run / 'ledger.json')
    ev = read(run / 'synthesis_evidence.json') if (run / 'synthesis_evidence.json').is_file() else None
    m = (ev or {}).get('metrics', {})
    val = lambda k: m.get(k, {}).get('value') if m.get(k, {}).get('status') == 'VALID' else None  # noqa: E731
    return {'candidate': cid, 'run': run.name, 'status': led['status'],
            'functional': (led.get('functional') or {}).get('status') or ('FAIL' if led['status'] == 'FAILED' else None),
            'mapped_proof': (led.get('synthesis') or {}).get('mapped_proof'),
            'area_um2': val('mapped_area'), 'setup_ws_ns': val('setup_ws'), 'logic_depth': val('logic_depth'),
            'cells': val('cell_count'), 'fingerprint': (ev or {}).get('structure', {}).get('fingerprint'),
            'error': led.get('error')}


def evaluate(root):
    root = Path(root).resolve()
    proto = read(root / 'protocol.json')
    ws = root / 'workspace'
    ir, vocab = proto['ir'], proto['vocabulary']
    pre = alu.preamble_of(ws / 'frozen/reference.v')
    index = read(root / 'index.json') if (root / 'index.json').is_file() else {}
    golden = proto['golden_candidate']
    if golden not in {v['candidate'] for v in index.values()}:
        r = pipeline.evaluate(ws, golden, 'synthesis')
        index['__golden__'] = {'candidate': golden, 'config': None, **_row(ws, golden, Path(r['run']))}
        write(root / 'index.json', index)
    for cfg in alu.space(ir, vocab):
        key = alu.config_id(cfg)
        if key in index:
            continue
        path = root / 'rendered' / (key + '.v')
        path.parent.mkdir(exist_ok=True)
        path.write_text(alu.render(ir, cfg, pre))
        rec = workspace.add(ws, path, action={'op': 'alu_config', 'config': cfg, 'vocabulary': proto['vocabulary_sha256']})
        r = pipeline.evaluate(ws, rec['id'], 'synthesis')
        index[key] = {'config': cfg, 'rtl_sha256': digest(path), **_row(ws, rec['id'], Path(r['run']))}
        write(root / 'index.json', index)
        print(key, index[key]['status'], index[key]['area_um2'], index[key]['setup_ws_ns'], flush=True)
    return {'evaluated': len(index)}


def route(root, keys):
    root = Path(root).resolve()
    index = read(root / 'index.json')
    for key in keys:
        e = index[key]
        if e.get('routed'):
            continue
        r = pipeline.evaluate(root / 'workspace', e['candidate'], 'finish')
        run = Path(r['run'])
        risk = read(run / 'risk.json') if (run / 'risk.json').is_file() else {}
        e['routed'] = {'run': run.name, 'status': r['status'], 'acceptance': r['acceptance']['status'],
                       'area_um2': risk.get('area_um2'), 'setup_ws_ns': risk.get('setup_ws'),
                       'repair_net_edits': (risk.get('repair') or {}).get('net_edits'),
                       'parasitics': (risk.get('parasitics') or {}).get('provenance'), 'error': r.get('error')}
        write(root / 'index.json', index)
        print(key, e['routed'], flush=True)


def report(root, budgets, trials, seed):
    root = Path(root).resolve()
    proto, index = read(root / 'protocol.json'), read(root / 'index.json')
    table = {k: v for k, v in index.items() if k != '__golden__'}
    golden = index['__golden__']
    origin = alu.config_id(proto['ir']['origin_config'])
    rule = {'min_rel_area_improvement': 0.005, 'setup_ws_min_ns': 0.0}
    neighbours = baselines.neighbours(list(table))
    out = {'schema': 's2g.alu-landscape-report.v1', 'generated_at': stamp(), 'model_calls': 0,
           'vocabulary_sha256': proto['vocabulary_sha256'], 'ir_sha256': proto['ir_sha256'],
           'golden': golden, 'origin_config': origin, 'origin': table.get(origin), 'rule': rule,
           'summary': baselines.summary(table, golden),
           'pareto_area_slack': baselines.pareto(table),
           'reachability_from_golden': baselines.reachability(table, neighbours, golden, origin, rule),
           'baselines': {str(n): baselines.simulate(table, neighbours, golden, origin, rule, n, trials, seed)
                         for n in budgets}}
    write(root / 'landscape.json', out)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--root', required=True)
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('prepare')
    for n in ('inputs', 'ir', 'vocab', 'context'):
        p.add_argument('--' + n, required=True)
    p.add_argument('--route-quota', type=int, default=12)
    sub.add_parser('evaluate')
    p = sub.add_parser('route'); p.add_argument('keys', nargs='+')
    p = sub.add_parser('report')
    p.add_argument('--budgets', type=int, nargs='+', default=[4, 8, 16])
    p.add_argument('--trials', type=int, default=10000); p.add_argument('--seed', type=int, default=20261013)
    a = ap.parse_args()
    if a.cmd == 'prepare':
        r = prepare(a.root, a.inputs, a.ir, a.vocab, a.context, a.route_quota)
    elif a.cmd == 'evaluate':
        r = evaluate(a.root)
    elif a.cmd == 'route':
        r = route(a.root, a.keys)
    else:
        r = report(a.root, a.budgets, a.trials, a.seed)
    print(json.dumps(r, indent=2)[:4000])


if __name__ == '__main__':
    main()
