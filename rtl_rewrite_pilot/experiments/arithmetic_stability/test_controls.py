import contextlib,io,re,shutil,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import experiment as e
import guard,cohort
ROOT=e.PILOT/'work/arithmetic_stability_v1'
@unittest.skipUnless(all((ROOT/f'{d}-rep1/protocol.json').is_file() for d in e.DESIGNS),
                     'Historical arithmetic archive is excluded from source-only checkouts')
class Controls(unittest.TestCase):
    def test_scope(self):
        for design in e.DESIGNS:
            e.configure(design,1);body=e.block_of((ROOT/f'{design}-rep1/rtl/B0/dut.v').read_text());body=re.sub(r'//[^\n]*','',body)
            self.assertTrue(guard.check(body)[0],guard.check(body))
            for bad in ['module Bad; endmodule','always @* begin A=0; product=0; end','always @* begin while(1) product=0; end','always @* begin for(int i=0;i<8000;i=i+1) product=0; end','always @* begin product=$random; end', 'always @* (* keep=1 *) product=0;']:
                self.assertFalse(guard.check(bad)[0],bad)
        e.configure('cla32',1);self.assertFalse(guard.check('assign c1=g1; assign c2=g2; assign c3=g3; assign c0=g4;')[0])
    def test_frozen_config_and_prompt_isolation(self):
        cohort.verify(ROOT)
        old=e.read_json(e.PILOT/'work/mux9_flash_nothinking_v1/protocol.json')['llm'];self.assertEqual(old,e.SETTINGS)
        for task in cohort.TASKS:
            r=ROOT/task['id'];b1=e.read_json(r/'dryrun/B1.json');b2=e.read_json(r/'dryrun/B2.json')
            self.assertEqual(b1['shared'],b2['shared']);self.assertEqual(b1['user'],b1['shared']);self.assertFalse(b1['feedback_refs'])
            self.assertEqual([x['id'] for x in b2['feedback_refs']],['B0'])
            self.assertNotIn('T0-clean',b2['user'])
    def test_failed_calls_consume_no_retry_and_B1_allowlist(self):
        e.configure('cla32',1);src=ROOT/'cla32-rep1'
        with tempfile.TemporaryDirectory(prefix='arith-control-') as td:
            r=Path(td)
            for part in ('frozen','rtl','feedback'):shutil.copytree(src/part,r/part)
            for f in ('protocol.json','baseline.json'):shutil.copyfile(src/f,r/f)
            e.write_json(r/'state.json',{'status':'PREPARED','attempts':[],'incumbents':{'B1':'B0','B2':'B0'},'closed_rounds':[],'reserved_cny':0})
            rd=r/'runs/B0-synth-1';rd.mkdir(parents=True);shutil.copyfile(src/'runs/B0-synth-1/synth_result.json',rd/'synth_result.json')
            calls=[]
            def fake(req):
                calls.append(req);self.assertEqual(len(e.read_json(r/'state.json')['attempts']),len(calls));self.assertEqual(req['thinking'],{'type':'disabled'})
                return {'error':'offline simulated error','http_status':500,'response':None,'elapsed_s':0}
            with patch.object(e,'verify',return_value=e.read_json(r/'protocol.json')),patch.object(e,'balance',return_value={'total_balance':10}),patch.object(e,'load_liberty',return_value={}),patch.object(e,'call_provider',side_effect=fake),patch.object(e.screen,'simulate',return_value={'status':'TB_PASSED'}),patch.object(e.screen,'formal',return_value={'status':'PASS'}),patch.object(e,'audit'),patch('urllib.request.urlopen',side_effect=AssertionError('network forbidden')),contextlib.redirect_stdout(io.StringIO()):
                e.live(r);e.live(r)
            self.assertEqual(len(calls),4)
            rec=e.read_attempt(r,'B1-r1-a1');rec.update(reason='SYNTH_SECRET',synth={'status':'SYNTH_DONE','metrics':{'area':12345678}});e.write_json(r/'attempts/B1-r1-a1/record.json',rec)
            prompt,_,refs=e.make_prompt(r,'B1',2,2);self.assertNotIn('SYNTH_SECRET',prompt);self.assertNotIn('12345678',prompt);self.assertFalse(refs)
if __name__=='__main__':unittest.main()
