import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from s2g.common import GateError,identity,write,read,job,digest,verify_manifest
from s2g.contracts import review
from s2g.pipeline import acceptance
from s2g.graphs import validate
from s2g.workspace import reserve,submit,abort
from s2g.search import dominates

BASE=Path(__file__).resolve().parents[1]

class Gates(unittest.TestCase):
    def test_review_requires_exact_hash_and_resolved_questions(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'spec.json';s=read(BASE/'examples/adder8/spec.json');write(p,s)
            with self.assertRaises(GateError):review(p,'user','wrong')
            s['open_questions']=['reset?'];write(p,s)
            with self.assertRaises(GateError):review(p,'user',identity(s))
    def test_missing_signoff_never_passes(self):
        c=read(BASE/'configs/local-nangate45.json')
        r=acceptance({}, {}, {}, c)
        self.assertEqual(r['status'],'REJECTED')
        self.assertFalse(r['gates']['lvs'])
    def test_unknown_electrical_metric_blocks_acceptance(self):
        c=read(BASE/'configs/local-nangate45.json')
        l={'status':'COMPLETE','functional':{'status':'PASS'},'synthesis':{'mapped_proof':'PASS'},'postroute_equivalence':{'status':'PASS'},'stages':{'finish':{'status':'COMPLETE'}}}
        risk=dict(setup_wns=1,hold_wns=1,slew_violations=0,cap_violations=None,fanout_violations=0)
        sig={k:{'status':'PASS'} for k in ('gds','drc','lvs')}
        self.assertEqual(acceptance(l,risk,sig,c)['status'],'REJECTED')
        risk['cap_violations']=0
        self.assertEqual(acceptance(l,risk,sig,c)['status'],'ACCEPTED')
    def test_timeout_kills_tool_group(self):
        with tempfile.TemporaryDirectory() as d:
            r=job(['/usr/bin/python3','-c','import subprocess,time; subprocess.Popen(["sleep","30"]); time.sleep(30)'],d,Path(d)/'log',0.1)
            self.assertTrue(r['timed_out']);self.assertIsNone(r['exit_code']);self.assertLess(r['elapsed_s'],3)
    def test_artifact_tamper_detected(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'a';p.write_text('a');man={str(p):digest(p)};p.write_text('b')
            with self.assertRaises(GateError):verify_manifest(man)
    def test_future_stage_and_missing_masks_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'graph.json'
            g={'schema':'s2g.stage-graph.v1','prediction_target':'floorplan','available_after':'finish','sources':{},'nodes':[],'edges':[]}
            write(p,g)
            with self.assertRaises(GateError):validate(p)
            g['available_after']='synthesis';g['nodes']=[{'id':'a','kind':'cell','x_um':1,'y_um':2,'placement_valid':False}];write(p,g)
            with self.assertRaises(GateError):validate(p)
    def test_labels_not_features(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'g.json';write(p,{'schema':'s2g.stage-graph.v1','prediction_target':'cts','available_after':'placement','risk':{},'sources':{},'nodes':[],'edges':[]})
            with self.assertRaises(GateError):validate(p)
    def test_failed_attempt_consumes_budget(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);write(root/'freeze.json',{});write(root/'state.json',{'attempts':[]})
            v=({'design_family':'f'},{'candidate_budget':1},{'spec_hash':'s','context_hash':'c'})
            with patch('s2g.workspace.verify',return_value=v):
                cid=reserve(root);abort(root,cid,'model produced no candidate')
                with self.assertRaises(GateError):reserve(root)
    def test_no_unknown_pareto_objectives(self):
        self.assertTrue(dominates([1,-2,3],[2,-1,4]))
        self.assertFalse(dominates([1,4,3],[2,1,4]))
    def test_runtime_script_paths_are_local(self):
        c=read(BASE/'configs/local-nangate45.json')
        self.assertNotIn('r2g_root',c)
        for key in ('graph_parser_dir','typed_rules','typed_guard'):
            self.assertTrue(Path(c[key]).resolve().is_relative_to(BASE.parent))
        provenance=read(Path(c['graph_parser_dir']).parent/'PROVENANCE.json')
        for name,sha in provenance['files'].items():
            self.assertEqual(digest(Path(c['graph_parser_dir']).parent/name),sha)

if __name__=='__main__':unittest.main()
