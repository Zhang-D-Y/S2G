"""Stress-contract pilot: sample selection and promotable definition."""
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import stress_alu_landscape as S  # noqa: E402


def row(area, ws, fp, acc='ACCEPTED', routed=None):
    return {'functional': 'PASS', 'mapped_proof': 'PASS', 'area_um2': area, 'setup_ws_ns': ws, 'fingerprint': fp,
            'routed': {'acceptance': acc, 'area_um2': routed if routed is not None else area * 1.25}}


class StressPilot(unittest.TestCase):
    def table(self):
        t = {S.R.GOLD_KEY: row(100, 0.3, 'g')}
        for i in range(30):
            t[f'p{i:02d}'] = row(90 - i * 0.1, 0.1, f'p{i}')        # promotable
        for i in range(10):
            t[f'n{i:02d}'] = row(80, -0.2, f'n{i}', routed=70 + i)  # negative synthesis slack
        t['same'] = row(99.9, 0.1, 'g2')                             # below the 0.5% threshold
        return t

    def test_promotable_matches_the_loop_rule(self):
        t = self.table()
        g = t[S.R.GOLD_KEY]
        self.assertTrue(S.promotable(t['p00'], g))
        self.assertFalse(S.promotable(t['n00'], g))
        self.assertFalse(S.promotable(t['same'], g))
        self.assertFalse(S.promotable(row(90, 0.1, 'g'), g))  # collapsed to the golden fingerprint

    def test_sample_is_seeded_unique_and_covers_the_named_roles(self):
        spec = {'promotable_random': 16, 'negative_synthesis_slack_random': 5}
        a = S.choose_sample(self.table(), spec, random.Random(7))
        b = S.choose_sample(self.table(), spec, random.Random(7))
        self.assertEqual(a, b)
        keys, roles, named = a
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(named['routed_optimum_C0'], 'n00')          # smallest routed area, synthesis slack < 0
        self.assertEqual(named['synthesis_rule_best_C0'], 'p29')     # smallest promotable synthesis area
        self.assertEqual(len(keys), 3 + 16 + 5)                      # named roles are excluded from the random pools
        t = self.table(); t['n00'] = row(80, -0.2, 'n0', routed=200)
        t['p29'] = row(87.1, 0.1, 'p29', routed=10)                  # rule best is also the routed optimum
        keys, roles, _ = S.choose_sample(t, spec, random.Random(7))
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(roles['p29'], 'routed_optimum_C0+synthesis_rule_best_C0')


if __name__ == '__main__':
    unittest.main()
