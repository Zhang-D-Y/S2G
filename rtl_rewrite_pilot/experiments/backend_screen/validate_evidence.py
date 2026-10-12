#!/usr/bin/env python3
"""Exercise real routed equivalence rejection and fail-closed evidence handling."""
import argparse
import copy
from pathlib import Path
import shutil
import sys
import tempfile
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from experiments.backend_screen import analyze as a
from experiments.backend_screen.screen import verify_frozen

def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--root',type=Path,required=True);args=ap.parse_args();root=args.root.resolve()
    verify_frozen(root)
    source=root/'runs/cla32-original-s17'
    # Real all-binary-input proof must reject corruption of a routed input buffer.
    control=root/'controls/operand_zero';net=control/'work/results/sky130hd/cla32/base/screen_post_route.v';net.parent.mkdir(parents=True,exist_ok=True)
    text=(source/'work/results/sky130hd/cla32/base/screen_post_route.v').read_text()
    assert text.count('.A(A[1])')==1
    net.write_text(text.replace('.A(A[1])',".A(1'b0)",1))
    proof=a.prove_routed(root,control,'cla32','original')
    assert proof['status']=='FAIL',proof
    a.write_json(control/'control.json',{'mutation':'one input buffer A[1] tied to zero in an isolated routed-netlist copy','expected':'FAIL','observed':proof['status']})
    checks={'routed_corruption_rejected':True}
    original_read=a.read_json
    # The fixture uses read-only symlinked flow output. All mutations and parser
    # output are restricted to the disposable fixture, never to measured runs.
    with tempfile.TemporaryDirectory(prefix='backend-evidence-') as tmp:
        tr=Path(tmp);run=tr/'fixture';run.mkdir()
        for name in ('work','flow.log','config.mk'):(run/name).symlink_to(source/name)
        (run/'routed_equivalence').mkdir()
        job=original_read(source/'job.json');proof=original_read(source/'routed_equivalence/result.json')
        def reset():
            a.write_json(run/'job.json',job)
            a.write_json(run/'routed_equivalence/result.json',proof)
        reset();checks['complete_positive_evidence']=a.read_run(tr,run)['status']=='MEASURED'
        for name,delta in [('failed_exit',{'exit_code':1}),('timeout',{'exit_code':None,'timed_out':True})]:
            reset();changed=copy.deepcopy(job);changed['job'].update(delta);a.write_json(run/'job.json',changed)
            checks[name+'_rejected']=a.read_run(tr,run)['status']=='INCOMPLETE'
        reset();a.write_json(run/'routed_equivalence/result.json',dict(proof,status='FAIL'))
        checks['failed_proof_rejected']=a.read_run(tr,run)['status']=='INCOMPLETE'
        for mode in ('missing_drc','no_path'):
            reset()
            def altered(path):
                d=original_read(path)
                if Path(path).name=='5_2_route.json':
                    if mode=='missing_drc':d.pop('detailedroute__route__drc_errors',None)
                    else:d['screen_post_route__timing__setup__ws']=1e39
                return d
            with patch.object(a,'read_json',side_effect=altered):
                checks[mode+'_rejected']=a.read_run(tr,run)['status']=='INCOMPLETE'
    assert all(checks.values()),checks
    a.write_json(root/'controls/evidence_checks.json',{'status':'PASS','checks':checks,'validator_sha256':a.sha256_file(__file__)})
    print(checks)
if __name__=='__main__':main()
