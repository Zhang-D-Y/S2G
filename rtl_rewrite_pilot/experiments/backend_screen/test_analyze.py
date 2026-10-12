"""Fail-closed tests for backend evidence and repair inventory accounting."""
import tempfile
import unittest
from pathlib import Path
from experiments.backend_screen.analyze import snapshot,edits,inventory,finite

class EvidenceTests(unittest.TestCase):
    def test_missing_timing_is_unknown(self):
        s=snapshot({},'post_route','')
        self.assertFalse(s['valid_timing'])
        self.assertIsNone(s['setup_wns_ns'])
        self.assertIsNone(s['setup_violating_endpoints'])
        self.assertIsNone(s['fanout_violations'])

    def test_native_no_path_sentinel_does_not_pass(self):
        p='screen_post_route__'
        d={p+'timing__setup__ws':1e39,p+'timing__setup__tns':0,p+'timing__drv__setup_violation_count':0,p+'timing__hold__ws':1.0}
        s=snapshot(d,'post_route','')
        self.assertFalse(s['valid_timing'])
        self.assertIsNone(s['setup_tns_ns'])
        self.assertIsNone(s['setup_violating_endpoints'])

    def test_bounded_electrical_zero_is_real_and_unbounded_is_unknown(self):
        p='screen_post_route__'
        d={p+'timing__setup__ws':-1.2,p+'timing__hold__ws':0.8,p+'timing__drv__max_slew':0,p+'timing__drv__max_fanout':0}
        report='post_route max_slew_check_limit\n----------\n1.5\npost_route max_slew_check_slack\n----------\n0.25\npost_route max_fanout_check_limit\n----------\n1.00000001e30\n'
        s=snapshot(d,'post_route',report)
        self.assertTrue(s['valid_timing'])
        self.assertEqual(s['slew_violations'],0)
        self.assertEqual(s['slew_worst_margin'],0.25)
        self.assertIsNone(s['fanout_violations'])

    def test_positive_slack_has_zero_wns(self):
        s=snapshot({'screen_x__timing__setup__ws':2,'screen_x__timing__hold__ws':1},'x','')
        self.assertEqual(s['setup_wns_ns'],0)
        self.assertIsNone(finite(float('nan')))

    def test_repair_net_edits_exclude_fill_and_count_replacements(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'before.tsv';q=Path(tmp)/'after.tsv'
            header='name\tmaster\ttype\tarea_um2\n'
            p.write_text(header+'a\tsky130_fd_sc_hd__and2_1\tCORE\t2\nb\tsky130_fd_sc_hd__buf_1\tCORE\t1\nf\tsky130_fd_sc_hd__fill_1\tCORE_SPACER\t100\n')
            q.write_text(header+'a\tsky130_fd_sc_hd__and2_2\tCORE\t4\nc\tsky130_fd_sc_hd__buf_2\tCORE\t2\n')
            result=edits(inventory(p),inventory(q))
            self.assertEqual(result,{'inserted':1,'removed':1,'master_changed':1,'inserted_buffers':1,'area_delta_um2':3.0})

if __name__=='__main__':unittest.main()
