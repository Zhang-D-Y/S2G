"""Fair physical exposure and confirmed physical outcome regressions."""
import json
from pathlib import Path

from s2g import controller, experiment, provider, feedback
from s2g.common import GateError, digest, files_manifest, read, write
from test_loop import ALT, SMALL, GOLD, CONFIRM, Crash, FakeWS, LoopBase, evidence, policy, submit


class PhysicalComparisonTests(LoopBase):
    def failed_placement_fixture(self):
        root = (self.root / 'failed-placement').resolve()
        run = root / 'candidates/c0000/run-001'
        (run / 'backend').mkdir(parents=True)
        rtl = run.parent / 'rtl.v'
        rtl.write_text(GOLD)
        write(root / 'frozen/spec.json', {})
        write(root / 'frozen/context.json', {})
        write(root / 'freeze.json', {'files': {}})
        rec = {'rtl_hash': digest(rtl), 'freeze_hash': digest(root / 'freeze.json'), 'last_run': run.name}
        write(run.parent / 'record.json', rec)
        log = run / 'backend/place.log'
        log.write_text('[ERROR GPL-0302] Use a higher -density or re-floorplan with a larger core area.\n'
                       'Given target density: 0.40\nSuggested target density: 0.42\n')
        write(str(log) + '.job.json', {'exit_code': 1, 'timed_out': False, 'log': str(log),
                                     'log_sha256': digest(log)})
        write(run / 'ledger.json', {'candidate': 'c0000', 'run': run.name, 'rtl_hash': rec['rtl_hash'],
                                   'freeze_hash': rec['freeze_hash'], 'status': 'FAILED',
                                   'functional': {'status': 'PASS'}, 'synthesis': {'mapped_proof': 'PASS'},
                                   'stages': {'floorplan': {'status': 'COMPLETE'}},
                                   'error': 'tool failed: ' + str(log)})
        write(run / 'synthesis_evidence.json', dict(evidence(12), candidate='c0000', run=run.name,
                                                   rtl_sha256=rec['rtl_hash']))
        write(run / 'evidence.json', {'files': files_manifest([p for p in run.rglob('*') if p.is_file()])})
        return root, run, log

    def test_early_physical_failure_is_mandatory_for_a3_and_hidden_from_a2(self):
        root, run, log = self.failed_placement_fixture()
        snap = feedback.snapshot(root, 'c0000')
        self.assertEqual(snap['physical_failure']['stage'], 'placement')
        self.assertIn('GPL-0302', snap['physical_failure']['reason'])
        self.assertIn('Given target density: 0.40', snap['physical_failure']['reason'])
        self.assertIn('Suggested target density: 0.42', snap['physical_failure']['reason'])
        self.assertIsNone(snap['physical'])
        base = dict(snap, candidate='baseline', run='run-000', status='COMPLETE', error=None,
                    physical_failure=None)
        loop = {'loop_id': 'L', 'manifest_sha256': 'm', 'toolchain_id': 't', 'policy': policy(),
                'corner': 'typical'}
        for arm in ('A2', 'A3'):
            packet = feedback.build_packet(loop=loop, arm=arm, incumbent=base, baseline=base,
                                           recent=[], budget={}, caps={'max_issue_examples': 1},
                                           recent_candidate=snap)
            text, dropped = feedback.render(packet, 1100)
            self.assertNotIn('final_route', packet['recent_candidate']['metrics'])
            if arm == 'A3':
                self.assertEqual(packet['recent_candidate']['stage'], 'physical')
                self.assertEqual(packet['recent_candidate']['blocking_categories'], ['physical'])
                self.assertIn('GPL-0302', text)
                self.assertIn('Suggested target density: 0.42', text)
                self.assertGreater(dropped, 0)
                with self.assertRaises(GateError):
                    feedback.render(packet, 100)
            else:
                self.assertNotIn('GPL-0302', text)
                self.assertNotIn('target density', text)
                self.assertNotIn('physical', packet['recent_candidate']['blocking_categories'])

    def test_failed_backend_job_cannot_point_to_a_foreign_log(self):
        root, run, log = self.failed_placement_fixture()
        receipt = Path(str(log) + '.job.json')
        j = read(receipt)
        j['log'] = str(root.parent / 'foreign.log')
        write(receipt, j)
        write(run / 'evidence.json', {'files': files_manifest([p for p in run.rglob('*')
                                                             if p.is_file() and p.name != 'evidence.json'])})
        with self.assertRaises(GateError):
            feedback.snapshot(root, 'c0000')

    def test_wildcard_sensitivity_is_allowed_without_allowing_attributes(self):
        for sensitivity in ('@*', '@(*)', '@ (*)', '@( * )', '@ (* )'):
            rtl = f'module m(input a, output reg y); always {sensitivity} y=a; endmodule'
            self.assertIsNone(controller.static_check(rtl, 'm', 1000), sensitivity)
        for attribute in ('(* keep = 1 *)', '(* dont_touch *)', '(* *)'):
            rtl = f'module m(input a, output y); {attribute} wire z=a; assign y=z; endmodule'
            self.assertIn('attribute', controller.static_check(rtl, 'm', 1000))
        rtl = 'module m(input a, output y); assign y=$signed(a); endmodule'
        self.assertIn('$signed', controller.static_check(rtl, 'm', 1000))

    def test_equal_stage_schedule_and_first_prompt_exposure(self):
        schedules = {}
        for arm in ('A2', 'A3'):
            with self.subTest(arm=arm):
                root = self.root / arm
                ws = FakeWS(root)
                cfg = policy(physical={'baseline': True, 'search_schedule': 'first_eligible',
                                       'search_quota': 3, 'final': True},
                             physical_evaluations=5, candidate_submissions=3)
                path = Path(self.tmp.name) / (arm + '.json')
                write(path, cfg)
                transport = provider.Scripted([submit(SMALL), submit(ALT), {'content': '{"action":"stop"}'}])
                controller.init(root, 'L', arm, path, transport, ws=ws)
                loop = controller.Loop(root, 'L', transport, ws=ws)
                loop.run(max_steps=2)
                self.assertEqual(ws.evals, [('c0000', 'synthesis'), ('c0000', 'finish')])
                self.assertEqual(loop.usage()['model_requests'], 0)
                loop.run()
                requests = sorted((root / 'loops/L/proposals').glob('*/request-*/request.json'))
                prompts = [read(p)['payload']['messages'][1]['content'] for p in requests]
                if arm == 'A3':
                    self.assertIn('final_route', prompts[0])
                    self.assertIn('0.4', prompts[0])
                    self.assertIn('-0.3', prompts[2])
                    self.assertIn('acceptance gate setup', prompts[2])
                else:
                    self.assertNotIn('final_route', '\n'.join(prompts))
                    self.assertNotIn('acceptance gate setup', '\n'.join(prompts))
                self.assertEqual([a['outcome'] for a in loop.state['attempts']],
                                 ['PROMOTED', 'NO_GAIN', 'MODEL_STOPPED'])
                self.assertEqual(loop.state['incumbent'], 'c0001')
                schedules[arm] = ws.evals
        self.assertEqual(schedules['A2'], schedules['A3'])

    def test_unqualified_physical_baseline_never_requests_a_model(self):
        old = self.ws.physical

        def rejected(cid, run):
            result = old(cid, run)
            if result is not None:
                result['acceptance']['status'] = 'REJECTED'
            return result

        self.ws.physical = rejected
        loop = self.start([submit(SMALL)], candidate_submissions=1,
                          physical={'baseline': True, 'search_schedule': 'none', 'final': True})
        loop.run()
        self.assertEqual(loop.state['state'], 'STOPPED')
        self.assertIn('baseline_not_qualified: routed acceptance', loop.state['stop_reason'])
        self.assertEqual(loop.usage()['model_requests'], 0)

    def test_completed_physical_baseline_is_not_repeated_after_crash(self):
        loop = self.start([submit(SMALL)], candidate_submissions=1,
                          physical={'baseline': True, 'search_schedule': 'none', 'final': True})
        goto = loop.goto

        def crash(dst, **why):
            if dst == 'BUILD_BASELINE_FEEDBACK':
                raise Crash('after physical baseline completion')
            return goto(dst, **why)

        loop.goto = crash
        with self.assertRaises(Crash):
            loop.run()
        resumed = controller.Loop(self.root, 'L', loop.transport, ws=self.ws)
        resumed.run()
        self.assertEqual(self.ws.evals.count(('c0000', 'finish')), 1)
        self.assertEqual(self.ws.evals.count(('c0000', 'synthesis')), 1 + CONFIRM)
        self.assertEqual(resumed.state['final']['label'], 'ACCEPTED_IMPLEMENTATION')

    def test_routed_gain_requires_acceptance_and_full_confirmation(self):
        final = {'label': 'ACCEPTED_IMPLEMENTATION', 'unmet_requirements': []}
        phys = {k: {'area_um2': a, 'parasitics': 'detailed_route_extraction'}
                for k, a in [('baseline', 100), ('winner', 75)]}
        self.assertEqual(experiment.routed_gain(final, phys), 0.25)
        for label in ('PHYSICALLY_REJECTED', 'SYNTHESIS_PROVISIONAL', None):
            self.assertIsNone(experiment.routed_gain(dict(final, label=label), phys))
        self.assertIsNone(experiment.routed_gain(dict(final, unmet_requirements=['repeat:c1:missing']), phys))
        self.assertIsNone(experiment.routed_gain(final, {'baseline': phys['baseline']}))
        bad = json.loads(json.dumps(phys)); bad['winner']['parasitics'] = 'placement_estimate'
        self.assertIsNone(experiment.routed_gain(final, bad))
        bad = json.loads(json.dumps(phys)); bad['winner']['area_um2'] = None
        self.assertIsNone(experiment.routed_gain(final, bad))
        phys['winner']['area_um2'] = 100
        self.assertEqual(experiment.routed_gain(dict(final, label='ORIGINAL_RETAINED'), phys), 0.0)
