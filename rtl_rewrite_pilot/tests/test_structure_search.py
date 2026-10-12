"""Safety and controller regressions; no network or EDA tools invoked here."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rtltrial.search import engine, rules, adapter
from rtltrial.util import read_json, write_json, sha256_file


class Rules(unittest.TestCase):
    def test_tree_operations_preserve_exact_operands_and_width(self):
        e=adapter.load('mul8')
        for shape in ('balanced','left','right'):
            s,b=rules.apply('mul8',rules.initial('mul8'),{'op':'sum_tree','params':{'shape':shape,'order':list(reversed(range(8)))}})
            self.assertEqual(sorted(rules.leaves(s['tree'])),list(range(8)))
            self.assertEqual(s['width'],16)
            self.assertEqual(b.count("=== 1'b1"),8)
            self.assertTrue(e.check(b)[0])
        a={'op':'rotate','params':{'path':[],'direction':'right'}}
        s,_=rules.apply('mul8',rules.initial('mul8'),a)
        t,_=rules.apply('mul8',s,{'op':'rotate','params':{'path':[],'direction':'left'}})
        self.assertEqual(t,rules.initial('mul8'))

    def test_bad_operations_cannot_emit_code(self):
        bad=[{'op':'sum_tree','params':{'shape':'balanced','order':[0]*8}},
             {'op':'sum_tree','params':{'shape':'balanced','order':[False,1,2,3,4,5,6,7]}},
             {'op':'rotate','params':{'path':[9],'direction':'right'}},
             {'op':'rotate','params':{'path':[0]*7,'direction':'right'}},
             {'op':'sum_tree','params':{},'block':'module evil; endmodule'}]
        for action in bad:
            with self.assertRaises(ValueError):rules.apply('mul8',rules.initial('mul8'),action)
        with self.assertRaises(ValueError):rules.apply('cla32',rules.initial('cla32'),{'op':'carry_factor','params':{'groups':[1,2,3,5]}})

    def test_carry_variants_stay_in_original_grammar(self):
        e=adapter.load('cla32')
        for action in rules.demo_actions('cla32')[:-1]:
            _,block=rules.apply('cla32',rules.initial('cla32'),action)
            self.assertTrue(e.check(block)[0],block)


class Controller(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.source=adapter.PILOT/'work/arithmetic_stability_v1/mul8-rep1'
        for name in ('rtl/B0/dut.v','frozen/mul8_test.sv','baseline.json',
                     'feedback/B0.json','runs/B0-synth-1/synth_result.json'):
            dst=self.root/name;dst.parent.mkdir(parents=True,exist_ok=True);dst.write_bytes((self.source/name).read_bytes())
        self.arm={'attempts':[],'success_pool':['B0'],'repair_pool':[], 'best':'B0','expansions':{},'pending':None}
        self.state={'status':'PREPARED','arms':{'B1':copy.deepcopy(self.arm),'B2':copy.deepcopy(self.arm)}}
        self.man={'design':'mul8','generation':'structured','generation_catalog':rules.catalog('mul8'),
                  'budget_per_arm':2,'pool_size':4,'repair_pool_size':2,'model':engine.MODEL,
                  'max_input_bytes':engine.INPUT_LIMIT,'arms':['B1','B2']}
        write_json(self.root/'state.json',self.state)

    def verify(self,root,**kwargs):
        return self.man,read_json(root/'state.json')

    def test_matched_prompts_and_functional_allowlist(self):
        r={'id':'B1-001','proposal':{'op':'rotate','params':{'path':[],'direction':'right'}},
           'reason':'SYNTH_SECRET','decision':'KEEP','synth':{'metrics':{'area':987654321}},
           'formal':{'status':'PASS','internal_secret':'SYNTH_SECRET'}}
        write_json(self.root/'attempts/B1-001/record.json',r)
        arm=copy.deepcopy(self.arm);arm['attempts']=[{'id':r['id']}];arm['repair_pool']=[r['id']]
        a,ma=engine.make_request(self.root,self.man,'B1',arm,'B0',2)
        b,mb=engine.make_request(self.root,self.man,'B2',arm,'B0',2)
        self.assertEqual(ma['shared_sha256'],mb['shared_sha256'])
        self.assertEqual(b['messages'][1]['content'].split('\nSYNTHESIS_FEEDBACK')[0],a['messages'][1]['content'])
        self.assertNotIn('SYNTH_SECRET',json.dumps(a));self.assertNotIn('987654321',json.dumps(a))
        self.assertEqual(ma['feedback_refs'],[]);self.assertEqual(mb['feedback_refs'][0]['id'],'B0')
        self.assertEqual(a['thinking'],{'type':'disabled'})

    def test_completed_feedback_tampering_detected(self):
        engine.checked_feedback(self.root,'B0')
        p=self.root/'runs/B0-synth-1/synth_result.json'
        p.write_text(p.read_text()+' ')
        with self.assertRaisesRegex(ValueError,'evidence changed'):engine.checked_feedback(self.root,'B0')

    def test_failed_calls_consume_slots_no_automatic_retry(self):
        with patch.object(engine,'verify',side_effect=self.verify),patch('urllib.request.urlopen',side_effect=AssertionError('network forbidden')):
            engine.reserve(self.root,'B1')
            self.assertEqual(len(read_json(self.root/'state.json')['arms']['B1']['attempts']),1)
            with self.assertRaisesRegex(ValueError,'pending'):engine.reserve(self.root,'B1')
            r=engine.submit(self.root,'B1',error='offline failed response')
            self.assertEqual(r['decision'],'INCONCLUSIVE')
            engine.reserve(self.root,'B1');engine.submit(self.root,'B1','{"type":"json_object"}')
            with self.assertRaisesRegex(ValueError,'exhausted'):engine.reserve(self.root,'B1')
            self.assertEqual(len(read_json(self.root/'state.json')['arms']['B1']['repair_pool']),2)

    def test_interrupted_attempt_never_retried(self):
        with patch.object(engine,'verify',side_effect=self.verify):
            r=engine.reserve(self.root,'B1');r['status']='RUNNING'
            write_json(self.root/'attempts'/r['id']/'record.json',r)
            with self.assertRaisesRegex(ValueError,'already started'):engine.submit(self.root,'B1','{}')
            engine.abort(self.root,'B1')
            self.assertEqual(len(read_json(self.root/'state.json')['arms']['B1']['attempts']),1)
            engine.reserve(self.root,'B1')

    def test_nonimproving_candidate_is_explorable_not_final(self):
        rec={'id':'B1-001','decision':'EXPLORE','structure':{'groups':[1,1,1,1]},'synth':{'metrics':{'area':3000}}}
        write_json(self.root/'attempts/B1-001/record.json',rec)
        arm=copy.deepcopy(self.arm)
        engine.update_pools(self.root,arm,rec,self.man)
        self.assertIn(rec['id'],arm['success_pool']);self.assertEqual(arm['best'],'B0')
        arm['expansions']['B0']=1;arm['attempts']=[{'id':rec['id']}]
        self.assertEqual(engine.choose_parent(self.root,arm),rec['id'])
        rec['decision']='COLLAPSED';rec['id']='B1-002'
        engine.update_pools(self.root,arm,rec,self.man)
        self.assertNotIn(rec['id'],arm['success_pool'])

    def test_freeform_uses_same_request_settings_and_evaluator(self):
        self.man['generation']='freeform'
        block=rules.apply('mul8',rules.initial('mul8'),rules.demo_actions('mul8')[0])[1]
        def evaluate(root,rec,man):
            self.assertEqual(rec['block'],block)
            self.assertEqual(man['model'],engine.MODEL)
            rec.update(decision='REJECT',reason='offline evaluator stub')
        with patch.object(engine,'verify',side_effect=self.verify),patch.object(engine,'evaluate',side_effect=evaluate) as evaluator:
            r=engine.reserve(self.root,'B1')
            req=read_json(self.root/'attempts'/r['id']/'request.json')
            self.assertNotIn('parent_structure',req['messages'][1]['content'])
            self.assertIn('selected_region',req['messages'][1]['content'])
            engine.submit(self.root,'B1',json.dumps({'block':block}))
            evaluator.assert_called_once()

    def test_untrusted_proposal_cannot_inject_tool_costs(self):
        self.man['schema']='test';self.man['code_hashes']={}
        r={'id':'B1-001','parent':'B0','status':'COMPLETE','decision':'INCONCLUSIVE',
           'proposal':{'jobs':[{'elapsed_s':1000000000}]},
           'usage':{'jobs':[{'elapsed_s':1000000000}]}}
        write_json(self.root/'attempts/B1-001/record.json',r)
        self.state['arms']['B1']['attempts']=[{'id':r['id']}]
        write_json(self.root/'state.json',self.state)
        with patch.object(engine,'verify',side_effect=self.verify):
            summary=engine.summarize(self.root)
        baseline_jobs=adapter.load('mul8').tool_jobs(read_json(self.root/'baseline.json'))
        self.assertEqual(summary['tool_invocations'],len(baseline_jobs))
        self.assertLess(summary['tool_wall_s'],1000000)

    def test_request_mutation_stops_submission(self):
        with patch.object(engine,'verify',side_effect=self.verify):
            r=engine.reserve(self.root,'B1')
            write_json(self.root/'attempts'/r['id']/'request.json',{'altered':True})
            with self.assertRaisesRegex(ValueError,'request changed'):engine.submit(self.root,'B1','{}')
            self.assertIsNotNone(read_json(self.root/'state.json')['arms']['B1']['pending'])


if __name__=='__main__':unittest.main()
