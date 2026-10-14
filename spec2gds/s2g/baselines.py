"""Non-LLM search baselines over a measured configuration table (no EDA calls).

Each table entry is one evaluated configuration keyed `share-...<shifter>.<style>.<select>`.
Searches start from the golden RTL placed at its structural reading (`origin`), take one
typed action per walk proposal (one dimension changed); independent sampling may change
several dimensions in a proposal. All searches promote with the same rule as the loop:
all correctness gates, VALID area and slack, slack >= floor, changed fingerprint, and area
improvement >= the threshold against the incumbent. Every proposal spends budget.
"""
import random
import statistics
from collections import deque
from .common import identity


def valid(e):
    return (e or {}).get('status') == 'COMPLETE' and e.get('functional') == 'PASS' and e.get('mapped_proof') == 'PASS' \
        and e.get('area_um2') is not None and e.get('setup_ws_ns') is not None


def promotable(cur, cand, rule):
    return valid(cand) and cand['setup_ws_ns'] >= rule['setup_ws_min_ns'] and cand['fingerprint'] != cur['fingerprint'] \
        and (cur['area_um2'] - cand['area_um2']) / cur['area_um2'] >= rule['min_rel_area_improvement']


def neighbours(keys):
    parts = {k: k.split('.') for k in keys}
    return {k: [j for j in keys if sum(x != y for x, y in zip(parts[k], parts[j])) == 1] for k in keys}


def gain(golden, e):
    return round((golden['area_um2'] - e['area_um2']) / golden['area_um2'], 6)


def best_under_rule(table, rule):
    ok = [(k, e) for k, e in table.items() if valid(e) and e['setup_ws_ns'] >= rule['setup_ws_min_ns']]
    return min(ok, key=lambda kv: (kv[1]['area_um2'], kv[0])) if ok else (None, None)


def summary(table, golden):
    v = {k: e for k, e in table.items() if valid(e)}
    areas = [e['area_um2'] for e in v.values()]
    return {'configurations': len(table), 'valid': len(v), 'invalid': sorted(k for k in table if k not in v),
            'distinct_fingerprints': len({e['fingerprint'] for e in v.values()}),
            'collapsed_to_golden': sorted(k for k, e in v.items() if e['fingerprint'] == golden['fingerprint']),
            'area_min': min(areas) if areas else None, 'area_max': max(areas) if areas else None,
            'better_than_golden_by_threshold': sum((golden['area_um2'] - a) / golden['area_um2'] >= 0.005 for a in areas),
            'negative_slack': sorted(k for k, e in v.items() if e['setup_ws_ns'] < 0)}


def pareto(table):
    v = [(k, e) for k, e in table.items() if valid(e)]
    front = [(k, e) for k, e in v if not any(o['area_um2'] <= e['area_um2'] and o['setup_ws_ns'] >= e['setup_ws_ns']
                                             and (o['area_um2'], o['setup_ws_ns']) != (e['area_um2'], e['setup_ws_ns'])
                                             for _, o in v)]
    return [{'config': k, 'area_um2': e['area_um2'], 'setup_ws_ns': e['setup_ws_ns'], 'logic_depth': e['logic_depth']}
            for k, e in sorted(front, key=lambda kv: kv[1]['area_um2'])]


def reachability(table, nbrs, golden, origin, rule):
    """Shortest promotable single-action path from the golden RTL to the best configuration,
    and how many promotable-move local optima exist (a measure of step-wise deceptiveness)."""
    best, _ = best_under_rule(table, rule)
    dist, frontier = {}, deque()
    for n in nbrs[origin]:
        if promotable(golden, table[n], rule):
            dist[n] = 1; frontier.append(n)
    while frontier:
        k = frontier.popleft()
        for n in nbrs[k]:
            if n not in dist and promotable(table[k], table[n], rule):
                dist[n] = dist[k] + 1; frontier.append(n)
    optima = [k for k in dist if not any(promotable(table[k], table[n], rule) for n in nbrs[k])]
    return {'best': best, 'best_gain': gain(golden, table[best]) if best else None,
            'reachable_states': len(dist), 'best_reachable': best in dist, 'min_steps_to_best': dist.get(best),
            'local_optima': len(optima),
            'local_optimum_gains': sorted({gain(golden, table[k]) for k in optima}, reverse=True)[:10]}


def _walk(table, nbrs, golden, origin, rule, budget, rng):
    pos, inc, tried = origin, golden, set()
    for _ in range(budget):
        options = [n for n in nbrs[pos] if n not in tried] or nbrs[pos]
        n = rng.choice(options)
        tried.add(n)
        if promotable(inc, table[n], rule):
            pos, inc = n, table[n]
    return inc


def _sample(table, golden, rule, budget, rng):
    inc = golden
    for k in rng.sample(sorted(table), min(budget, len(table))):
        if promotable(inc, table[k], rule):
            inc = table[k]
    return inc


def _greedy(table, nbrs, golden, origin, rule, budget):
    """Deterministic coordinate sweep: evaluate neighbours in key order, take the first promotable."""
    pos, inc, used, moved = origin, golden, 0, True
    while moved and used < budget:
        moved = False
        for n in sorted(nbrs[pos]):
            if used >= budget:
                break
            used += 1
            if promotable(inc, table[n], rule):
                pos, inc, moved = n, table[n], True
                break
    return inc, used


def simulate(table, nbrs, golden, origin, rule, budget, trials, seed):
    rng = random.Random(seed)
    best, _ = best_under_rule(table, rule)
    out = {}
    for name, fn in (('random_single_action_walk', lambda: _walk(table, nbrs, golden, origin, rule, budget, rng)),
                     ('random_independent_sampling', lambda: _sample(table, golden, rule, budget, rng))):
        finals = [fn() for _ in range(trials)]
        gains = [gain(golden, f) for f in finals]
        out[name] = {'trials': trials, 'mean_gain': round(statistics.mean(gains), 6),
                     'median_gain': round(statistics.median(gains), 6),
                     'p_positive_gain': round(sum(g > 0 for g in gains) / trials, 4),
                     'p_best': round(sum(f is table.get(best) for f in finals) / trials, 4) if best else None,
                     'quantiles_10_50_90': [round(sorted(gains)[int(q * (trials - 1))], 6) for q in (0.1, 0.5, 0.9)]}
    inc, used = _greedy(table, nbrs, golden, origin, rule, budget)
    out['greedy_coordinate_sweep'] = {'gain': gain(golden, inc), 'evaluations_used': used, 'reached_best': inc is table.get(best)}
    return out


def simulate_controller(table, configs, golden, origin_config, rule, budget, seeds):
    """v2 controls, matching generation.RandomTransport's stateless draws and ordering.

    Unlike the historical sampler, a zero-edit draw of the golden annotation is
    excluded. Compound independent draws use 1..4 edits; the walk uses exactly one.
    These are projections over measured synthesis evidence, not new physical runs.
    """
    from .alu import config_id
    origin = config_id(origin_config)
    keys = [config_id(c) for c in configs]
    nearby = {k: [j for j, c in zip(keys, configs) if sum(c[d] != cfg[d] for d in cfg) == 1]
              for k, cfg in zip(keys, configs)}
    independent = [k for k in keys if k != origin]
    best, e = best_under_rule(table, rule)
    region = {k for k, c in table.items() if valid(c) and c['setup_ws_ns'] >= rule['setup_ws_min_ns']
              and c['area_um2'] <= e['area_um2'] * 1.005} if e else set()
    output = {}
    for strategy in ('random_single_action_walk', 'random_independent_sampling'):
        gains, hits, edits, known_route = [], 0, [], 0
        for seed in seeds:
            pos, inc, winner, seen, n_edits = origin, golden, None, set(), 0
            for proposal in range(1, budget + 1):
                options = nearby[pos] if strategy == 'random_single_action_walk' else independent
                fresh = [k for k in options if k not in seen]
                rng = random.Random(identity([seed, proposal]))
                k = rng.choice(fresh or options)
                parent = pos if strategy == 'random_single_action_walk' else origin
                n_edits += sum(x != y for x, y in zip(k.split('.'), parent.split('.')))
                seen.add(k)
                if promotable(inc, table[k], rule):
                    inc, winner = table[k], k
                    if strategy == 'random_single_action_walk':
                        pos = k
            gains.append(gain(golden, inc))
            hits += winner in region
            edits.append(n_edits)
            known_route += all(table[k].get('routed') for k in seen)
        output[strategy] = {'trials': len(seeds), 'mean_gain': round(statistics.mean(gains), 6),
                            'p_positive_gain': round(sum(g > 0 for g in gains) / len(gains), 6),
                            'p_best_observed_synthesis_region': round(hits / len(seeds), 6),
                            'mean_primitive_edits': round(statistics.mean(edits), 3),
                            'trajectories_with_all_candidate_routes_in_table': known_route,
                            'confirmed_routed_gain': None,
                            'routed_gain_reason': 'curated routed subset cannot support full random-control outcomes'}
    return {'controls': output, 'best_observed_synthesis_region': sorted(region), 'best_config': best,
            'scope': 'synthesis projections using measured table; no new EDA or model requests'}
