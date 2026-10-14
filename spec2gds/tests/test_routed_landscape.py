"""Routed batch replay/identity and determinism-gate regressions; no EDA calls."""
import copy
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from s2g.common import GateError, digest, read, write

BASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('routed_batch', BASE / 'scripts/route_alu_landscape.py')
batch = importlib.util.module_from_spec(spec); spec.loader.exec_module(batch)


def row(edits=40, slack=.3):
    x = {'area_um2': 100., 'setup_ws_ns': .1, 'logic_depth': 12, 'cells': 20, 'fingerprint': 'fp',
         'elapsed_s': 200., 'routed': {'status': 'COMPLETE', 'acceptance': 'ACCEPTED', 'gates': {'setup': True},
         'area_um2': 120., 'setup_ws_ns': slack, 'hold_ws_ns': .2, 'repair_net_edits': edits,
         'repair_events': [{'command': 'repair_design', 'inserted': edits}],
         'parasitics': 'detailed_route_extraction'}}
    x['decision_signature'] = batch.signature(x)
    return x


class Batch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_metric_determinism_excludes_wall_time_but_catches_repair_and_timing_drift(self):
        a = row(); b = copy.deepcopy(a); b['elapsed_s'] = 9999
        self.assertEqual(batch.signature(a), batch.signature(b))
        self.assertNotEqual(batch.signature(a), batch.signature(row(edits=41)))
        self.assertNotEqual(batch.signature(a), batch.signature(row(slack=.300001)))

    def test_probe_gate_requires_every_case_and_exact_metric_agreement(self):
        write(self.root / 'expectations.json', {'riscv32': {'a': row(), 'b': row()}})
        out = batch.compare_probe(self.root, {'probe:riscv32:a': row()})
        self.assertEqual(out['status'], 'FAIL')
        out = batch.compare_probe(self.root, {'probe:riscv32:a': row(), 'probe:riscv32:b': row(edits=41)})
        self.assertEqual(out['status'], 'FAIL')
        out = batch.compare_probe(self.root, {'probe:riscv32:a': row(), 'probe:riscv32:b': row()})
        self.assertEqual((out['status'], out['matched']), ('PASS', 2))

    def test_finished_job_is_recovered_without_another_pipeline_run(self):
        j = {'workspace': str(self.root), 'candidate': 'c0000', 'id': 'full:r:a', 'key': 'a', 'config': {}}
        write(self.root / 'candidates/c0000/record.json', {'status': 'EVALUATED', 'last_run': 'run-000'})
        with patch.object(batch, 'collect', return_value={'evidence_sha256': 'h'}), patch.object(batch.pipeline, 'evaluate') as ev:
            recovered = batch.recover_job(j, {'evidence_sha256': 'h'})
            self.assertEqual(recovered['evidence_sha256'], 'h')
            ev.assert_not_called()

    def test_completed_evidence_cannot_change_or_be_claimed_without_terminal_state(self):
        j = {'workspace': str(self.root), 'candidate': 'c0000', 'id': 'full:r:a', 'key': 'a', 'config': {}}
        p = self.root / 'candidates/c0000/record.json'
        write(p, {'status': 'EVALUATED', 'last_run': 'run-000'})
        with patch.object(batch, 'collect', return_value={'evidence_sha256': 'changed'}):
            with self.assertRaisesRegex(GateError, 'evidence changed'):
                batch.recover_job(j, {'evidence_sha256': 'h'})
        write(p, {'status': 'SUBMITTED'})
        with self.assertRaisesRegex(GateError, 'without terminal'):
            batch.recover_job(j, {'evidence_sha256': 'h'})

    def test_interrupted_running_candidate_is_not_blindly_rerun(self):
        j = {'workspace': str(self.root), 'candidate': 'c0000', 'id': 'full:r:a'}
        write(self.root / 'candidates/c0000/record.json', {'status': 'RUNNING'})
        with patch.object(batch.pipeline, 'evaluate') as ev:
            with self.assertRaisesRegex(GateError, 'new workspace'):
                batch.recover_job(j)
            ev.assert_not_called()

    def test_bulk_import_binds_rtl_and_budget_without_skipping_validation(self):
        src = self.root / 'source.v'; src.write_text('module m; endmodule\n')
        ws = self.root / 'workspace'; ws.mkdir(); (ws / '.lock').touch()
        write(ws / 'state.json', {'schema': 's2g.pool.v1', 'attempts': []})
        freeze = {'spec_hash': 's', 'context_hash': 'c'}; write(ws / 'freeze.json', freeze)
        entries = [('a', {'rtl': str(src), 'rtl_sha256': digest(src), 'config': {'select': 'case'}})]
        verified = ({'design_family': 'family'}, {'candidate_budget': 1}, freeze)
        with patch.object(batch.workspace, 'verify', return_value=verified):
            cids = batch.bulk_import_new(ws, entries)
            self.assertEqual(cids, {'a': 'c0000'})
            rec = read(ws / 'candidates/c0000/record.json')
            self.assertEqual(rec['status'], 'SUBMITTED')
            self.assertEqual(rec['rtl_hash'], digest(src))
            self.assertEqual(rec['freeze_hash'], digest(ws / 'freeze.json'))
            self.assertEqual(read(ws / 'state.json')['attempts'], ['c0000'])
            self.assertNotIn('last_run', rec)
            with self.assertRaisesRegex(GateError, 'empty workspace'):
                batch.bulk_import_new(ws, entries)

    def test_over_budget_or_changed_sources_fail_before_creating_candidates(self):
        src = self.root / 'source.v'; src.write_text('module m; endmodule\n')
        ws = self.root / 'workspace'; ws.mkdir()
        entry = ('a', {'rtl': str(src), 'rtl_sha256': digest(src), 'config': None})
        with patch.object(batch.workspace, 'verify', return_value=({}, {'candidate_budget': 0}, {})):
            with self.assertRaisesRegex(GateError, 'budget'):
                batch.bulk_import_new(ws, [entry])
        entry[1]['rtl_sha256'] = 'stale'
        with patch.object(batch.workspace, 'verify', return_value=({}, {'candidate_budget': 1}, {})):
            with self.assertRaisesRegex(GateError, 'source changed'):
                batch.bulk_import_new(ws, [entry])
        self.assertFalse((ws / 'candidates').exists())


if __name__ == '__main__':
    unittest.main()
