"""Transport-independent candidate requests, physical Pareto pool, and lineage splits."""
from pathlib import Path
from .common import *
from .workspace import verify, reserve
from .pipeline import run_evidence
from .graphs import validate as validate_graph

def feedback(root,cid,mode='physical'):
    run,rec,ledger=run_evidence(root,cid)
    report={'schema':'s2g.feedback.v1','candidate':cid,'run':run.name,'rtl_hash':rec['rtl_hash'],
            'status':ledger['status'],'error':ledger.get('error'),'functional':ledger.get('functional'),
            'synthesis':ledger.get('synthesis'),'mode':mode}
    if mode in ('physical','graph'):
        report['measured_physical']=read(run/'risk.json') if (run/'risk.json').exists() else {'status':'UNKNOWN'}
        report['acceptance']=read(run/'acceptance.json')
    if mode=='graph':
        report['graph_summaries']=[]
        report['graph_error']=ledger.get('graph_error')
        for path in sorted(ledger.get('graphs',[])):
            validate_graph(path)
            g=read(path);counts={}
            for e in g['edges']:counts[e['net']]=counts.get(e['net'],0)+1
            report['graph_summaries'].append({'sample_id':g['sample_id'],'target':g['prediction_target'],'available_after':g['available_after'],
                'coverage':g['coverage'],'high_degree_nets':sorted(counts.items(),key=lambda x:(-x[1],x[0]))[:8],
                'interpretation':'topology degree; not measured congestion or a calibrated risk prediction'})
    return report

def request(root,parent,mode='physical'):
    root=Path(root);spec,ctx,freeze=verify(root)
    fb=feedback(root,parent,mode) if parent else None
    cid=reserve(root,parent,{'op':'external_proposal','feedback_mode':mode})
    payload={'schema':'s2g.generation-request.v1','candidate':cid,'spec':spec,
             'parent_rtl':(root/'candidates'/parent/'rtl.v').read_text() if parent else (root/'frozen/reference.v').read_text(),
             'feedback':fb,'technology':{'platform':ctx['platform'],'corner':ctx['corner'],'dont_use':ctx['dont_use']},
             'constraints_sha256':digest(root/'frozen/constraints.sdc'),'response_format':'one complete Verilog module, submitted with s2g submit',
             'instruction':'Preserve exact interface, binary function and cycle behavior. Do not edit tests, spec, SDC or physical constraints.',
             'provider':{'transport':'file','network_calls':0,'model':None,'thinking':False}}
    path=root/'candidates'/cid/'request.json';write(path,payload)
    return {'candidate':cid,'request':str(path),'status':'AWAITING_EXTERNAL_RESPONSE','paid_calls':0}

def dominates(a,b):
    return all(x<=y for x,y in zip(a,b)) and any(x<y for x,y in zip(a,b))

def pool(root):
    root=Path(root);verify(root);success=[];repair=[];pending=[];seen={}
    for cid in read(root/'state.json')['attempts']:
        rec=read(root/'candidates'/cid/'record.json')
        if 'last_run' not in rec:pending.append(cid);continue
        run,rec,ledger=run_evidence(root,cid);acc=read(run/'acceptance.json')
        syn=ledger.get('synthesis',{}).get('netlist_sha256')
        duplicate=seen.get(syn) if syn else None
        if syn:seen.setdefault(syn,cid)
        risk=read(run/'risk.json') if (run/'risk.json').exists() else {}
        slack=worst_slack(risk,'setup')
        vals=[risk.get('area_um2'),-slack if slack is not None else None,risk.get('repair',{}).get('net_edits')]
        item={'candidate':cid,'acceptance':acc['status'],'objectives':vals,'synthesis_duplicate_of':duplicate}
        if acc['status']=='ACCEPTED' and all(v is not None for v in vals):success.append(item)
        else:repair.append(item)
    pareto=[a for a in success if not any(dominates(b['objectives'],a['objectives']) for b in success if a!=b)]
    return {'schema':'s2g.pool-view.v1','pareto':pareto,'validated':success,'repair_queue':repair,'pending':pending,
            'objectives':['min final instance area','min negative final setup slack','min net repair edits'],
            'note':'Only complete, accepted candidates have feasible Pareto status; unknown values are never zero-filled.'}

def dataset(root,out,split):
    root=Path(root);verify(root);assign=read(split);rows=[]
    for cid in read(root/'state.json')['attempts']:
        rec=read(root/'candidates'/cid/'record.json');family=rec['design_family']
        require(assign.get(family) in ('train','dev','test'),'family must have explicit split assignment')
        for r in sorted((root/'candidates'/cid).glob('run-*')):
            run,_,ledger=run_evidence(root,cid,r.name)
            rows.append({'candidate_record':rec,'run':str(run),'split':assign[family],
                         'ledger':ledger,'acceptance':read(run/'acceptance.json'),
                         'labels':read(run/'labels.json') if (run/'labels.json').exists() else None,
                         'evidence_sha256':digest(run/'evidence.json')})
    write(out,{'schema':'s2g.dataset-manifest.v1','split_policy':'same source family and descendants stay together; aliases must be canonicalized before import',
               'rows':rows,'pending_attempts':[x for x in read(root/'state.json')['attempts'] if not list((root/'candidates'/x).glob('run-*'))]})
    return {'path':str(out),'runs':len(rows)}
