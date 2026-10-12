"""Regression witnesses found while running the previously tested mul8 design."""
import contextlib
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from s2g.common import read,write,digest,GateError,worst_slack
from s2g import graphs,pipeline
from s2g.backend import measure
from s2g.__main__ import main

BASE=Path(__file__).resolve().parents[1]

class SmokeRegressions(unittest.TestCase):
    def test_worst_slack_and_wns_are_distinct_with_legacy_support(self):
        with tempfile.TemporaryDirectory() as d:
            run=Path(d);ctx={'platform':'test'}
            write(run/'backend/work/logs/test/design/base/6_report.json',
                  {'finish__timing__setup__ws':5,'finish__timing__hold__ws':-0.1})
            p=run/'stages/finish/export.log';p.parent.mkdir(parents=True)
            p.write_text('S2G_SLEW_COUNT 0\nS2G_CAP_COUNT 0\nS2G_FANOUT_COUNT 0\n')
            risk=measure(run,run/'unused_results',ctx)
            self.assertEqual(risk['setup_ws'],5);self.assertEqual(risk['setup_wns'],0)
            self.assertEqual(risk['hold_wns'],-0.1)
            self.assertEqual(worst_slack(risk,'setup'),5)
            self.assertEqual(worst_slack({'schema':'s2g.risk.v1','setup_wns':5},'setup'),5)

    def test_graph_failure_closes_run_and_acceptance(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);p=root/'candidates/c0000';p.mkdir(parents=True)
            write(root/'freeze.json',{});(p/'rtl.v').write_text('module top; endmodule')
            write(p/'record.json',{'id':'c0000','status':'SUBMITTED','rtl_hash':digest(p/'rtl.v'),'freeze_hash':digest(root/'freeze.json')})
            c=read(BASE/'configs/local-nangate45.json')
            def synthesis(root,run,*args):
                write(run/'mapped.json',{});return {'status':'PASS','mapped_proof':'PASS'}
            def backend(root,run,spec,ctx,ledger):
                ledger['stages']['finish']={'status':'COMPLETE'}
                ledger['postroute_equivalence']={'status':'PASS'}
                return ({'setup_wns':1,'hold_wns':1,'slew_violations':0,'cap_violations':0,'fanout_violations':0},
                        {k:{'status':'PASS'} for k in ('gds','drc','lvs')})
            with patch('s2g.pipeline.verify',return_value=({},c,{})), \
                 patch('s2g.pipeline.functional',return_value={'status':'PASS'}), \
                 patch('s2g.pipeline.synth',side_effect=synthesis), \
                 patch('s2g.pipeline.run_backend',side_effect=backend), \
                 patch('s2g.graphs.build',side_effect=GateError('bad graph')):
                result=pipeline.evaluate(root,'c0000')
            self.assertEqual(result['status'],'FAILED')
            self.assertEqual(result['acceptance']['status'],'REJECTED')

    def cli(self,args,result):
        with patch('sys.argv',['s2g']+args),patch('s2g.pipeline.evaluate',return_value=result),contextlib.redirect_stdout(io.StringIO()):
            return main()

    def test_batch_propagates_tool_failure(self):
        self.assertEqual(self.cli(['batch','--root','unused','--candidates','c0000'],{'status':'FAILED'}),1)

    def test_finish_rejection_has_distinct_nonzero_exit(self):
        self.assertEqual(self.cli(['run','--root','unused','--candidate','c0000'],
            {'status':'COMPLETE','acceptance':{'status':'REJECTED'}}),3)

    def test_synthesis_only_does_not_require_signoff(self):
        self.assertEqual(self.cli(['run','--root','unused','--candidate','c0000','--until','synthesis'],
            {'status':'COMPLETE','acceptance':{'status':'REJECTED'}}),0)

    def test_failure_cannot_be_accepted_with_stale_positive_gates(self):
        c=read(BASE/'configs/local-nangate45.json')
        l={'status':'FAILED','functional':{'status':'PASS'},'synthesis':{'mapped_proof':'PASS'},
           'postroute_equivalence':{'status':'PASS'},'stages':{'finish':{'status':'COMPLETE'}}}
        risk={'setup_wns':1,'hold_wns':1,'slew_violations':0,'cap_violations':0,'fanout_violations':0}
        sig={k:{'status':'PASS'} for k in ('gds','drc','lvs')}
        self.assertEqual(pipeline.acceptance(l,risk,sig,c)['status'],'REJECTED')

    def test_same_rtl_distinct_candidates_have_distinct_graph_ids(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);write(root/'freeze.json',{'r2g_commit':'test'})
            ids=[];fake=SimpleNamespace(load_liberty_db=lambda _: {'cells':{}},macro_sizes=lambda _: {})
            for cid in ('c0000','c0001'):
                run=root/'candidates'/cid/'run-000'
                write(run.parent/'record.json',{'id':cid,'freeze_hash':'f','rtl_hash':'r','spec_hash':'s','context_hash':'c'})
                mod={'ports':{'a':{'direction':'input','bits':[2]}},'cells':{},'netnames':{}}
                for name in ('mapped.json','candidate.json'):write(run/name,{'modules':{'top':mod}})
                with patch('s2g.graphs.parser',return_value=fake):
                    graphs.build(root,run,{'top':'top'},{'platform_dir':'/unused','liberty':'x','cell_lef':'y'},
                                 {'stages':{},'status':'COMPLETE'})
                ids.append(read(run/'graphs/floorplan.json')['sample_id'])
            self.assertNotEqual(*ids)

    def test_backend_failure_preserves_completed_measurements(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);p=root/'candidates/c0000';p.mkdir(parents=True)
            write(root/'freeze.json',{});(p/'rtl.v').write_text('module top; endmodule')
            write(p/'record.json',{'id':'c0000','status':'SUBMITTED','rtl_hash':digest(p/'rtl.v'),'freeze_hash':digest(root/'freeze.json')})
            c=read(BASE/'configs/local-nangate45.json')
            def fails(root,run,*args):
                write(run/'risk.json',{'schema':'s2g.risk.v1','setup_wns':1,'hold_wns':1})
                raise GateError('checker failed after timing completed')
            with patch('s2g.pipeline.verify',return_value=({},c,{})), \
                 patch('s2g.pipeline.functional',return_value={'status':'PASS'}), \
                 patch('s2g.pipeline.synth',return_value={'status':'PASS','mapped_proof':'PASS'}), \
                 patch('s2g.pipeline.run_backend',side_effect=fails):
                result=pipeline.evaluate(root,'c0000')
            self.assertEqual(result['status'],'FAILED')
            self.assertTrue(result['acceptance']['gates']['setup'])
            self.assertEqual(result['acceptance']['status'],'REJECTED')

if __name__=='__main__':unittest.main()
