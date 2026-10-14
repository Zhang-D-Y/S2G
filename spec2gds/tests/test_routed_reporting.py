"""Fixed-seed routed scoring regressions on explicit toy metrics, not PPA data."""
import copy
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from s2g import alu, baselines, generation
from s2g.common import GateError, read, write

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / 'scripts'))
spec = importlib.util.spec_from_file_location('routed_report', BASE / 'scripts/report_routed_alu.py')
report = importlib.util.module_from_spec(spec); spec.loader.exec_module(report)
VOCAB = read(BASE / 'configs/alu-actions-v1.json')
VOCAB = copy.deepcopy(VOCAB)
VOCAB['dimensions']['shifter']['values'] = ['separate', 'reverse_right']
VOCAB['dimensions']['shift_style']['values'] = ['native']
VOCAB['dimensions']['select']['values'] = ['case', 'and_or']
IR = {'schema': alu.IR_SCHEMA, 'top': 'm', 'width': 4, 'op_ports': [{'name': 'op', 'width': 1}],
      'a': 'a', 'b': 'b', 'y': 'y', 'default': 'PASS_A', 'ops': [{'op': 'SLL', 'codes': [1]}],
      'shift_amount': {'operand': 'b', 'bits': 2}}
CONFIGS = alu.space(IR, VOCAB)
KEYS = [alu.config_id(c) for c in CONFIGS]
RULE = {'setup_ws_min_ns': 0., 'min_rel_area_improvement': .005}


def row(synth_area, routed_area, slack=1., fingerprint=None):
    return {'status': 'COMPLETE', 'functional': 'PASS', 'mapped_proof': 'PASS', 'area_um2': synth_area,
            'setup_ws_ns': slack, 'fingerprint': fingerprint or str(synth_area),
            'routed': {'status': 'COMPLETE', 'acceptance': 'ACCEPTED', 'area_um2': routed_area,
                       'parasitics': 'detailed_route_extraction', 'repair_net_edits': 40, 'outcome_known': True}}


GOLD = row(100., 200., fingerprint='raw-golden')
TABLE = {KEYS[0]: row(100., 199.), KEYS[1]: row(80., 180.),
         KEYS[2]: row(95., 120., slack=-.2), KEYS[3]: row(70., 170.)}


class Scoring(unittest.TestCase):
    def test_negative_synthesis_slack_is_not_a_final_physical_filter(self):
        k, gain = report.routed_winner(TABLE, GOLD, [KEYS[1], KEYS[2]])
        self.assertEqual(k, KEYS[2])
        self.assertEqual(gain, .4)

    def test_failed_routed_acceptance_does_not_win(self):
        table = copy.deepcopy(TABLE)
        table[KEYS[2]]['routed']['acceptance'] = 'REJECTED'
        k, gain = report.routed_winner(table, GOLD, [KEYS[1], KEYS[2]])
        self.assertEqual((k, gain), (KEYS[1], .1))

    def test_unresolved_or_timed_out_candidates_do_not_become_zero_gains(self):
        table = copy.deepcopy(TABLE)
        table[KEYS[2]]['routed']['outcome_known'] = False
        self.assertEqual(report.routed_winner(table, GOLD, [KEYS[1], KEYS[2]]), (None, None))
        s = report.landscape_summary(TABLE, table, GOLD)
        self.assertFalse(s['complete_routed_optimum_known'])
        self.assertEqual(s['unknown'], [KEYS[2]])

    def test_structural_collapse_is_filtered_by_shared_selector(self):
        table = copy.deepcopy(TABLE)
        table[KEYS[2]]['fingerprint'] = GOLD['fingerprint']
        self.assertEqual(report.routed_winner(table, GOLD, [KEYS[2]]), (None, 0.))

    def test_equal_area_tie_uses_first_submitted_candidate(self):
        table = copy.deepcopy(TABLE)
        table[KEYS[1]]['routed']['area_um2'] = 120.
        a = report.routed_winner(table, GOLD, [KEYS[1], KEYS[2]])
        b = report.routed_winner(table, GOLD, [KEYS[2], KEYS[1]])
        self.assertEqual(a[0], KEYS[1]); self.assertEqual(b[0], KEYS[2])

    def test_summary_counts_synthesis_false_rejections_and_optimum(self):
        s = report.landscape_summary(TABLE, TABLE, GOLD, GOLD)
        self.assertEqual(s['optimum_configs'], [KEYS[2]])
        self.assertTrue(s['complete_routed_optimum_known'])
        self.assertEqual(s['synthesis_reject_routed_accept'], [KEYS[2]])
        self.assertEqual(s['synthesis_reject_routed_beneficial'], [KEYS[2]])
        self.assertEqual(s['synthesis_promotable_vs_raw_golden'], 2)

    def test_replay_matches_structural_random_transport_with_shared_synthesis_parent_updates(self):
        seeds = list(range(10))
        got = report.replay(TABLE, TABLE, GOLD, GOLD, CONFIGS, CONFIGS[0], RULE, 4, seeds)
        for strategy in generation.STRATEGIES:
            expected = []
            for seed in seeds:
                parents = {'gold': {'kind': 'golden_annotation', 'config': CONFIGS[0]}}
                parents.update({k: {'kind': 'alu_rendered', 'config': c} for k, c in zip(KEYS, CONFIGS)})
                inc, pos, visited, seen = GOLD, 'gold', [], []
                for n in range(1, 5):
                    ctx = {'ir': IR, 'vocabulary': VOCAB, 'parents': parents, 'incumbent': pos,
                           'proposal': n, 'max_edits_per_proposal': 4, 'seen_configurations': seen}
                    obj = generation.random_proposal(ctx, strategy, seed)
                    cfg = parents[obj['parent']]['config']
                    for edit in obj['edits']:
                        cfg = alu.apply(IR, VOCAB, cfg, edit)
                    k = alu.config_id(cfg); visited.append(k); seen.append(k)
                    if baselines.promotable(inc, TABLE[k], RULE):
                        inc, pos = TABLE[k], k
                _, gain = report.routed_winner(TABLE, GOLD, visited)
                expected.append(round(gain, 9))
            self.assertEqual(got[strategy]['routed_gain_rel'], expected)

    def test_percentiles_match_experiment_rounding_and_expose_nulls(self):
        with tempfile.TemporaryDirectory() as d:
            write(Path(d) / 'analysis/routed-projections.json', {'riscv32': {'8': {'walk': {
                'routed_gain_rel': [0., .189265371, .4, None]}}}})
            p = report.percentile(d, 'riscv32', 8, 'walk', .189265)
            self.assertEqual(p['midrank_percentile'], 50.)
            self.assertEqual(p['unknown'], 1)
            with self.assertRaises(GateError):
                report.percentile(d, 'riscv32', 8, 'walk', float('nan'))


if __name__ == '__main__':
    unittest.main()
