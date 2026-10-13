import argparse
import json
from pathlib import Path
import sys
from .common import GateError, identity, read
from . import contracts,workspace,pipeline,graphs,search,actions,pyg,controller,provider,experiment

def main():
    p=argparse.ArgumentParser(description='Evidence-bound spec to GDS research workflow (no implicit provider calls)')
    s=p.add_subparsers(dest='command',required=True)
    q=s.add_parser('doctor');q.add_argument('--context',required=True)
    q=s.add_parser('spec-check');q.add_argument('spec')
    q=s.add_parser('spec-draft');q.add_argument('--requirements',required=True);q.add_argument('--out',required=True)
    q=s.add_parser('review');q.add_argument('spec');q.add_argument('--reviewer',required=True);q.add_argument('--spec-hash',required=True);q.add_argument('--demo',action='store_true')
    q=s.add_parser('init');q.add_argument('--root',required=True)
    for name in ('spec','reference','tb','sdc','context'):q.add_argument('--'+name,required=True)
    q.add_argument('--review')
    q=s.add_parser('add');q.add_argument('--root',required=True);q.add_argument('--rtl',required=True);q.add_argument('--parent')
    q=s.add_parser('submit');q.add_argument('--root',required=True);q.add_argument('--candidate',required=True);q.add_argument('--rtl',required=True)
    q=s.add_parser('run');q.add_argument('--root',required=True);q.add_argument('--candidate',required=True);q.add_argument('--until',choices=['synthesis','finish'],default='finish')
    q.add_argument('--require-graph',action='store_true',help='also fail when the dataset/graph gate fails')
    for name in ('request','feedback'):
        q=s.add_parser(name);q.add_argument('--root',required=True);q.add_argument('--candidate',required=name=='feedback');q.add_argument('--mode',choices=['synthesis','physical','graph'],default='physical')
    q=s.add_parser('pool');q.add_argument('--root',required=True)
    q=s.add_parser('export');q.add_argument('--root',required=True);q.add_argument('--out',required=True);q.add_argument('--splits',required=True)
    q=s.add_parser('graph-check');q.add_argument('graph')
    q=s.add_parser('abort');q.add_argument('--root',required=True);q.add_argument('--candidate',required=True);q.add_argument('--reason',required=True)
    q=s.add_parser('propose');q.add_argument('--root',required=True);q.add_argument('--action',required=True);q.add_argument('--parent')
    q=s.add_parser('batch');q.add_argument('--root',required=True);q.add_argument('--candidates',nargs='+',required=True);q.add_argument('--repeats',type=int,default=1);q.add_argument('--until',choices=['synthesis','finish'],default='finish')
    q.add_argument('--require-graph',action='store_true')
    q=s.add_parser('loop-init',help='freeze a finite optimization loop over an existing workspace')
    q.add_argument('--root',required=True);q.add_argument('--loop',required=True);q.add_argument('--arm',choices=controller.ARMS,required=True)
    q.add_argument('--policy',default=str(Path(__file__).resolve().parents[1]/'configs/loop-policy-v1.json'))
    g=q.add_mutually_exclusive_group(required=True);g.add_argument('--live',action='store_true',help='paid provider calls');g.add_argument('--scripted',help='JSON file of offline responses')
    q=s.add_parser('loop-run',help='run or resume a loop until it completes or stops')
    q.add_argument('--root',required=True);q.add_argument('--loop',required=True)
    g=q.add_mutually_exclusive_group();g.add_argument('--live',action='store_true');g.add_argument('--scripted')
    q=s.add_parser('loop-status');q.add_argument('--root',required=True);q.add_argument('--loop',required=True)
    q=s.add_parser('exp-init',help='freeze an arm comparison: N independent runs per arm sharing one cost ledger')
    q.add_argument('--root',required=True)
    for name in ('spec','reference','tb','sdc','context','policy','pricing'):q.add_argument('--'+name,required=True)
    q.add_argument('--arms',nargs='+',required=True,choices=controller.ARMS);q.add_argument('--repeats',type=int,required=True)
    q.add_argument('--ceiling',type=float,required=True,help='currency ceiling for all paid requests')
    q.add_argument('--mode',required=True,help="'live' or 'scripted:<responses.json>'")
    for name in ('exp-preflight','exp-run','exp-report'):
        q=s.add_parser(name);q.add_argument('--root',required=True)
    q=s.add_parser('pyg-export')
    for name in ('graph','out','python'):q.add_argument('--'+name,required=True)
    a=p.parse_args()
    try:
        if a.command=='doctor':result=workspace.doctor(a.context)
        elif a.command=='spec-check':result={'spec_hash':contracts.validate_spec(read(a.spec))}
        elif a.command=='spec-draft':result=contracts.draft(a.requirements,a.out)
        elif a.command=='review':result=contracts.review(a.spec,a.reviewer,a.spec_hash,a.demo)
        elif a.command=='init':result=workspace.init(a.root,a.spec,a.reference,a.tb,a.sdc,a.context,a.review)
        elif a.command=='add':result=workspace.add(a.root,a.rtl,a.parent)
        elif a.command=='submit':result=workspace.submit(a.root,a.candidate,a.rtl)
        elif a.command=='run':result=pipeline.evaluate(a.root,a.candidate,a.until)
        elif a.command=='request':result=search.request(a.root,a.candidate,a.mode)
        elif a.command=='feedback':result=search.feedback(a.root,a.candidate,a.mode)
        elif a.command=='pool':result=search.pool(a.root)
        elif a.command=='export':result=search.dataset(a.root,a.out,a.splits)
        elif a.command=='abort':result=workspace.abort(a.root,a.candidate,a.reason)
        elif a.command=='propose':result=actions.propose(a.root,read(a.action),a.parent)
        elif a.command=='batch':
            from .common import require
            require(a.repeats>0,'repeats must be positive')
            result=[pipeline.evaluate(a.root,cid,a.until) for _ in range(a.repeats) for cid in a.candidates]
        elif a.command=='pyg-export':result=pyg.export(a.graph,a.out,a.python)
        elif a.command in ('loop-init','loop-run'):
            policy=read(a.policy) if a.command=='loop-init' else read(Path(a.root)/'loops'/a.loop/'manifest.json')['policy']
            transport=provider.OpenAICompatible(policy['provider']) if a.live else (provider.Scripted(read(a.scripted)['responses']) if a.scripted else None)
            if a.command=='loop-init':result=controller.init(a.root,a.loop,a.arm,a.policy,transport)
            else:
                st=controller.Loop(a.root,a.loop,transport).run()
                result={'state':st['state'],'stop_reason':st.get('stop_reason'),'incumbent':st['incumbent'],'final':st.get('final')}
        elif a.command=='exp-init':
            result=experiment.init(a.root,a.spec,a.reference,a.tb,a.sdc,a.context,a.arms,a.repeats,a.policy,a.ceiling,read(a.pricing),a.mode)
        elif a.command=='exp-preflight':result=experiment.preflight(a.root)
        elif a.command=='exp-run':result=experiment.run(a.root)
        elif a.command=='exp-report':result=experiment.report(a.root)
        elif a.command=='loop-status':
            d=Path(a.root)/'loops'/a.loop;result=read(d/'summary.json') if (d/'summary.json').is_file() else read(d/'state.json')
        else:result=graphs.validate(a.graph)
        print(json.dumps(result,indent=2,ensure_ascii=False,allow_nan=False))
        rows=result if isinstance(result,list) else [result]
        if any(r.get('status') in ('FAILED','FAIL','ERROR','BLOCKED_OR_FAILED') for r in rows):return 1
        if a.command in ('run','batch') and a.until=='finish' and any(r.get('acceptance',{}).get('status')!='ACCEPTED' for r in rows):return 3
        if a.command in ('run','batch') and a.require_graph and any(r.get('acceptance',{}).get('dataset_gates',{}).get('graph')!='PASS' for r in rows):return 1
        if a.command=='loop-run' and result['state']=='STOPPED':return 1
        return 0
    except (GateError,OSError,KeyError,ValueError) as e:
        print(json.dumps({'status':'ERROR','error':str(e)},ensure_ascii=False),file=sys.stderr);return 2

if __name__=='__main__':sys.exit(main())
