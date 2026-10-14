#!/usr/bin/env python3
"""Real-tool hybrid smoke, scripted proposals only. Golden + one candidate to GDS.

The deliberately chosen candidate is known to have negative synthesis slack and
positive routed slack. This is a plumbing test, never a model-performance result.
"""
import argparse
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from s2g import alu, controller, provider, workspace
from s2g.common import digest, files_manifest, read, require, write
from freeze_alu_hybrid import build_policy


def smoke(root):
    root = Path(root).resolve()
    require(not root.exists(), 'smoke root must be new')
    inputs = BASE / 'runs/alu-landscape-riscv32-v1/workspace/frozen'
    ir = read(BASE / 'examples/riscv_alu32/alu_ir.json')
    vocab = read(BASE / 'configs/alu-actions-v1.json')
    target = dict(ir['origin_config'], addsub_share=list(alu.ADDER_OPS), shifter='reverse_right',
                  shift_style='native', select='and_or')
    p = build_policy(ir, vocab, 3, 'hybrid')
    p['policy_version'] = 'hybrid-scripted-smoke-v1'
    ctx = read(inputs / 'context.json'); ctx.update(candidate_budget=4, tool_run_budget=10)
    write(root / 'context.json', ctx)
    write(root / 'policy.json', p)
    ws = root / 'workspace'
    workspace.init(ws, inputs / 'spec.json', inputs / 'reference.v', inputs / 'tb.v', inputs / 'constraints.sdc',
                   root / 'context.json', inputs / 'review.json')
    # First is typed; second is the same implementation through the free-form interface.
    # It must be charged as a duplicate and never gain a second tool evaluation.
    free = alu.render(ir, target, alu.preamble_of(inputs / 'reference.v'))
    edits = [{'op': 'set', 'dimension': d, 'value': target[d]} for d in sorted(target)
             if target[d] != ir['origin_config'][d]]
    replies = [
        {'action': 'alu', 'parent': 'c0000', 'edits': edits,
         'hypothesis': 'Scripted negative-synthesis-slack routed-feasible fixture'},
        {'action': 'submit', 'rtl': free, 'hypothesis': 'Same structure via free-form interface; duplicate fixture'},
        {'action': 'alu', 'parent': 'c0000', 'edits': [
            {'op': 'set', 'dimension': 'addsub_share', 'value': ['ADD', 'SUB', 'MUL']}],
         'hypothesis': 'Invalid action must be charged and rejected without tools'}]
    import json
    write(root / 'responses.json', {'responses': [{'content': json.dumps(r)} for r in replies]})
    transport = provider.Scripted(read(root / 'responses.json')['responses'])
    controller.init(ws, 'main', 'A3', root / 'policy.json', transport)
    loop = controller.Loop(ws, 'main', transport)
    print('Running baseline and hybrid candidate; no model calls', flush=True)
    st = loop.run()
    summary = read(ws / 'loops/main/summary.json')
    expected = ['REJECTED_GATE', 'DUPLICATE', 'PROPOSAL_FAILED']
    success = st['state'] == 'COMPLETE' and st['final']['label'] == 'ACCEPTED_IMPLEMENTATION' \
        and [a['outcome'] for a in st['attempts']] == expected and len(st['candidates']) == 2 \
        and st['counters']['physical_evaluations'] == 2 and not st['final']['unmet_requirements']
    report = {'schema': 's2g.hybrid-smoke.v1', 'status': 'PASS' if success else 'FAIL', 'model_calls': 0,
              'fixture': alu.config_id(target), 'expected_outcomes': expected,
              'summary_sha256': digest(ws / 'loops/main/summary.json'), 'summary': summary,
              'inputs': files_manifest([root / 'policy.json', root / 'responses.json', root / 'context.json']),
              'scope': 'scripted fixture selected using an inspected landscape; validates plumbing only'}
    write(root / 'SMOKE.json', report)
    print('Smoke', report['status'], st['final']['label'], flush=True)
    return 0 if success else 1


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__); p.add_argument('--root', required=True)
    raise SystemExit(smoke(p.parse_args().root))
