import contextlib
import io
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import experiment as e
import guard
import cohort

ROOT=e.PILOT/'work/mux_stability_v2'

class Controls(unittest.TestCase):
    def test_design_specific_scope(self):
        for name in e.DESIGNS:
            e.configure(name,1);root=ROOT/f'{name}-rep1'
            self.assertTrue(guard.check(e.block_of((root/'rtl/B0/dut.v').read_text()))[0],name)
            for bad in ['always @(*) out=$random;', 'always @(*) begin sel=0; out=a; end','always @(*) out=a+b;', 'always @(*) out=a; module bad; endmodule']:
                self.assertFalse(guard.check(bad)[0],(name,bad))
        e.configure('mux4',1)
        self.assertFalse(guard.check("always @(*) q=c;")[0])
        self.assertTrue(guard.check("always @(*) if(c==0) q=b; else q=4'hf;")[0])
        e.configure('mux6',1)
        self.assertFalse(guard.check("always @(*) out=4'hf;")[0])
        self.assertFalse(guard.check("always @(*) if(sel[3]) out=data0; else out=0;")[0])

    def test_initial_isolation_and_frozen_configuration(self):
        cohort.verify(ROOT)
        old=e.read_json(e.PILOT/'work/mux9_flash_nothinking_v1/protocol.json')['llm']
        self.assertEqual(e.SETTINGS,old)
        for task in cohort.TASKS:
            r=ROOT/task['id'];b1=e.read_json(r/'dryrun/B1.json');b2=e.read_json(r/'dryrun/B2.json')
            self.assertEqual(b1['shared'],b2['shared']);self.assertEqual(b1['user'],b1['shared'])
            self.assertFalse(b1['feedback_refs']);self.assertEqual([x['id'] for x in b2['feedback_refs']],['B0'])

    def test_failures_count_no_retry_and_B1_synthesis_isolation(self):
        e.configure('mux6',1);src=ROOT/'mux6-rep1'
        with tempfile.TemporaryDirectory(prefix='mux-stability-test-') as td:
            root=Path(td)
            for name in ('frozen','rtl','feedback'):shutil.copytree(src/name,root/name)
            for name in ('protocol.json','baseline.json'):shutil.copyfile(src/name,root/name)
            e.write_json(root/'state.json',{'status':'PREPARED','attempts':[],'incumbents':{'B1':'B0','B2':'B0'},'closed_rounds':[],'reserved_cny':0})
            rd=root/'runs/B0-synth-1';rd.mkdir(parents=True);shutil.copyfile(src/'runs/B0-synth-1/synth_result.json',rd/'synth_result.json')
            calls=[]
            def fake(req):
                calls.append(req);s=e.read_json(root/'state.json');self.assertEqual(len(calls),len(s['attempts']))
                self.assertEqual(req['thinking'],{'type':'disabled'});self.assertEqual(req['temperature'],1)
                return {'error':'simulated failure','http_status':500,'response':None,'elapsed_s':0}
            with patch.object(e,'verify',return_value=e.read_json(root/'protocol.json')),patch.object(e,'balance',return_value={'total_balance':10}),patch.object(e,'load_liberty',return_value={}),patch.object(e,'call_provider',side_effect=fake),patch.object(e.screen,'simulate',return_value={'status':'TB_PASSED'}),patch.object(e.screen,'formal',return_value={'status':'PASS'}),patch.object(e,'audit'),patch('urllib.request.urlopen',side_effect=AssertionError('network forbidden')),contextlib.redirect_stdout(io.StringIO()):
                e.live(root);e.live(root)
            self.assertEqual(len(calls),4)
            rec=e.read_attempt(root,'B1-r1-a1');rec.update(synth={'status':'SYNTH_DONE','metrics':{'area':12345678}},decision='SYNTH_SECRET',reason='REASON_SECRET');e.write_json(root/'attempts/B1-r1-a1/record.json',rec)
            prompt,shared,refs=e.make_prompt(root,'B1',2,2)
            for sentinel in ('12345678','SYNTH_SECRET','REASON_SECRET'):self.assertNotIn(sentinel,prompt)
            self.assertFalse(refs)

if __name__=='__main__':unittest.main()
