from pathlib import Path
import time
from .common import *
from .workspace import verify, locked
from .verify import functional, synth
from .backend import run_backend
from . import graphs

def acceptance(ledger,risk,signoff,ctx):
    gates={'run_complete':ledger.get('status')=='COMPLETE' and not ledger.get('graph_error'),
           'functional':ledger.get('functional',{}).get('status')=='PASS',
           'mapped_equivalence':ledger.get('synthesis',{}).get('mapped_proof')=='PASS',
           'postroute_equivalence':ledger.get('postroute_equivalence',{}).get('status')=='PASS',
           'finish':ledger.get('stages',{}).get('finish',{}).get('status')=='COMPLETE',
           'gds':signoff.get('gds',{}).get('status')=='PASS'}
    for k in ('setup','hold'):
        v=worst_slack(risk,k);gates[k]=v is not None and v>=ctx['acceptance'].get(k+'_ws_min',ctx['acceptance'].get(k+'_wns_min',0))
    for k in ('slew_violations','cap_violations','fanout_violations'):
        gates[k]=risk.get(k) is not None and risk[k]==0
    for k in ('drc','lvs'):
        if ctx['acceptance'].get('require_'+k):gates[k]=signoff.get(k,{}).get('status')=='PASS'
    return {'schema':'s2g.acceptance.v1','status':'ACCEPTED' if all(gates.values()) else 'REJECTED',
            'gates':gates,'qualification':'research contract only; single corner; no foundry signoff, CDC, DFT, IR or activity-qualified power claim'}

def evaluate(root,cid,until='finish'):
    root=Path(root).resolve();spec,ctx,freeze=verify(root)
    with locked(root):
        p=root/'candidates'/cid;rec=read(p/'record.json')
        require(rec['status'] in ('SUBMITTED','EVALUATED','FAILED'),'candidate is not ready or another run is active')
        require(digest(p/'rtl.v')==rec['rtl_hash'],'candidate RTL changed')
        require(rec['freeze_hash']==digest(root/'freeze.json'),'freeze identity changed')
        total=sum(len(list(d.glob('run-*'))) for d in (root/'candidates').iterdir())
        require(total<ctx.get('tool_run_budget',ctx['candidate_budget']*3),'tool run budget exhausted; failures count')
        n=len(list(p.glob('run-*')));run=p/('run-%03d'%n);run.mkdir()
        rec['status']='RUNNING';write(p/'record.json',rec)
    code=files_manifest(tree_files(Path(__file__).parent));start=time.monotonic()
    ledger={'schema':'s2g.run.v1','candidate':cid,'run':run.name,'status':'RUNNING','stages':{},
            'rtl_hash':rec['rtl_hash'],'freeze_hash':rec['freeze_hash'],'started_at':stamp(),'implementation':code}
    write(run/'ledger.json',ledger);risk={};signoff={}
    try:
        ledger['functional']=functional(root,run,p/'rtl.v',spec,ctx);write(run/'ledger.json',ledger)
        ledger['synthesis']=synth(root,run,p/'rtl.v',spec,ctx);write(run/'ledger.json',ledger)
        if until=='finish':risk,signoff=run_backend(root,run,spec,ctx,ledger)
        ledger['status']='COMPLETE'
    except (GateError,OSError,ValueError,KeyError) as e:
        ledger.update(status='FAILED',error=str(e))
    finally:
        # A later checker can fail after measurements were durably written.
        # Retain those observations while the run-complete gate stays closed.
        if (run/'risk.json').is_file():risk=read(run/'risk.json')
        if (run/'signoff.json').is_file():signoff=read(run/'signoff.json')
        ledger['elapsed_s']=round(time.monotonic()-start,3);ledger['completed_at']=stamp()
        write(run/'ledger.json',ledger)
        if (run/'mapped.json').is_file() and ledger.get('synthesis',{}).get('status')=='PASS':
            try:ledger['graphs']=graphs.build(root,run,spec,ctx,ledger)
            except (GateError,OSError,ValueError,KeyError) as e:
                ledger.update(graph_error=str(e),status='FAILED',error='graph validation failed: '+str(e))
        ledger['elapsed_s']=round(time.monotonic()-start,3);ledger['completed_at']=stamp()
        result=acceptance(ledger,risk,signoff,ctx)
        result.update(candidate=cid,run=run.name,freeze_hash=rec['freeze_hash'],rtl_hash=rec['rtl_hash'])
        write(run/'acceptance.json',result);write(run/'ledger.json',ledger)
        # Hash every evidence artifact; source/graph/review and outputs can be checked before reuse.
        evidence=files_manifest([f for f in tree_files(run) if f.name!='evidence.json'])
        write(run/'evidence.json',{'schema':'s2g.evidence.v1','files':evidence})
        with locked(root):
            rec=read(p/'record.json');rec.update(status='EVALUATED' if ledger['status']=='COMPLETE' else 'FAILED',last_run=run.name)
            write(p/'record.json',rec)
    return {'run':str(run),'status':ledger['status'],'acceptance':result,'error':ledger.get('error')}

def run_evidence(root,cid,run_name=None):
    root=Path(root);verify(root);rec=read(root/'candidates'/cid/'record.json')
    require(digest(root/'candidates'/cid/'rtl.v')==rec['rtl_hash'],'candidate changed')
    run=root/'candidates'/cid/(run_name or rec['last_run'])
    verify_manifest(read(run/'evidence.json')['files']);ledger=read(run/'ledger.json')
    require(ledger['candidate']==cid and ledger['run']==run.name and ledger['rtl_hash']==rec['rtl_hash']
            and ledger['freeze_hash']==rec['freeze_hash']==digest(root/'freeze.json'),'run evidence identity mismatch')
    require(ledger['status'] in ('COMPLETE','FAILED'),'feedback requires terminal run')
    return run,rec,ledger
