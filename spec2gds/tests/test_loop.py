"""Upgrade-plan section 12 tests: controller, feedback, evidence parsers (stubbed stages)."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from s2g import controller, cost, experiment, feedback as fb, provider as pv, synthesis
from s2g.backend import parasitic_provenance
from s2g.common import GateError, digest, identity, read, write
from s2g.pipeline import acceptance

BASE = Path(__file__).resolve().parents[1]
POLICY = read(BASE / 'configs/loop-policy-v1.json')
CONFIRM = POLICY['final_confirmation']['repeat_synthesis']
GOLD = 'module m(input [1:0] a, output [1:0] y);\n  assign y = a;\nendmodule\n'
SMALL = 'module m(input [1:0] a, output [1:0] y);\n  assign y = {a[1], a[0]};\nendmodule\n'
WRONG = 'module m(input [1:0] a, output [1:0] y);\n  assign y = ~a;\nendmodule\n'
SAME = 'module m(input [1:0] a, output [1:0] y);\n  assign y = a; // same logic\nendmodule\n'
ALT = 'module m(input [1:0] a, output [1:0] y);\n  assign y = a & a;\nendmodule\n'


def evidence(area, ws=1.0, fp='fp0', issues=(), mapping='COMPLETE'):
    valid = lambda v, u: {'value': v, 'unit': u, 'status': 'VALID' if v is not None else 'UNKNOWN', 'reason': None}  # noqa: E731
    return {'metrics': {'mapped_area': valid(area, 'um^2'), 'setup_ws': valid(ws, 'ns'), 'cell_count': valid(3, 'cells'),
                        'logic_depth': valid(1, 'cells'), 'setup_tns': valid(0.0, 'ns'),
                        'unconstrained_endpoints': valid(0, 'count')},
            'mapping': {'status': mapping}, 'structure': {'fingerprint': fp}, 'issues': list(issues), 'paths': []}


OUTCOMES = {  # rtl text -> (functional, area, fingerprint)
    GOLD: ('PASS', 10.0, 'fp-gold'), SMALL: ('PASS', 8.0, 'fp-small'), WRONG: ('FAIL', None, None),
    SAME: ('PASS', 10.0, 'fp-gold'), ALT: ('PASS', 10.0, 'fp-alt')}
PHYS = {GOLD: ('ACCEPTED', 0.4), SMALL: ('ACCEPTED', 0.5), ALT: ('REJECTED', -0.3)}  # routed outcome, setup ws


class Crash(Exception):
    pass


class FakeWS:
    """In-memory stand-in for the workspace/pipeline adapter; counts every tool call."""

    def __init__(self, root):
        self.root = Path(root)
        (self.root / 'frozen').mkdir(parents=True)
        (self.root / 'frozen/reference.v').write_text(GOLD)
        write(self.root / 'freeze.json', {'tools': {'yosys': 'x'}, 'spec_hash': 's'})
        self.recs, self.order, self.evals, self.crash_on = {}, [], [], None
        self.tool_failures, self.run_budget = {}, 200

    def verify(self):
        return ({'top': 'm', 'ports': {}, 'behavior': 'b', 'arithmetic': '', 'legal_inputs': '', 'requirements': []},
                {'candidate_budget': 50, 'tool_run_budget': self.run_budget, 'corner': 'typical', 'platform': 'nangate45'},
                read(self.root / 'freeze.json'))

    def reserve(self, parent, action):
        cid = 'c%04d' % len(self.order)
        self.order.append(cid)
        self.recs[cid] = {'status': 'RESERVED', 'action': action, 'runs': []}
        return cid

    def submit(self, cid, path):
        text = Path(path).read_text()
        (self.root / 'candidates' / cid).mkdir(parents=True, exist_ok=True)
        (self.root / 'candidates' / cid / 'rtl.v').write_text(text)
        self.recs[cid].update(status='SUBMITTED', text=text, rtl_hash=identity(text))
        return self.recs[cid]

    def abort(self, cid, reason):
        self.recs[cid].update(status='GENERATION_FAILED', error=reason)

    def evaluate(self, cid, until):
        if self.crash_on == (cid, until):
            self.crash_on = None
            raise Crash('simulated power loss')
        rec = self.recs[cid]
        run = 'run-%03d' % len(rec['runs'])
        rec['runs'].append((run, until)); rec['last_run'] = run; rec['status'] = 'EVALUATED'
        self.evals.append((cid, until))
        return {'status': 'COMPLETE', 'run': run, 'error': None, 'acceptance': {'status': 'REJECTED'}}

    def physical(self, cid, run):
        if dict(self.recs[cid]['runs']).get(run) != 'finish':
            return None
        acc, ws = PHYS[self.recs[cid]['text']]
        risk = {'schema': 's2g.risk.v2', 'setup_ws': ws, 'setup_tns': min(ws, 0), 'area_um2': 20.0,
                'parasitics': {'provenance': 'detailed_route_extraction'}}
        return {'risk': risk, 'acceptance': {'status': acc, 'gates': {'setup': ws >= 0, 'drc': True}}}

    def snapshot(self, cid, run=None):
        func, area, fp = OUTCOMES[self.recs[cid]['text']]
        return {'candidate': cid, 'run': run, 'rtl_sha256': self.recs[cid]['rtl_hash'],
                'status': 'COMPLETE' if func == 'PASS' else 'FAILED', 'error': None if func == 'PASS' else 'SAT counterexample',
                'functional': func, 'mapped_proof': 'PASS' if func == 'PASS' else 'NOT_RUN',
                'synthesis': evidence(area, fp=fp) if func == 'PASS' else None, 'physical': self.physical(cid, run),
                'run_dir': str(self.root / 'candidates' / cid / (run or 'run-000'))}

    def record(self, cid):
        return self.recs[cid]

    def attempts(self):
        return list(self.order)

    def run_count(self):
        return sum(len(r['runs']) for r in self.recs.values())

    def rtl(self, cid):
        return self.recs[cid]['text']

    def costs(self, cid):
        n = len(self.recs[cid]['runs'])
        return {'tool_invocations': 3 * n, 'tool_wall_s': 1.0 * n, 'failed_invocations': self.tool_failures.get(cid, 0)}


def submit(rtl, **extra):
    return {'content': json.dumps({'action': 'submit', 'rtl': rtl, 'hypothesis': 'h', **extra})}


def policy(physical=None, **budget):
    p = json.loads(json.dumps(POLICY))
    p['budget'].update(budget)
    p['physical'].update(physical or {'search_schedule': 'none', 'final': False})
    return p


class LoopBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'ws'
        self.ws = FakeWS(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def start(self, responses, arm='A2', physical=None, **budget):
        p = Path(self.tmp.name) / 'policy.json'
        write(p, policy(physical, **budget))
        transport = pv.Scripted(responses)
        controller.init(self.root, 'L', arm, p, transport, ws=self.ws)
        return controller.Loop(self.root, 'L', transport, ws=self.ws)

    def prompts(self):
        reqs = sorted((self.root / 'loops/L/proposals').glob('*/request-*/request.json'))
        return [read(r)['payload']['messages'][1]['content'] for r in reqs]


class LoopTests(LoopBase):
    # 1
    def test_mandatory_synthesis_findings_without_queries(self):
        loop = self.start([submit(SMALL)], candidate_submissions=1)
        loop.run()
        first = self.prompts()[0]
        self.assertIn('Evidence (pushed automatically)', first)
        self.assertIn('mapped_area', first)
        self.assertIn('information exposed: contract, correctness, synthesis', first)
        self.assertEqual(loop.state['incumbent'], 'c0001')  # 8.0 < 10.0 promoted

    def test_model_stop_is_a_recorded_outcome(self):
        loop = self.start([submit(SMALL), {'content': '{"action": "stop", "reason": "no justified change"}'}],
                          candidate_submissions=3)
        loop.run()
        self.assertEqual([a['outcome'] for a in loop.state['attempts']], ['PROMOTED', 'MODEL_STOPPED'])
        self.assertEqual(loop.state['stop_reason'], 'model_reported_no_justified_rtl_action')

    def test_a1_receives_no_synthesis_metrics(self):
        loop = self.start([submit(SMALL), submit(SAME)], arm='A1', candidate_submissions=2)
        loop.run()
        for text in self.prompts():
            self.assertNotIn('Metrics (incumbent)', text)
            self.assertNotIn('um^2', text)
            self.assertNotIn('synthesis:', text)

    # 2
    def test_blocking_issue_survives_truncation_and_category_competition(self):
        issues = [{'issue_id': f't{i}', 'category': 'timing', 'evidence_kind': 'observation', 'severity': 'info',
                   'summary': f'path {i}', 'object_refs': [], 'artifact_ref': None, 'rtl_mapping_status': 'partial',
                   'rtl_refs': [f'rtl.v:{i}']} for i in range(60)]
        issues.append({'issue_id': 'm1', 'category': 'mapping', 'evidence_kind': 'observation', 'severity': 'blocking',
                       'summary': 'unmapped $_AND_', 'object_refs': [], 'artifact_ref': 'stat.json',
                       'rtl_mapping_status': 'not_applicable', 'rtl_refs': []})
        snap = {'candidate': 'c1', 'run': 'run-000', 'rtl_sha256': 'h', 'status': 'COMPLETE', 'functional': 'PASS',
                'mapped_proof': 'PASS', 'error': None, 'physical': None, 'synthesis': evidence(5.0, issues=issues)}
        loop = {'loop_id': 'L', 'manifest_sha256': 'm', 'toolchain_id': 't', 'policy': POLICY, 'corner': 'typical'}
        pk = fb.build_packet(loop=loop, arm='A2', incumbent=snap, baseline=snap, recent=[], budget={},
                             caps={'max_issue_examples': 2})
        self.assertEqual(pk['blocking_categories'], ['mapping'])
        self.assertTrue(pk['detail_selection']['truncated'])
        self.assertEqual(pk['issue_counts']['timing']['info'], 60)
        text, dropped = fb.render(pk, 700)
        self.assertIn('unmapped $_AND_', text)
        self.assertGreater(dropped, 0)
        with self.assertRaises(GateError):
            fb.render(pk, 100)  # mandatory minimum cannot fit -> stop, never drop the gate failure

    # 3
    def test_stale_or_foreign_evidence_rejected(self):
        root = Path(self.tmp.name) / 'real'
        run = root / 'candidates/c0000/run-000'
        run.mkdir(parents=True)
        (root / 'candidates/c0000/rtl.v').write_text(GOLD)
        write(root / 'frozen/spec.json', {}); write(root / 'frozen/context.json', {})
        write(root / 'freeze.json', {'files': {}})
        rec = {'rtl_hash': digest(root / 'candidates/c0000/rtl.v'), 'freeze_hash': digest(root / 'freeze.json'),
               'last_run': 'run-000'}
        write(root / 'candidates/c0000/record.json', rec)
        write(run / 'ledger.json', {'candidate': 'c0000', 'run': 'run-000', 'rtl_hash': rec['rtl_hash'],
                                    'freeze_hash': rec['freeze_hash'], 'status': 'COMPLETE'})
        write(run / 'synthesis_evidence.json', {'rtl_sha256': 'an-older-rtl', 'candidate': 'c0000', 'run': 'run-000'})
        write(run / 'evidence.json', {'files': {}})
        with self.assertRaises(GateError):
            fb.snapshot(root, 'c0000')  # timing/area of a previous RTL version
        write(run / 'synthesis_evidence.json', {'rtl_sha256': rec['rtl_hash'], 'candidate': 'c0000', 'run': 'run-000'})
        write(run / 'risk.json', {'setup_ws': 1})
        with self.assertRaises(GateError):
            fb.snapshot(root, 'c0000')  # physical RC/timing without a finish stage in this run
        (run / 'risk.json').unlink()
        self.assertEqual(fb.snapshot(root, 'c0000')['candidate'], 'c0000')
        (root / 'candidates/c0000/rtl.v').write_text(WRONG)
        with self.assertRaises(GateError):
            fb.snapshot(root, 'c0000')  # RTL changed after the run

    # 4
    def test_missing_constraints_give_unknown_timing(self):
        log = ('S2G_CHECK_SETUP_BEGIN\nWarning: There are 9 unconstrained endpoints.\n  y[0]\nS2G_CHECK_SETUP_END\n'
               'S2G_ENDPOINTS 9\nS2G_SETUP_WS 1.0000000150474662e+30\nS2G_HOLD_WS 1e+30\nS2G_SETUP_TNS 0.0\n'
               'S2G_SLEW_COUNT 0\nS2G_CAP_COUNT 0\nS2G_FANOUT_COUNT 0\nS2G_STA_COMPLETE\n')
        t = synthesis.parse_sta(log, '{"checks": []}', {'exit_code': 0, 'timed_out': False})
        self.assertEqual(t['status'], 'UNKNOWN')
        self.assertIsNone(t['setup_ws'])
        snap = {'status': 'COMPLETE', 'functional': 'PASS', 'mapped_proof': 'PASS', 'synthesis': evidence(5.0, ws=None, fp='x')}
        inc = {'status': 'COMPLETE', 'functional': 'PASS', 'mapped_proof': 'PASS', 'synthesis': evidence(10.0, fp='y')}
        d = controller.decide(POLICY, snap, inc)
        self.assertFalse(d['promote'])
        self.assertEqual(d['outcome'], 'UNKNOWN_METRIC')

    # 5
    def test_unmapped_logic_invalidates_mapped_metrics(self):
        stat = {'modules': {'\\m': {'area': 3.0, 'num_cells': 2, 'num_cells_by_type': {'INV_X1': 1, '$_AND_': 1}}}}
        lib = {'INV_X1': {'area': 0.532, 'seq': None, 'pins': {}}}
        out = synthesis.parse_stat(stat, lib, {'INV_X1': (0.38, 1.4)})
        self.assertEqual(out['mapped_area']['status'], 'INVALID')
        self.assertIsNone(out['mapped_area']['value'])
        ev = {'metrics': {k: {'status': 'INVALID', 'value': None} for k in
                          ('setup_ws', 'setup_tns', 'slew_violations', 'cap_violations', 'fanout_violations')},
              'timing': {'status': 'INVALID', 'reason': 'mapping'}, 'paths': [], 'warnings': [],
              'mapping': {'status': 'INCOMPLETE', 'unmapped_cell_types': {'$_AND_': 1}, 'coarse_state_cells': 0}}
        self.assertEqual(synthesis.issues_from(ev)[0]['severity'], 'blocking')

    # 6
    def test_simulation_pass_with_equivalence_counterexample_never_promoted(self):
        cand = {'status': 'FAILED', 'functional': 'FAIL', 'mapped_proof': 'NOT_RUN', 'error': 'SAT counterexample',
                'synthesis': evidence(1.0, fp='z')}
        inc = {'status': 'COMPLETE', 'functional': 'PASS', 'mapped_proof': 'PASS', 'synthesis': evidence(10.0, fp='y')}
        d = controller.decide(POLICY, cand, inc)
        self.assertFalse(d['promote'])
        self.assertEqual(d['outcome'], 'REJECTED_GATE')
        loop = self.start([submit(WRONG)], candidate_submissions=1)
        loop.run()
        self.assertEqual(loop.state['incumbent'], 'c0000')

    # 7
    def test_timeout_or_parser_failure_is_not_a_pass(self):
        t = synthesis.parse_sta('', '', {'exit_code': None, 'timed_out': True})
        self.assertEqual(t['status'], 'ERROR')
        ok_log = 'S2G_CHECK_SETUP_BEGIN\nS2G_CHECK_SETUP_END\nS2G_ENDPOINTS 2\nS2G_SETUP_WS 1e-9\nS2G_STA_COMPLETE\n'
        self.assertEqual(synthesis.parse_sta(ok_log, '{broken', {'exit_code': 0, 'timed_out': False})['status'], 'ERROR')
        self.assertEqual(synthesis.parse_sta(ok_log, '{"checks": []}', {'exit_code': 0, 'timed_out': False})['status'], 'VALID')
        loop = self.start([{'content': 'not json at all'}], candidate_submissions=1)
        loop.run()
        self.assertTrue(loop.state['attempts'][0]['correctness'].startswith('PROPOSAL_FAILED'))
        self.assertEqual(loop.state['incumbent'], 'c0000')

    # 8
    def test_duplicate_consumes_allowance_without_reevaluation(self):
        loop = self.start([submit(SMALL), submit(SMALL + '\n// whitespace and comments differ\n')],
                          candidate_submissions=2)
        loop.run()
        outcomes = [a['outcome'] for a in loop.state['attempts']]
        self.assertEqual(outcomes, ['PROMOTED', 'DUPLICATE'])
        self.assertEqual(self.ws.order, ['c0000', 'c0001'])  # no candidate created for the duplicate
        self.assertEqual(self.ws.evals.count(('c0001', 'synthesis')), 1 + CONFIRM)  # search run + final confirmation
        self.assertEqual(loop.state['stop_reason'], 'budget_exhausted:candidate_submissions')

    # 9
    def test_transport_retries_and_resume_create_no_hidden_search(self):
        cfg = dict(POLICY['provider'], transport_retries=2)
        t = pv.Scripted([{'error': 'HTTP 503'}, {'error': 'timeout'}, {'content': '{"action": "stop"}'}])
        with tempfile.TemporaryDirectory() as d:
            r = pv.call(t, cfg, 's', 'u', d, 'rid')
            self.assertEqual(r['transport_attempts'], 3)
            self.assertEqual(len(list(Path(d).glob('transport-*.json'))), 3)
            self.assertEqual(len({read(p)['request_id'] for p in Path(d).glob('transport-*.json')}), 1)
        loop = self.start([submit(SMALL)], candidate_submissions=1)
        self.ws.crash_on = ('c0001', 'synthesis')
        with self.assertRaises(Crash):
            loop.run()
        # Resume with an empty script: any resend of the completed request would fail loudly.
        resumed = controller.Loop(self.root, 'L', pv.Scripted([]), ws=self.ws)
        resumed.run()
        self.assertEqual(resumed.usage()['model_requests'], 1)
        self.assertEqual(len(resumed.state['attempts']), 1)
        self.assertEqual(resumed.state['incumbent'], 'c0001')
        self.assertEqual(self.ws.evals.count(('c0001', 'synthesis')), 1 + CONFIRM)

    # 10
    def test_detail_queries_bounded_and_task_isolated(self):
        q = {'content': json.dumps({'action': 'query', 'queries': [{'evidence_id': 'rtl', 'query_type': 'rtl_region'}] * 4})}
        loop = self.start([q, q, submit(SMALL)], candidate_submissions=1, query_calls=3, model_requests=3)
        loop.run()
        self.assertLessEqual(loop.usage()['query_calls'], 3)
        snap = self.ws.snapshot('c0000', 'run-000')
        with self.assertRaises(GateError):
            fb.query(self.root, snap, {'c9999'}, fb.EXPOSURE['A2'], 'rtl', 'rtl_region', 100)  # not evaluated here
        with self.assertRaises(GateError):
            fb.query(self.root, snap, {'c0000'}, fb.EXPOSURE['A1'], 'path-001', 'path_detail', 100)  # not exposed
        foreign = dict(snap, run_dir='/tmp/other-task/candidates/c0000/run-000')
        with self.assertRaises(GateError):
            fb.query(self.root, foreign, {'c0000'}, fb.EXPOSURE['A2'], 'rtl', 'rtl_region', 100)

    # 11
    def test_estimated_spef_is_not_labeled_routed_extraction(self):
        with tempfile.TemporaryDirectory() as d:
            run = Path(d) / 'run'
            logs = run / 'backend/work/logs/nangate45/top/base'
            logs.mkdir(parents=True)
            results = Path(d) / 'results'; results.mkdir()
            (results / '6_final.spef').write_text('*SPEF "IEEE 1481-1998"\n')
            rules = Path(d) / 'rcx.rules'; rules.write_text('rules')
            write(run / 'ledger.json', {'stages': {'route': {}, 'finish': {}}})
            c = {'platform': 'nangate45', 'platform_dir': d, 'rcx_rules': 'rcx.rules'}
            (logs / '6_report.log').write_text('estimate_parasitics -global_routing\nwrite_spef 6_final.spef\n')
            self.assertNotEqual(parasitic_provenance(run, results, c)['provenance'], 'detailed_route_extraction')
            (logs / '6_report.log').write_text(f'[INFO RCX-0435] Reading extraction model file {rules} ...\n'
                                                '[INFO RCX-0045] Extract 10 nets, 20 rsegs\n')
            self.assertEqual(parasitic_provenance(run, results, c)['provenance'], 'detailed_route_extraction')
            other = Path(d) / 'other.rules'; other.write_text('different')
            (logs / '6_report.log').write_text(f'[INFO RCX-0435] Reading extraction model file {other} ...\n'
                                                '[INFO RCX-0045] Extract 10 nets\n')
            self.assertEqual(parasitic_provenance(run, results, c)['provenance'], 'unknown')

    # 12
    def test_circuit_and_graph_gates_are_independent(self):
        c = read(BASE / 'configs/local-nangate45.json')
        ledger = {'status': 'COMPLETE', 'functional': {'status': 'PASS'}, 'synthesis': {'mapped_proof': 'PASS'},
                  'postroute_equivalence': {'status': 'PASS'}, 'stages': {'finish': {'status': 'COMPLETE'}},
                  'graph_error': 'DEF join failed'}
        risk = dict(setup_ws=1, hold_ws=1, slew_violations=0, cap_violations=0, fanout_violations=0, schema='s2g.risk.v2')
        sig = {k: {'status': 'PASS'} for k in ('gds', 'drc', 'lvs')}
        r = acceptance(ledger, risk, sig, c)
        self.assertEqual((r['status'], r['dataset_gates']['graph']), ('ACCEPTED', 'FAIL'))
        ledger.update(graphs=['g.json'], graph_error=None, functional={'status': 'FAIL'})
        r = acceptance(ledger, risk, sig, c)
        self.assertEqual((r['status'], r['dataset_gates']['graph']), ('REJECTED', 'PASS'))

    # 13
    def test_model_cannot_modify_checker_reference_constraints_or_budget(self):
        rogue = 'module tb; endmodule\n' + SMALL
        loop = self.start([submit(SMALL, budget={'candidate_submissions': 99}, sdc='', tb='module tb; endmodule'),
                           submit(rogue)], candidate_submissions=2)
        before = read(self.root / 'loops/L/manifest.json')
        loop.run()
        self.assertEqual(read(self.root / 'loops/L/manifest.json'), before)
        self.assertEqual(loop.remaining()['candidate_submissions'], 0)
        self.assertTrue(loop.state['attempts'][1]['correctness'].startswith('STATIC_REJECTED'))
        self.assertEqual(self.ws.recs['c0002']['status'], 'GENERATION_FAILED')
        man = read(self.root / 'loops/L/manifest.json')
        man['policy']['budget']['candidate_submissions'] = 99
        write(self.root / 'loops/L/manifest.json', man)
        with self.assertRaises(GateError):
            controller.Loop(self.root, 'L', pv.Scripted([]), ws=self.ws).run()

    # 14
    def test_cost_totals_include_rejected_and_failed_invocations(self):
        self.ws.tool_failures = {'c0001': 2}
        loop = self.start([submit(WRONG), submit(SMALL)], candidate_submissions=2)
        loop.run()
        s = read(self.root / 'loops/L/summary.json')
        self.assertEqual(s['costs']['failed_tool_invocations'], 2)
        self.assertEqual(s['costs']['tool_invocations'], 3 * len(self.ws.evals))
        self.assertEqual(s['counters']['candidate_submissions'], 2)
        self.assertEqual(s['attempts'][0]['outcome'], 'REJECTED_GATE')

    # 15
    def test_crash_recovery_preserves_decision_trace(self):
        loop = self.start([submit(SAME), submit(SMALL)], candidate_submissions=2)
        self.ws.crash_on = ('c0002', 'synthesis')
        with self.assertRaises(Crash):
            loop.run()
        events_before = (self.root / 'loops/L/events.jsonl').read_text()
        decision_before = read(self.root / 'loops/L/decisions/p001.json')
        resumed = controller.Loop(self.root, 'L', pv.Scripted([]), ws=self.ws)
        resumed.run()
        self.assertTrue((self.root / 'loops/L/events.jsonl').read_text().startswith(events_before))
        self.assertEqual(read(self.root / 'loops/L/decisions/p001.json'), decision_before)
        self.assertEqual([a.get('outcome') for a in resumed.state['attempts']], ['COLLAPSED', 'PROMOTED'])
        self.assertEqual(resumed.state['state'], 'COMPLETE')


class ReviewRegressions(LoopBase):
    """Fault-injection cases from the 2026-10-12 review."""

    def crash_at(self, target):
        real = controller.Loop.goto

        def goto(loop, state, **why):
            if state == target and not getattr(goto, 'fired', False):
                goto.fired = True
                raise Crash('crash before committing ' + state)
            return real(loop, state, **why)
        return patch.object(controller.Loop, 'goto', goto)

    def test_crash_after_response_before_freeze_still_verifies_candidate(self):
        loop = self.start([submit(SMALL)], candidate_submissions=1)
        with self.crash_at('FREEZE_CANDIDATE'), self.assertRaises(Crash):
            loop.run()
        resumed = controller.Loop(self.root, 'L', pv.Scripted([]), ws=self.ws)
        resumed.run()
        self.assertEqual([a.get('outcome') for a in resumed.state['attempts']], ['PROMOTED'])
        self.assertEqual(resumed.usage()['model_requests'], 1)
        self.assertEqual(self.ws.evals.count(('c0001', 'synthesis')), 1 + CONFIRM)

    def test_uncommitted_attempt_from_older_state_is_resumed_not_skipped(self):
        loop = self.start([submit(SMALL)], candidate_submissions=1)
        with self.crash_at('FREEZE_CANDIDATE'), self.assertRaises(Crash):
            loop.run()
        st = read(self.root / 'loops/L/state.json')  # reproduce the pre-fix save-then-goto window
        st['attempts'].append(st['inflight']); st['inflight'] = None
        write(self.root / 'loops/L/state.json', st)
        resumed = controller.Loop(self.root, 'L', pv.Scripted([]), ws=self.ws)
        resumed.run()
        self.assertEqual([a.get('outcome') for a in resumed.state['attempts']], ['PROMOTED'])
        self.assertFalse((self.root / 'loops/L/proposals/p002').exists())

    def routed_failure_prompt(self, arm):
        loop = self.start([submit(ALT), submit(SAME)], arm=arm, candidate_submissions=2,
                          physical={'search_schedule': 'first_eligible', 'search_quota': 1, 'final': False})
        loop.run()
        self.assertEqual(loop.state['attempts'][0]['outcome'], 'NO_GAIN')
        self.assertTrue(loop.state['candidates']['c0001'].get('phys_run'))
        return self.prompts()[1]

    def test_a3_receives_routed_failure_of_unpromoted_candidate(self):
        text = self.routed_failure_prompt('A3')
        self.assertIn('Most recent evaluated candidate (not promoted) c0001', text)
        self.assertIn('acceptance gate setup not met', text)
        self.assertIn('setup worst slack -0.3 ns', text)

    def test_a2_gets_the_failed_candidates_synthesis_but_never_its_routed_result(self):
        text = self.routed_failure_prompt('A2')
        self.assertIn('Most recent evaluated candidate (not promoted) c0001', text)
        self.assertNotIn('acceptance gate', text)
        self.assertNotIn('-0.3', text)

    def test_final_label_requires_complete_confirmation(self):
        p = policy({'search_schedule': 'none', 'final': True})
        ok = {'repeats': {'c1': [{'same_fingerprint': True, 'area': 8.0}], 'c0': [{'same_fingerprint': True, 'area': 10.0}]},
              'physical': {c: {'acceptance': 'ACCEPTED', 'parasitics': 'detailed_route_extraction'} for c in ('c1', 'c0')}}
        self.assertEqual(controller.final_label(p, 'c1', 'c0', ok), ('ACCEPTED_IMPLEMENTATION', []))
        cases = [('repeats', {'c1': [], 'c0': []}),
                 ('repeats', {'c1': [{'same_fingerprint': False, 'area': 8.0}], 'c0': ok['repeats']['c0']}),
                 ('physical', {'c1': ok['physical']['c1'], 'c0': {'status': 'NOT_RUN'}}),
                 ('physical', {'c1': dict(ok['physical']['c1'], parasitics='unknown'), 'c0': ok['physical']['c0']})]
        for key, value in cases:
            label, missing = controller.final_label(p, 'c1', 'c0', dict(ok, **{key: value}))
            self.assertEqual(label, 'SYNTHESIS_PROVISIONAL')
            self.assertTrue(missing)
        self.assertEqual(controller.final_label(p, 'c0', 'c0', {})[0], 'ORIGINAL_RETAINED')
        rejected = dict(ok, physical={'c1': dict(ok['physical']['c1'], acceptance='REJECTED'), 'c0': ok['physical']['c0']})
        self.assertEqual(controller.final_label(p, 'c1', 'c0', rejected)[0], 'PHYSICALLY_REJECTED')

    def test_skipped_final_resynthesis_is_not_reported_reproducible(self):
        loop = self.start([submit(SMALL)], candidate_submissions=1,
                          physical={'search_schedule': 'none', 'final': True})
        self.ws.run_budget = 2  # baseline + one candidate run; no room for final re-synthesis
        loop.run()
        final = loop.state['final']
        self.assertEqual(final['physical']['c0001']['acceptance'], 'ACCEPTED')
        self.assertEqual(final['label'], 'SYNTHESIS_PROVISIONAL')
        self.assertFalse(final['synthesis_reproducible'])
        self.assertTrue(any('repeat synthesis' in m for m in final['unmet_requirements']))


PRICING = read(BASE / 'configs/pricing-deepseek-flash-2026-10-12.json')


class CostAndExperimentTests(LoopBase):
    def ledger(self, ceiling):
        path = Path(self.tmp.name) / 'ledger.json'
        cost.create(path, ceiling, PRICING)
        return path

    def start_with_ledger(self, responses, ceiling, **budget):
        p = Path(self.tmp.name) / 'policy.json'
        write(p, policy(**budget))
        transport = pv.Scripted(responses)
        path = self.ledger(ceiling)
        controller.init(self.root, 'L', 'A2', p, transport, ws=self.ws, cost_ledger=path)
        return controller.Loop(self.root, 'L', transport, ws=self.ws), path

    def test_ledger_reserves_worst_case_settles_actual_and_keeps_unknown(self):
        led = cost.Ledger(self.ledger(0.15))
        worst = led.worst_case(PRICING, 10000, 8192, 1)
        self.assertAlmostEqual(worst, ((10000 + 1024) * 2.0 + 8192 * 8.0) / 1e6, places=6)
        led.reserve('r1', 10000, 8192, 1, 'o')
        self.assertEqual(led.reserve('r1', 10000, 8192, 1, 'o')['reserved'], worst)  # idempotent
        led.settle('r1', [{'prompt_cache_hit_tokens': 1000, 'prompt_cache_miss_tokens': 2000, 'completion_tokens': 300}], 1)
        self.assertAlmostEqual(led.snapshot()['entries']['r1']['actual'], (1000 * 0.04 + 2000 * 2.0 + 300 * 8.0) / 1e6)
        led.reserve('r2', 10000, 8192, 1, 'o'); led.keep_unknown('r2', 'crash')
        self.assertAlmostEqual(led.headroom(), 0.15 - worst - led.snapshot()['entries']['r1']['actual'], places=6)
        with self.assertRaises(GateError):
            led.reserve('r3', 10000, 8192, 1, 'o')  # 3rd worst case would exceed the ceiling

    def test_currency_ceiling_stops_before_sending_without_spending_a_slot(self):
        loop, _ = self.start_with_ledger([submit(SMALL)], 0.01, candidate_submissions=2)
        loop.run()
        self.assertTrue(loop.state['stop_reason'].startswith('budget_exhausted:currency_ceiling'))
        self.assertEqual(loop.state['attempts'], [])
        self.assertEqual(len(loop.transport.items), 1)  # nothing was sent

    def test_requests_are_settled_from_reported_usage(self):
        usage = {'prompt_tokens': 3000, 'prompt_cache_hit_tokens': 0, 'prompt_cache_miss_tokens': 3000, 'completion_tokens': 200}
        loop, path = self.start_with_ledger([dict(submit(SMALL), usage=usage)], 5.0, candidate_submissions=1)
        loop.run()
        entries = list(read(path)['entries'].values())
        self.assertEqual([e['status'] for e in entries], ['SETTLED'])
        self.assertAlmostEqual(entries[0]['actual'], (3000 * 2.0 + 200 * 8.0) / 1e6)
        self.assertEqual(entries[0]['owner'], loop.man['manifest_sha256'])
        self.assertEqual(loop.usage()['prompt_tokens'], 3000)

    def test_live_transport_requires_a_cost_ledger(self):
        class Live(pv.Scripted):
            live = True
        loop = self.start([submit(SMALL)], candidate_submissions=1)
        with self.assertRaises(GateError):
            controller.Loop(self.root, 'L', Live([]), ws=self.ws).run()

    def test_report_counts_every_run_and_never_takes_best_of_arm(self):
        root = Path(self.tmp.name) / 'exp'
        cost.create(root / 'cost_ledger.json', 5.0, PRICING)
        runs = []
        finals = {'A1-r1': ('ORIGINAL_RETAINED', 'c0', [], 10.0),
                  'A2-r1': ('ACCEPTED_IMPLEMENTATION', 'c1', [], 7.0),
                  'A2-r2': ('SYNTHESIS_PROVISIONAL', 'c1', ['repeat synthesis of winner c1: 0/1'], 5.0)}
        for rid, (label, win, unmet, area) in finals.items():
            ws = root / 'runs' / rid
            reps = {'c0': [{'area': 10.0, 'same_fingerprint': True}]}
            if win != 'c0' and not unmet:
                reps[win] = [{'area': area, 'same_fingerprint': True}]
            attempts = [{'proposal': 'p001', 'outcome': 'PROMOTED' if win != 'c0' else 'NO_GAIN', 'promoted': win != 'c0',
                         'correctness': 'functional PASS; mapped proof PASS', 'requests': [
                             {'transport_requests': 1, 'prompt_tokens': 10, 'completion_tokens': 5}]}]
            write(ws / 'loops/main/summary.json', {
                'state': 'COMPLETE', 'stop_reason': 'x', 'baseline': 'c0', 'final_incumbent': win,
                'final': {'label': label, 'unmet_requirements': unmet, 'repeats': reps, 'physical': {}},
                'attempts': attempts, 'counters': {'physical_evaluations': 0},
                'costs': {'tool_invocations': 4, 'tool_wall_s': 1.0, 'failed_tool_invocations': 0}})
            write(ws / 'loops/main/state.json', {'attempts': attempts})
            write(ws / 'loops/main/manifest.json', {'manifest_sha256': rid, 'provider': {'model': 'm', 'paid': False}})
            runs.append({'run': rid, 'arm': rid[:2], 'repeat': 1, 'workspace': str(ws)})
        write(root / 'experiment.json', {'arms': ['A1', 'A2'], 'runs': runs, 'experiment_sha256': 'e'})
        rep = experiment.report(root)
        self.assertEqual(rep['arms']['A1']['confirmed_gain_rel']['values'], [0.0])
        self.assertEqual(rep['arms']['A2']['confirmed_gain_rel']['values'], [0.3])  # unconfirmed run is not a gain
        self.assertEqual(rep['arms']['A2']['runs_unconfirmed'], 1)
        self.assertEqual(rep['arms']['A2']['runs_complete'], 2)
        self.assertNotIn('best', json.dumps(rep['arms']))


if __name__ == '__main__':
    unittest.main()
