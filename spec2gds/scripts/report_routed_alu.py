#!/usr/bin/env python3
"""Audit a completed routed landscape and replay fixed random seeds. No EDA/model calls.

Search parents still follow synthesis promotion; final scoring uses the preregistered
routed-pool selector. Fixed-seed scores are exact table replays, conditional on measured
repeatability, not 10,000 executed controller confirmations or a population expectation.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
import json
import math
import random
from pathlib import Path
import statistics
import sys

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from s2g import alu, baselines
from s2g.common import digest, files_manifest, identity, read, require, stamp, tree_files, verify_manifest, write
import route_alu_landscape as batch


def accepted(row):
    r = row['routed']
    return row['functional'] == row['mapped_proof'] == 'PASS' and r['status'] == 'COMPLETE' \
        and r['acceptance'] == 'ACCEPTED' and r['parasitics'] == 'detailed_route_extraction' \
        and r['area_um2'] is not None and r['area_um2'] > 0


def routed_winner(table, golden, visited, threshold=.005):
    """Smallest qualified changed implementation; equal-area ties use first submission."""
    require(accepted(golden), 'raw golden physical result is not qualified')
    if any(not table[k]['routed']['outcome_known'] for k in visited):
        return None, None
    selected, area0, area = None, golden['routed']['area_um2'], golden['routed']['area_um2']
    for k in visited:
        c = table[k]
        if accepted(c) and c['fingerprint'] != golden['fingerprint'] and c['routed']['area_um2'] < area \
                and (area0 - c['routed']['area_um2']) / area0 >= threshold:
            selected, area = k, c['routed']['area_um2']
    return selected, (area0 - area) / area0


def replay(synthesis, physical, golden_syn, golden_phys, configs, origin_config, rule, budget, seeds):
    keys = [alu.config_id(c) for c in configs]
    origin = alu.config_id(origin_config)
    nearby = {k: [j for j, c in zip(keys, configs) if sum(c[d] != cfg[d] for d in cfg) == 1]
              for k, cfg in zip(keys, configs)}
    independent = [k for k in keys if k != origin]
    feasible = [k for k in keys if accepted(physical[k])]
    best_area = min((physical[k]['routed']['area_um2'] for k in feasible), default=None)
    optimum = {k for k in feasible if physical[k]['routed']['area_um2'] == best_area}
    results = {}
    for strategy in ('random_single_action_walk', 'random_independent_sampling'):
        gains, winners, best_hits, visited_best, first_gain, primitive = [], [], [], [], [], []
        for seed in seeds:
            pos, inc, seen, visited, edits = origin, golden_syn, set(), [], 0
            first = None
            for n in range(1, budget + 1):
                options = nearby[pos] if strategy == 'random_single_action_walk' else independent
                fresh = [k for k in options if k not in seen]
                k = random.Random(identity([seed, n])).choice(fresh or options)
                parent = pos if strategy == 'random_single_action_walk' else origin
                edits += sum(x != y for x, y in zip(k.split('.'), parent.split('.')))
                visited.append(k); seen.add(k)
                if baselines.promotable(inc, synthesis[k], rule):
                    inc = synthesis[k]
                    if strategy == 'random_single_action_walk':
                        pos = k
                c = physical[k]
                if first is None and accepted(c) and c['fingerprint'] != golden_phys['fingerprint'] \
                        and (golden_phys['routed']['area_um2'] - c['routed']['area_um2']) / golden_phys['routed']['area_um2'] >= .005:
                    first = n
            selected, gain = routed_winner(physical, golden_phys, visited)
            gains.append(None if gain is None else round(gain, 9))
            winners.append(selected)
            best_hits.append(gain is not None and selected in optimum)
            visited_best.append(bool(set(visited) & optimum))
            first_gain.append(first); primitive.append(edits)
        known = [g for g in gains if g is not None]
        results[strategy] = {'seeds': list(seeds), 'routed_gain_rel': gains, 'winners': winners,
                            'summary': {'trials': len(seeds), 'known': len(known), 'unknown': len(seeds) - len(known),
                            'mean_gain_rel': statistics.mean(known) if known else None,
                            'median_gain_rel': statistics.median(known) if known else None,
                            'p_positive_gain_all_seeds': sum(g is not None and g > 0 for g in gains) / len(seeds),
                            'p_selected_routed_optimum_all_seeds': sum(best_hits) / len(seeds),
                            'p_visited_routed_optimum_all_seeds': sum(visited_best) / len(seeds),
                            'mean_primitive_edits': statistics.mean(primitive),
                            'first_routed_gain_proposal': {str(n): first_gain.count(n) for n in range(1, budget + 1)},
                            'first_routed_gain_not_observed': first_gain.count(None),
                            'scope': 'known-only mean explicitly reports coverage; unknowns are not zero gains'}}
    return results


def landscape_summary(syn, physical, golden, golden_syn=None):
    valid = {k: e for k, e in physical.items() if accepted(e)}
    unknown = [k for k, e in physical.items() if not e['routed']['outcome_known']]
    known_rejected = [k for k, e in physical.items() if e['routed']['outcome_known'] and not accepted(e)]
    synth_passing = {k for k, e in syn.items() if baselines.valid(e) and e['setup_ws_ns'] >= 0}
    synth_reject_routed_accept = [k for k in valid if k not in synth_passing]
    synth_accept_routed_reject = [k for k in known_rejected if k in synth_passing]
    best_area = min((e['routed']['area_um2'] for e in valid.values()), default=None)
    opt = [k for k, e in valid.items() if e['routed']['area_um2'] == best_area]
    syn_best, _ = baselines.best_under_rule(syn, {'setup_ws_min_ns': 0.0})
    beneficial = {k for k, e in valid.items() if e['fingerprint'] != golden['fingerprint'] and
                  (golden['routed']['area_um2'] - e['routed']['area_um2']) / golden['routed']['area_um2'] >= .005}
    promotable = {k for k, e in syn.items() if baselines.promotable(golden_syn, e,
                 {'setup_ws_min_ns': 0., 'min_rel_area_improvement': .005})} if golden_syn else set()
    # Area, timing margin and stable edit counts are kept separate; this is an
    # exploratory Pareto table, not a change to the registered area objective.
    front = []
    for k, e in valid.items():
        p = e['routed']
        if p.get('setup_ws_ns') is None or p.get('repair_net_edits') is None:
            continue
        triple = (p['area_um2'], -p['setup_ws_ns'], p['repair_net_edits'])
        dominated = False
        for j, other in valid.items():
            q = other['routed']
            if j == k or q.get('setup_ws_ns') is None or q.get('repair_net_edits') is None:
                continue
            score = (q['area_um2'], -q['setup_ws_ns'], q['repair_net_edits'])
            if all(a <= b for a, b in zip(score, triple)) and score != triple:
                dominated = True; break
        if not dominated:
            front.append({'config': k, 'area_um2': p['area_um2'], 'setup_ws_ns': p['setup_ws_ns'],
                          'repair_net_edits': p['repair_net_edits']})
    repair = [e['routed']['repair_net_edits'] for e in valid.values() if e['routed'].get('repair_net_edits') is not None]
    return {'configurations': len(physical), 'accepted': len(valid), 'known_rejected': len(known_rejected),
            'unknown': unknown, 'known_rejected_configs': known_rejected,
            'golden_area_um2': golden['routed']['area_um2'], 'best_measured_area_um2': best_area,
            'best_measured_gain_rel': (golden['routed']['area_um2'] - best_area) / golden['routed']['area_um2'] if best_area else None,
            'optimum_configs': opt, 'complete_routed_optimum_known': not unknown,
            'optimum_scope': 'finite frozen library, one corner, seed 17; not arbitrary RTL or foundry signoff',
            'synthesis_passing': len(synth_passing),
            'synthesis_reject_routed_accept': synth_reject_routed_accept,
            'synthesis_accept_routed_reject': synth_accept_routed_reject,
            'synthesis_reject_routed_beneficial': sorted(beneficial - synth_passing),
            'synthesis_promotable_vs_raw_golden': len(promotable) if golden_syn else None,
            'synthesis_promotable_routed_reject': sorted(promotable & set(known_rejected)) if golden_syn else None,
            'synthesis_passing_routed_unknown': sorted(synth_passing & set(unknown)),
            'synthesis_best_config': syn_best,
            'synthesis_best_routed': physical[syn_best]['routed'] if syn_best else None,
            'optimum_synthesis_slack_ns': {k: syn[k]['setup_ws_ns'] for k in opt},
            'optimum_routed_metrics': {k: physical[k]['routed'] for k in opt},
            'routed_area_slack_edits_pareto': sorted(front, key=lambda e: (e['area_um2'], e['config'])),
            'repair_edit_range': [min(repair), max(repair)] if repair else None}


def build_report(root, registration):
    root = Path(root).resolve()
    man = batch.verify(root)
    reg = read(registration)
    require(identity({k: v for k, v in reg.items() if k != 'registration_sha256'}) == reg['registration_sha256'],
            'random seed registration changed')
    descriptors = [j for j in read(root / 'jobs.json') if j['phase'] == 'full']
    rows = read(root / 'results.json')
    require(all(j['id'] in rows for j in descriptors), 'full routed table is incomplete')
    # Re-read hash-verified artifacts; results.json itself is only a mutable checkpoint.
    with ThreadPoolExecutor(max_workers=8) as executor:
        audited = list(executor.map(lambda j: batch.recover_job(j, rows[j['id']]), descriptors))
    rows = {j['id']: r for j, r in zip(descriptors, audited)}
    summary, projections, cache = {}, {}, {}
    for design, info in man['designs'].items():
        table = {j['key']: rows[j['id']] for j in descriptors if j['design'] == design}
        golden, physical = table[batch.GOLD_KEY], {k: r for k, r in table.items() if k != batch.GOLD_KEY}
        old_index = read(Path(info['source_landscape']) / 'index.json')
        syn = {k: e for k, e in old_index.items() if k != batch.GOLD_KEY}
        for key, row in table.items():
            require(all(row[k] == old_index[key][k] for k in ('area_um2', 'setup_ws_ns', 'fingerprint')),
                    'fresh synthesis differs from registered table: ' + key)
        summary[design] = landscape_summary(syn, physical, golden, old_index[batch.GOLD_KEY])
        projections[design] = {}
        for n in reg['proposal_budgets']:
            projections[design][str(n)] = replay(syn, physical, old_index[batch.GOLD_KEY], golden,
                alu.space(info['ir'], info['vocabulary']), info['origin_config'],
                {'min_rel_area_improvement': .005, 'setup_ws_min_ns': 0.0}, n, reg['control_seeds'])
        ws = Path(golden['workspace'])
        ctx = read(ws / 'frozen/context.json')
        ctx = {k: v for k, v in ctx.items() if k not in batch.BUDGET_KEYS}
        frozen = read(ws / 'freeze.json')
        assets = {('workspace/' + str(Path(p).relative_to(ws))) if Path(p).is_relative_to(ws) else p: sha
                  for p, sha in frozen['files'].items() if p != str(ws / 'frozen/context.json')}
        contract = {'context_without_budgets': ctx, 'frozen_assets': assets, 'runtime_code': man['runtime_code']}
        contract_key = identity(contract)
        cache[design] = {'contract_key': contract_key, 'contract': contract,
                         'entries': {e['rtl_sha256']: {'config_key': k, 'row': e} for k, e in table.items()},
                         'scope': 'verified measured artifact references; controller cache consumption is not implemented'}
    outdir = root / 'analysis'
    require(not outdir.exists(), 'analysis output must be new; preserve earlier analysis snapshots')
    outdir.mkdir()
    write(outdir / 'routed-projections.json', projections)
    write(outdir / 'cache-index.json', cache)
    write(outdir / 'landscape-summary.json', summary)
    with (outdir / 'landscape.csv').open('w') as f:
        columns = ['design', 'config', 'rtl_sha256', 'synth_area_um2', 'synth_setup_ws_ns', 'routed_status',
                   'acceptance', 'routed_area_um2', 'routed_setup_ws_ns', 'repair_net_edits', 'outcome_known']
        writer = csv.DictWriter(f, fieldnames=columns); writer.writeheader()
        for design in man['designs']:
            for j in descriptors:
                if j['design'] != design:
                    continue
                e = rows[j['id']]; p = e['routed']
                writer.writerow(dict(zip(columns, [design, j['key'], e['rtl_sha256'], e['area_um2'], e['setup_ws_ns'],
                    p['status'], p['acceptance'], p['area_um2'], p['setup_ws_ns'], p['repair_net_edits'], p['outcome_known']])))
    report = {'schema': 's2g.routed-alu-analysis.v1', 'at': stamp(), 'model_calls': 0,
              'source_manifest_sha256': man['manifest_sha256'], 'seed_registration_sha256': reg['registration_sha256'],
              'summary': summary, 'outputs': files_manifest(tree_files(outdir)),
              'analysis_code': files_manifest([Path(__file__)]),
              'scope': 'fixed-seed table replays under empirically checked repeatability; no 10,000 tool/controller '
                       'confirmation runs; search parents are indirectly informed by the common synthesis rule'}
    report['report_sha256'] = identity(report)
    write(outdir / 'report.json', report)
    return report


def percentile(root, design, budget, strategy, gain):
    require(math.isfinite(gain) and -1 <= gain <= 1, 'gain must be a finite relative fraction')
    data = read(Path(root) / 'analysis/routed-projections.json')[design][str(budget)][strategy]
    # The experiment reporter exports routed gains at six decimal places. Compare
    # at that same precision so a cached identical winner receives a tie, not a
    # spurious rank difference from the reporter's rounding.
    known = [round(x, 6) for x in data['routed_gain_rel'] if x is not None]
    gain = round(gain, 6)
    require(known, 'no known random gains')
    lower, equal = sum(g < gain for g in known), sum(g == gain for g in known)
    return {'gain': gain, 'known_random_runs': len(known), 'unknown': len(data['routed_gain_rel']) - len(known),
            'below_percent': 100 * lower / len(known), 'equal_percent': 100 * equal / len(known),
            'midrank_percentile': 100 * (lower + .5 * equal) / len(known),
            'comparison_precision_decimals': 6,
            'definition': 'midrank among known fixed-seed outcomes; null coverage explicitly reported'}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=('build', 'percentile')); p.add_argument('--root', required=True)
    p.add_argument('--registration', default=str(BASE / 'runs/hybrid-alu-registration-v2/registration.json'))
    p.add_argument('--design', choices=('riscv32', 'kleine32')); p.add_argument('--budget', type=int, choices=(4, 8))
    p.add_argument('--strategy', choices=('random_single_action_walk', 'random_independent_sampling'))
    p.add_argument('--gain', type=float)
    a = p.parse_args()
    if a.command == 'build':
        x = build_report(a.root, a.registration)
        print(json.dumps({'report_sha256': x['report_sha256'], 'summary': x['summary']}, indent=2))
    else:
        require(a.design and a.budget and a.strategy and a.gain is not None, 'percentile needs design, budget, strategy and gain')
        print(json.dumps(percentile(a.root, a.design, a.budget, a.strategy, a.gain), indent=2))


if __name__ == '__main__':
    main()
