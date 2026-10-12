#!/usr/bin/env python3
"""Previously tested mul8: original and balanced tree through full GDS acceptance."""
import argparse
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from s2g import workspace,actions,pipeline,graphs,search
from s2g.common import read,write,require,worst_slack
from qualify_lvs import qualify


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',required=True,type=Path)
    p.add_argument('--context',type=Path,default=Path(__file__).resolve().parents[1]/'configs/local-nangate45.json')
    a=p.parse_args();b=Path(__file__).resolve().parents[1];e=b/'examples/mul8';root=a.root.resolve()
    workspace.init(root,e/'spec.json',e/'golden.v',e/'tb.v',e/'constraints.sdc',a.context)
    base=workspace.add(root,e/'golden.v');candidate=actions.propose(root,read(e/'balanced.json'),base['id'])
    results=[]
    for rec in (base,candidate):
        cid=rec['id'];r=pipeline.evaluate(root,cid);run,_,ledger=pipeline.run_evidence(root,cid)
        r['graph_checks']=[graphs.validate(p) for p in ledger.get('graphs',[])]
        if (run/'risk.json').is_file():
            risk=read(run/'risk.json');r['metrics']={'area_um2':risk['area_um2'],'setup_ws':worst_slack(risk,'setup'),
                'hold_ws':worst_slack(risk,'hold'),'repair_edits':risk['repair']['net_edits']}
        if r['acceptance']['status']=='ACCEPTED':
            r['lvs_controls']=qualify(run,a.context,root/'qualification'/cid)['status']
        results.append(r);write(root/'SMOKE.json',{'schema':'s2g.smoke.v1','model_calls':0,'results':results})
        print(cid,r['status'],r['acceptance']['status'],r.get('metrics'),flush=True)
    success=all(r['status']=='COMPLETE' and r['acceptance']['status']=='ACCEPTED' and r.get('lvs_controls')=='PASS' and len(r['graph_checks'])==4 for r in results)
    write(root/'pool.json',search.pool(root));write(root/'splits.json',{'pilot_mul8':'dev'})
    search.dataset(root,root/'dataset.json',root/'splits.json')
    write(root/'feedback.json',search.feedback(root,candidate['id'],'graph'))
    report=read(root/'SMOKE.json');report['status']='PASS' if success else 'FAIL';write(root/'SMOKE.json',report)
    return 0 if success else 1

if __name__=='__main__':sys.exit(main())
