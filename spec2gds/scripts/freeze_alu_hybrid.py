#!/usr/bin/env python3
"""Freeze offline ALU controls and future A2/A3 typed/hybrid policies. No model calls.

This registers a new development comparison after both landscapes were inspected.
It never overwrites historical policies or claims a new untouched hold-out set.
"""
import argparse
import copy
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from s2g import alu, baselines
from s2g.common import digest, files_manifest, identity, read, require, stamp, tree_files, verify_manifest, write


def build_policy(ir, vocabulary, budget, mode):
    p = read(BASE / 'configs/loop-policy-v1.json')
    p['policy_version'] = f'alu-{mode}-routed-pool-v1-n{budget}'
    p['note'] = 'New DEV comparison; existing policies/results remain unchanged. Not authorization for paid calls.'
    p['generation'] = {'mode': mode, 'ir': ir, 'vocabulary': vocabulary, 'max_edits_per_proposal': 4,
                       'parent_policy': 'validated_typed_pool'}
    p['budget'].update(candidate_submissions=budget, model_requests=2 * budget,
                       query_calls=budget, physical_evaluations=budget + 1, wall_s=21600)
    p['feedback']['generation_chars'] = 24000
    p['physical'] = {'baseline': True, 'final': True, 'search_schedule': 'first_eligible', 'search_quota': budget}
    p['final_selection'] = {'kind': 'routed_pool', 'min_rel_area_improvement': .005}
    p['stop']['max_consecutive_no_change'] = budget + 1
    p['provider']['model_env'] = None
    p['experiment'] = {
        'primary_metric': 'confirmed_routed_area_gain_rel (shared preregistered routed-pool selector)',
        'goal_text': 'After search ends, choose the smallest accepted routed implementation from the whole '
                     'validated pool or retain the original, at >=0.5% routed area improvement. Negative '
                     'synthesis slack does not exclude final pool selection. Search promotion remains the '
                     'shared synthesis rule. Only A3 sees physical evidence during search. Missing physical '
                     'coverage or repeat confirmation makes the primary outcome unknown. Repair edits/time, '
                     'timing headroom, candidate validity and tool cost are secondary outcomes.'}
    return p


def freeze(root, trials=10000, seed_start=100000):
    root = Path(root).resolve()
    require(not root.exists(), 'registration root must be new')
    require(trials > 0, 'trials must be positive')
    vpath = BASE / 'configs/alu-actions-v1.json'
    vocab = alu.load_vocabulary(vpath)
    seeds = list(range(seed_start, seed_start + trials))
    designs = {}
    sources = [vpath, Path(__file__), BASE / 'configs/loop-policy-v1.json'] + tree_files(BASE / 's2g')
    for name, example in (('riscv32', 'riscv_alu32'), ('kleine32', 'kleine_alu32')):
        landscape = BASE / 'runs' / ('alu-landscape-' + name + '-v1')
        ir_path = BASE / 'examples' / example / 'alu_ir.json'
        ir = alu.validate_ir(read(ir_path))
        proto, index = read(landscape / 'protocol.json'), read(landscape / 'index.json')
        require(proto['ir_sha256'] == digest(ir_path) and proto['vocabulary_sha256'] == digest(vpath),
                'landscape IR or vocabulary differs')
        table = {k: e for k, e in index.items() if k != '__golden__'}
        configs = alu.space(ir, vocab)
        require(set(table) == {alu.config_id(c) for c in configs} and all(baselines.valid(e) for e in table.values()),
                'landscape must contain every verified synthesis configuration')
        inputs = landscape / 'workspace/frozen'
        sources += [ir_path, landscape / 'protocol.json', landscape / 'index.json']
        sources += [inputs / n for n in ('spec.json', 'reference.v', 'tb.v', 'constraints.sdc', 'context.json', 'review.json')]
        ctx = copy.deepcopy(read(inputs / 'context.json'))
        ctx.update(candidate_budget=9, tool_run_budget=20)
        write(root / name / 'context-n8.json', ctx)
        ctx.update(candidate_budget=5, tool_run_budget=12)
        write(root / name / 'context-n4.json', ctx)
        policies, projections = {}, {}
        for n in (4, 8):
            for mode in ('typed', 'hybrid'):
                path = root / name / f'policy-{mode}-n{n}.json'
                p = build_policy(ir, vocab, n, mode)
                write(path, p)
                policies[f'{mode}-n{n}'] = {'path': str(path), 'sha256': digest(path)}
            print(f'{name}: projecting {trials} control seeds at {n} proposals', flush=True)
            projections[str(n)] = baselines.simulate_controller(
                table, configs, index['__golden__'], ir['origin_config'], p['objective'], n, seeds)
        write(root / name / 'synthesis-projections.json', projections)
        designs[name] = {'split': 'development (both landscapes already inspected)',
                         'scope': 'combinational golden; kleine registered-source derivation is simulation-qualified only',
                         'inputs': str(inputs), 'golden': '__golden__', 'policies': policies,
                         'projection_sha256': digest(root / name / 'synthesis-projections.json')}
    man = {'schema': 's2g.alu-hybrid-registration.v1', 'created_at': stamp(), 'paid_calls': 0,
           'launch_status': 'OFFLINE_REGISTERED; no paid experiment launched or authorized by this file',
           'designs': designs, 'inputs_and_code': files_manifest(sources),
           'outputs': files_manifest(tree_files(root)),
           'planned_model_arms': ['A2', 'A3'], 'planned_repeats': [0, 1, 2], 'proposal_budgets': [4, 8],
           'control_seeds': seeds, 'control_strategies': ['random_single_action_walk', 'random_independent_sampling'],
           'comparison': {
               'primary': 'A2 vs A3 within the SAME generation mode and budget; identical execution and final selector',
               'typed_controls': 'single-action walk (1 edit/proposal), independent sampling (1..4 edits/proposal, golden anchor)',
               'hybrid_scope': 'secondary unrestricted RTL exploration; random typed controls cover a narrower generator class',
               'counts': 'all failed/duplicate/stopped submissions count; queries share the 2N request cap; no transport retries',
               'normalization': 'raw golden remains baseline; no free rendered origin; zero-edit typed proposals are rejected',
               'primary_metrics': ['per-run confirmed routed area gain', 'mean gain at N', 'probability of positive gain'],
               'secondary_metrics': ['synthesis-region hit rate', 'first gain proposal', 'primitive edit count',
                                     'repair net edits/time', 'routed timing', 'wall time', 'verification failure rate'],
               'region': 'observed feasible synthesis area within 0.5% of table minimum; never claimed as routed optimum',
               'missing': 'unknown results remain null and counted; no averaging only successful runs without coverage counts',
               'physical_controls': 'must run the same registered real-tool control loops; nine curated routes are insufficient',
               'generalization': 'a new lineage-separated design is required for a future untouched hold-out evaluation'}}
    man['registration_sha256'] = identity(man)
    write(root / 'registration.json', man)
    print('Registered', man['registration_sha256'], flush=True)
    return man


def verify(root):
    man = read(Path(root) / 'registration.json')
    require(identity({k: v for k, v in man.items() if k != 'registration_sha256'}) == man['registration_sha256'],
            'registration manifest changed')
    verify_manifest(man['inputs_and_code'])
    verify_manifest(man['outputs'])
    return {'status': 'PASS', 'registration_sha256': man['registration_sha256'], 'model_calls': 0}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', required=True)
    p.add_argument('--trials', type=int, default=10000)
    p.add_argument('--seed-start', type=int, default=100000)
    p.add_argument('--verify', action='store_true', help='verify an existing registration without launching any run')
    a = p.parse_args()
    if a.verify:
        print(verify(a.root))
    else:
        freeze(a.root, a.trials, a.seed_start)


if __name__ == '__main__':
    main()
