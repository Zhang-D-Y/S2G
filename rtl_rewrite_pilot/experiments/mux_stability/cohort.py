#!/usr/bin/env python3
"""Frozen 3 designs x 3 independent repeats x 2 arms x 2 attempts study."""
import argparse
import csv
import fcntl
import json
from pathlib import Path
import statistics
import experiment as e
from designs import DESIGNS

TASKS=[{'design':d,'repeat':n,'id':f'{d}-rep{n}'} for n in (1,2,3) for d in DESIGNS]

def prepare(root):
    if root.exists(): raise SystemExit('choose a fresh cohort directory')
    root.mkdir(parents=True)
    plan={'schema':'rtltrial.mux_stability.v1','frozen_at':e.now(),'designs':DESIGNS,'tasks':TASKS,
          'selection':'all three original exact-selector, constant-default, single-output procedural data muxes in pinned VerilogEval; chosen before paid calls; no synthetic size variants',
          'settings':e.SETTINGS,'calls':36,'reserved_cny':36*e.CALL_RESERVE,'ceiling_cny':5,
          'repeats':'independent provider draws; no supported seed claimed; no cross-repeat or cross-design history',
          'order':'interleave designs within repeat; reverse arm order in repeat 2',
          'primary':'new-design stratum only; final retained area improvement per arm, all failures included',
          'stability_rule':'B2 beats B1 in at least 2/3 repeats on EACH new design, with positive mean paired improvement on EACH; otherwise not stable in this cohort',
          'limits':'two new small mux designs plus one DEV replication; no timing or P&R, no broad generalization or guaranteed statistical independence',
          'supplemental':'after calls, each non-baseline retained version gets mapped equivalence and two synth repeats; failures invalidate the study claim',
          'code_hashes':{str(p.relative_to(e.PILOT)):e.sha256_file(p) for p in e.sources()}}
    plan['contract_hash']=e.sha256_json(plan);e.write_json(root/'plan.json',plan)
    (root/'plan.json').chmod(0o444)
    for task in TASKS:
        print('PREPARE',task['id'],flush=True)
        e.configure(task['design'],task['repeat']);e.prepare(root/task['id'])
        b1=e.read_json(root/task['id']/'dryrun/B1.json');b2=e.read_json(root/task['id']/'dryrun/B2.json')
        assert b1['shared']==b2['shared'] and b1['user']==b1['shared'] and not b1['feedback_refs']
        assert b2['user'].startswith(b1['shared']+'\nSYNTHESIS_FEEDBACK_FROM_COMPLETED_RUNS\n')
    e.write_json(root/'preflight.json',{'status':'PASS','baselines':9,'negative_controls':9,'initial_prompt_isolation':'PASS','paid_calls':0,'protocol_hashes':{t['id']:e.sha256_file(root/t['id']/'protocol.json') for t in TASKS}})

def verify(root):
    plan=e.read_json(root/'plan.json');assert e.sha256_json({k:v for k,v in plan.items() if k!='contract_hash'})==plan['contract_hash']
    assert plan['tasks']==TASKS and plan['settings']==e.SETTINGS
    assert plan['reserved_cny']<=plan['ceiling_cny']
    for p,h in plan['code_hashes'].items(): assert e.sha256_file(e.PILOT/p)==h
    pre=e.read_json(root/'preflight.json');assert pre['status']=='PASS'
    for t in TASKS:
        e.configure(t['design'],t['repeat']);e.verify(root/t['id'])
        assert e.sha256_file(root/t['id']/'protocol.json')==pre['protocol_hashes'][t['id']]
    return plan

def supplemental(root):
    records=[]
    for t in TASKS:
        e.configure(t['design'],t['repeat']);r=root/t['id'];m=e.verify(r);s=e.read_json(r/'state.json')
        for cid in sorted(set(s['incumbents'].values())-{'B0'}):
            out=r/f'{cid}-supplemental.json'
            if out.exists(): records.append(e.read_json(out));continue
            rec=e.read_attempt(r,cid);lib=e.load_liberty(m['liberty'],r/'cache')
            rep=[e.screen.synth(r,cid,m['tools'],(r/'frozen/synth.ys').read_text(),lib,n) for n in (2,3)]
            metrics=[rec['synth']['metrics']]+[x.get('metrics',{}) for x in rep]
            stable=all(x.get('status')=='SYNTH_DONE' and x.get('check_clean') for x in rep) and len({(x.get('area'),x.get('netlist_sha256')) for x in metrics})==1
            proof=rec.get('large_drop_audit') or e.gate_audit(r,cid,m)
            jobs=e.tool_jobs(rep)+(e.tool_jobs(proof) if not rec.get('large_drop_audit') else [])
            result={'task':t['id'],'candidate':cid,'status':'PASS' if stable and proof['status']=='PASS' else 'FAIL','stable_netlist_3_repeats':stable,'mapped_equivalence':proof,'repeats':rep,'additional_tool_invocations':len(jobs),'additional_tool_wall_s':sum(x['elapsed_s'] for x in jobs),'not_fed_to_models':True}
            e.write_json(out,result);records.append(result)
    e.write_json(root/'supplemental.json',{'status':'PASS' if all(x['status']=='PASS' for x in records) else 'FAIL','records':records})

def summarize(root):
    verify(root);rows=[];attempts=[];cost=0;complete=0
    for t in TASKS:
        r=root/t['id'];a=e.read_json(r/'audit.json');assert a['status']=='PASS';cost+=a['peak_price_cost_cny']
        b1=a['arms']['B1'];b2=a['arms']['B2'];delta=b2['improvement_vs_B0_pct']-b1['improvement_vs_B0_pct']
        rows.append({**t,'stratum':DESIGNS[t['design']]['stratum'],'baseline_area':a['baseline_area'],'B1_area':b1['area'],'B2_area':b2['area'],'B1_gain_pct':b1['improvement_vs_B0_pct'],'B2_gain_pct':b2['improvement_vs_B0_pct'],'B2_minus_B1_gain_pp':delta,'winner':'B2' if delta>1e-9 else ('B1' if delta< -1e-9 else 'TIE')})
        s=e.read_json(r/'state.json')
        for at in s['attempts']:
            rec=e.read_attempt(r,at['id']);complete+=int(rec.get('finish_reason')=='stop' and bool(rec.get('block')))
            attempts.append({'task':t['id'],'id':at['id'],'arm':at['arm'],'round':at['round'],'decision':rec['decision'],'reason':rec.get('reason'),'finish_reason':rec.get('finish_reason'),'area':rec.get('synth',{}).get('metrics',{}).get('area'),'functional_pass':rec.get('formal',{}).get('status')=='PASS'})
    designs={}
    for d in DESIGNS:
        rs=[x for x in rows if x['design']==d];ds=[x['B2_minus_B1_gain_pp'] for x in rs]
        designs[d]={'stratum':DESIGNS[d]['stratum'],'B1_mean_gain_pct':statistics.mean(x['B1_gain_pct'] for x in rs),'B2_mean_gain_pct':statistics.mean(x['B2_gain_pct'] for x in rs),'B2_wins':sum(x['winner']=='B2' for x in rs),'B1_wins':sum(x['winner']=='B1' for x in rs),'ties':sum(x['winner']=='TIE' for x in rs),'paired_delta_mean_pp':statistics.mean(ds),'paired_delta_range_pp':[min(ds),max(ds)]}
    stable=all(x['B2_wins']>=2 and x['paired_delta_mean_pp']>0 for x in designs.values() if x['stratum']=='new_design')
    sup=e.read_json(root/'supplemental.json');assert sup['status']=='PASS'
    out={'status':'COMPLETE','frozen_stability_rule_met':stable,'designs':designs,'calls':len(attempts),'complete_candidates':complete,'usage_peak_cost_cny':cost,'supplemental_status':sup['status'],'rows':rows,'limitations':'3 repeats per design; 2 new small mux designs; DEV separate; not a broad causal/statistical generalization; no P&R/timing/power'}
    assert len(attempts)==36
    e.write_json(root/'summary.json',out)
    for name,rs in [('summary.csv',rows),('attempts.csv',attempts)]:
        with (root/name).open('w',newline='') as f:
            w=csv.DictWriter(f,fieldnames=list(rs[0]));w.writeheader();w.writerows(rs)
    print(json.dumps(out,indent=2),flush=True)

def live(root):
    with (root/'live.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        verify(root)
        for t in TASKS:
            print('LIVE',t['id'],flush=True);e.configure(t['design'],t['repeat'])
            if e.read_json(root/t['id']/'state.json')['status']=='COMPLETE':continue
            e.live(root/t['id'])
        supplemental(root);summarize(root)

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('command',choices=['prepare','live','summarize']);ap.add_argument('--root',type=Path,required=True);a=ap.parse_args()
    globals()[a.command](a.root.resolve())
