"""Hybrid adapter regressions; stub metrics are not experimental PPA results."""
import copy
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from s2g import alu, baselines, controller, generation, provider
from s2g.common import GateError, digest, read, write
from test_loop import FakeWS, Crash, evidence, policy

BASE = Path(__file__).resolve().parents[1]
VOCAB = read(BASE / 'configs/alu-actions-v1.json')
IR = read(BASE / 'examples/riscv_alu32/alu_ir.json')
IR = dict(IR, width=4, shift_amount={'operand': 'b', 'bits': 2})
V2 = read(BASE / 'configs/local-nangate45.json')['rtl_policy']
ORIGIN = IR['origin_config']
FULL = list(alu.ADDER_OPS)


def proposal(parent='c0000', edits=None):
    return {'action': 'alu', 'parent': parent, 'edits': edits or [
        {'op': 'set', 'dimension': 'addsub_share', 'value': FULL}], 'hypothesis': 'h'}


def item(obj):
    return {'content': json.dumps(obj)}


class AluWS(FakeWS):
    def __init__(self, root):
        super().__init__(root)
        (self.root / 'frozen/reference.v').write_text(alu.render(IR, ORIGIN, '// Test license\n'))

    def verify(self):
        spec, ctx, fr = super().verify()
        spec.update(top=IR['top'], latency_cycles=0, clocks=[], resets=[], ports={
            **{p['name']: {'direction': 'input', 'width': p['width'], 'signed': False} for p in IR['op_ports']},
            **{IR[n]: {'direction': 'output' if n == 'y' else 'input', 'width': 4, 'signed': False}
               for n in ('a', 'b', 'y')}})
        ctx['rtl_policy'] = V2
        return spec, ctx, fr

    def submit(self, cid, path):
        r = super().submit(cid, path)
        r['rtl_hash'] = digest(path)
        return r

    def values(self, cid):
        text = self.rtl(cid)
        key = re.findall(r'\(share-[^)]*\)', text)[-1][1:-1]
        cfg = next(c for c in alu.space(IR, VOCAB) if alu.config_id(c) == key)
        if cfg == ORIGIN:
            return 100., 1., 200., 1., key
        if cfg['addsub_share'] == FULL and cfg['shifter'] == 'reverse_left':
            return 60., -.2, 120., .3, key
        if cfg['addsub_share'] == FULL:
            return 70., 1., 160., .4, key
        return 90., 1., 180., .4, key

    def snapshot(self, cid, run=None):
        area, slack, physical_area, physical_slack, fp = self.values(cid)
        phys = None
        if dict(self.recs[cid]['runs']).get(run) == 'finish':
            phys = {'risk': {'schema': 's2g.risk.v2', 'area_um2': physical_area, 'setup_ws': physical_slack,
                             'parasitics': {'provenance': 'detailed_route_extraction'}},
                    'acceptance': {'status': 'ACCEPTED', 'gates': {'setup': True}}}
        return {'candidate': cid, 'run': run, 'rtl_sha256': self.recs[cid]['rtl_hash'], 'status': 'COMPLETE',
                'error': None, 'functional': 'PASS', 'mapped_proof': 'PASS',
                'synthesis': evidence(area, ws=slack, fp=fp), 'physical': phys,
                'run_dir': str(self.root / 'candidates' / cid / (run or 'run-000'))}


class Hybrid(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'ws'
        self.ws = AluWS(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def start(self, replies, mode='hybrid', arm='A2', routed=False, transport=None, n=None):
        n = n or len(replies)
        p = policy(candidate_submissions=n, model_requests=n, physical_evaluations=n + 1)
        p['generation'] = {'mode': mode, 'ir': IR, 'vocabulary': VOCAB,
                           'max_edits_per_proposal': 4, 'parent_policy': 'validated_typed_pool'}
        p['stop']['max_consecutive_no_change'] = n + 1
        p['feedback']['generation_chars'] = 20000
        if routed:
            p['physical'] = {'baseline': True, 'final': True, 'search_schedule': 'first_eligible', 'search_quota': n}
            p['final_selection'] = {'kind': 'routed_pool', 'min_rel_area_improvement': .005}
        path = Path(self.tmp.name) / 'p.json'
        write(path, p)
        t = transport or provider.Scripted([item(x) for x in replies])
        controller.init(self.root, 'L', arm, path, t, self.ws)
        return controller.Loop(self.root, 'L', t, self.ws)

    def prompts(self):
        return [read(p)['payload']['messages'][1]['content'] for p in
                sorted((self.root / 'loops/L/proposals').glob('*/request-*/request.json'))]

    def test_typed_metadata_and_freeform_have_same_mandatory_path(self):
        free = alu.render(IR, dict(ORIGIN, shifter='reverse_right'), '// Free-form candidate\n')
        loop = self.start([proposal(), {'action': 'submit', 'rtl': free}])
        loop.run()
        self.assertEqual([a['outcome'] for a in loop.state['attempts']], ['PROMOTED', 'NO_GAIN'])
        self.assertEqual(loop.state['candidates']['c0001']['generation']['primitive_edits'], 1)
        self.assertNotIn('generation', loop.state['candidates']['c0002'])
        self.assertEqual(loop.ws.evals.count(('c0002', 'synthesis')), 1)
        self.assertTrue(loop.ws.rtl('c0001').startswith('// Test license'))

    def test_stale_parent_never_downgrades_current_incumbent(self):
        loop = self.start([proposal(), proposal(edits=[{'op': 'set', 'dimension': 'shifter', 'value': 'reverse_right'}])])
        loop.run()
        a = loop.state['attempts'][1]
        self.assertEqual(a['parent'], 'c0000')
        self.assertEqual(a['comparison_incumbent'], 'c0001')
        self.assertEqual(a['outcome'], 'NO_GAIN')
        self.assertEqual(loop.state['incumbent'], 'c0001')

    def test_foreign_parent_is_charged_without_tools(self):
        loop = self.start([proposal(parent='other-loop/c0000')])
        loop.run()
        self.assertEqual(len(loop.state['attempts']), 1)
        self.assertIn('validated typed pool', loop.state['attempts'][0]['correctness'])
        self.assertEqual(loop.ws.order, ['c0000'])

    def test_bad_edit_is_charged_and_cannot_silently_be_corrected(self):
        bad = proposal(edits=[{'op': 'set', 'dimension': 'addsub_share', 'value': FULL + ['MUL']}])
        loop = self.start([bad])
        loop.run()
        self.assertIn('unknown, absent or repeated', loop.state['attempts'][0]['correctness'])
        self.assertEqual(loop.ws.order, ['c0000'])

    def test_duplicate_typed_candidate_is_charged_without_rerun(self):
        loop = self.start([proposal(), proposal()])
        loop.run()
        self.assertEqual(loop.state['attempts'][1]['outcome'], 'DUPLICATE')
        self.assertEqual(loop.usage()['model_requests'], 2)
        self.assertEqual(loop.ws.order, ['c0000', 'c0001'])

    def test_typed_mode_forbids_submit(self):
        loop = self.start([{'action': 'submit', 'rtl': alu.render(IR, ORIGIN)}], mode='typed')
        loop.run()
        self.assertIn('disabled in typed mode', loop.state['attempts'][0]['correctness'])

    def test_freeform_incumbent_is_not_assumed_structured(self):
        free = alu.render(IR, dict(ORIGIN, addsub_share=FULL))
        loop = self.start([{'action': 'submit', 'rtl': free}, proposal(parent='c0001')])
        loop.run()
        ctx = json.loads(self.prompts()[1][len(generation.HEADER):].split('\n', 1)[0])
        self.assertEqual(ctx['incumbent'], 'c0001')
        self.assertNotIn('c0001', ctx['parents'])
        self.assertIn('validated typed pool', loop.state['attempts'][1]['correctness'])

    def test_corrupt_parent_binding_stops_before_generation(self):
        loop = self.start([proposal()])
        loop.run(max_steps=3)
        loop.state['candidates']['c0000']['generation']['rtl_sha256'] = 'bad'
        with self.assertRaisesRegex(GateError, 'binding changed'):
            loop.generation_context()

    def test_renderer_change_refuses_resume(self):
        loop = self.start([proposal()])
        with patch('s2g.generation.verify_manifest', side_effect=GateError('frozen renderer changed')):
            with self.assertRaisesRegex(GateError, 'frozen renderer'):
                loop.run()

    def test_typed_response_crash_recovery_never_resends(self):
        loop = self.start([proposal()])
        original = loop.goto
        def crash(state, **why):
            if state == 'FREEZE_CANDIDATE':
                raise Crash('before atomic attempt commit')
            original(state, **why)
        with patch.object(loop, 'goto', side_effect=crash), self.assertRaises(Crash):
            loop.run()
        resumed = controller.Loop(self.root, 'L', provider.Scripted([]), self.ws)
        resumed.run()
        self.assertEqual(resumed.usage()['model_requests'], 1)
        self.assertEqual(resumed.state['attempts'][0]['outcome'], 'PROMOTED')
        self.assertEqual(resumed.state['candidates']['c0001']['generation']['config']['addsub_share'], FULL)

    def test_compound_actions_have_distinct_dimensions_and_bounded_count(self):
        loop = self.start([proposal()])
        binding = loop.man['generation']
        parents = {'c0000': generation.annotation(binding)}
        with self.assertRaisesRegex(GateError, 'only once'):
            generation.decode(binding, proposal(edits=[{'op': 'set', 'dimension': 'shifter', 'value': 'reverse_right'},
                {'op': 'set', 'dimension': 'shifter', 'value': 'reverse_left'}]), parents)
        binding = copy.deepcopy(binding)
        binding['settings']['max_edits_per_proposal'] = 1
        with self.assertRaisesRegex(GateError, 'edit count'):
            generation.decode(binding, proposal(edits=[{'op': 'set', 'dimension': 'shifter', 'value': 'reverse_right'},
                {'op': 'set', 'dimension': 'select', 'value': 'and_or'}]), parents)

    def test_negative_synthesis_slack_candidate_can_win_routed_selection(self):
        loop = self.start([proposal(edits=[{'op': 'set', 'dimension': 'addsub_share', 'value': FULL},
                    {'op': 'set', 'dimension': 'shifter', 'value': 'reverse_left'}])], routed=True)
        loop.run()
        a = loop.state['attempts'][0]
        self.assertFalse(a['promoted'])
        self.assertEqual(a['outcome'], 'REJECTED_GATE')
        self.assertEqual(loop.state['incumbent'], 'c0001')
        self.assertEqual(loop.state['final']['label'], 'ACCEPTED_IMPLEMENTATION')
        self.assertEqual(loop.state['final']['selection']['online_incumbent'], 'c0000')
        self.assertEqual(loop.state['final']['selection']['rows']['c0001']['gain_rel'], .4)

    def test_a2_never_gets_routed_values_in_structural_context(self):
        loop = self.start([proposal(), proposal()], routed=True)
        loop.run()
        for text in self.prompts():
            ctx = json.loads(text[len(generation.HEADER):].split('\n', 1)[0])
            self.assertNotIn('risk', json.dumps(ctx))
            self.assertNotIn('area_um2', json.dumps(ctx))
            self.assertNotIn('detailed_route_extraction', text)

    def test_a3_gets_routed_feedback_with_same_parent_context(self):
        loop = self.start([proposal(), proposal()], routed=True, arm='A3')
        loop.run()
        text = self.prompts()[1]
        self.assertIn('area_um2', text)
        ctx = json.loads(text[len(generation.HEADER):].split('\n', 1)[0])
        self.assertNotIn('area_um2', json.dumps(ctx))

    def test_missing_pool_coverage_cannot_be_certified(self):
        loop = self.start([proposal()], routed=True)
        loop.run(max_steps=3)
        s = self.ws.snapshot('c0000', loop.state['candidates']['c0000']['synth_run'])
        sel = controller.routed_pool_selection(loop.policy, 'c0000', {'c0000': s})
        self.assertTrue(sel['unmet_requirements'])
        conf = {'selection': sel, 'repeats': {}, 'physical': {}}
        label, missing = controller.final_label(loop.policy, 'c0000', 'c0000', conf)
        self.assertEqual(label, 'SYNTHESIS_PROVISIONAL')
        self.assertTrue(missing)

    def test_routed_pool_policy_rejects_incomplete_schedule_before_baseline_import(self):
        loop = self.start([proposal()], routed=True)
        p = copy.deepcopy(loop.policy)
        p['physical']['search_quota'] = 0
        path = Path(self.tmp.name) / 'badpolicy.json'; write(path, p)
        with self.assertRaisesRegex(GateError, 'every eligible'):
            controller.init(self.root, 'bad', 'A2', path, provider.Scripted([]), self.ws)
        self.assertFalse((self.root / 'loops/bad').exists())

    def test_offline_random_controls_use_controller_without_paid_calls(self):
        t = generation.RandomTransport('random_independent_sampling', 17)
        loop = self.start([], mode='typed', routed=True, transport=t, n=4)
        loop.run()
        self.assertEqual(len(loop.state['attempts']), 4)
        self.assertTrue(all(a['generation']['primitive_edits'] <= 4 for a in loop.state['attempts']))
        self.assertFalse(loop.man['provider']['paid'])
        self.assertIn(loop.state['final']['label'], ('ACCEPTED_IMPLEMENTATION', 'ORIGINAL_RETAINED'))

    def test_random_draws_are_stateless_and_ignore_metrics(self):
        loop = self.start([proposal()])
        loop.run(max_steps=3)
        ctx = loop.generation_context()
        for strategy in generation.STRATEGIES:
            a = generation.random_proposal(ctx, strategy, 17)
            b = generation.random_proposal(dict(ctx, feedback={'area': 1e9}), strategy, 17)
            self.assertEqual(a, b)
        walk = generation.random_proposal(ctx, generation.STRATEGIES[0], 17)
        self.assertEqual(len(walk['edits']), 1)

    def test_measured_table_projection_matches_actual_controller_draws(self):
        configs = alu.space(IR, VOCAB)
        table = {}
        for i, cfg in enumerate(configs):
            self.ws.recs['t'] = {'text': alu.render(IR, cfg)}
            area, ws, _, _, fp = self.ws.values('t')
            table[alu.config_id(cfg)] = {'status': 'COMPLETE', 'functional': 'PASS', 'mapped_proof': 'PASS',
                                       'area_um2': area, 'setup_ws_ns': ws, 'fingerprint': fp}
        del self.ws.recs['t']
        golden = table[alu.config_id(ORIGIN)]
        for strategy in generation.STRATEGIES:
            with self.subTest(strategy=strategy):
                # Each control gets a fresh workspace, as registered for real experiments.
                self.root = Path(self.tmp.name) / strategy
                self.ws = AluWS(self.root)
                loop = self.start([], mode='typed', n=4, transport=generation.RandomTransport(strategy, 17))
                loop.run()
                area = self.ws.values(loop.state['incumbent'])[0]
                projected = baselines.simulate_controller(table, configs, golden, ORIGIN, loop.policy['objective'], 4, [17])
                self.assertEqual(projected['controls'][strategy]['mean_gain'], round((100 - area) / 100, 6))
                self.assertEqual(projected['controls'][strategy]['mean_primitive_edits'],
                                 loop.summarize()['counters']['primitive_edits'])

    def test_original_retained_in_routed_pool_still_requires_repeat_confirmation(self):
        loop = self.start([proposal()], routed=True)
        conf = {'selection': {'unmet_requirements': []}, 'repeats': {},
                'physical': {'c0000': {'acceptance': 'ACCEPTED', 'parasitics': 'detailed_route_extraction'}}}
        label, missing = controller.final_label(loop.policy, 'c0000', 'c0000', conf)
        self.assertEqual(label, 'SYNTHESIS_PROVISIONAL')
        self.assertTrue(any(m.startswith('repeat') for m in missing))


class StrictActions(unittest.TestCase):
    def test_action_parser_rejects_hidden_mistakes(self):
        for val in (['ADD', 'SUB', 'MUL'], ['ADD', 'SUB', 'SUB'], ['ADD', 'SUB', {}]):
            with self.assertRaises(GateError):
                alu.apply(IR, VOCAB, ORIGIN, {'op': 'set', 'dimension': 'addsub_share', 'value': val})
        with self.assertRaises(GateError):
            alu.apply(IR, VOCAB, ORIGIN, {'op': 'set', 'dimension': 'shifter', 'value': 'reverse_right', 'rtl': 'evil'})


if __name__ == '__main__':
    unittest.main()
