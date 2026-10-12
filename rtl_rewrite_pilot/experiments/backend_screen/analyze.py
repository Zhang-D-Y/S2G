#!/usr/bin/env python3
"""Evidence-based backend screen aggregation; missing measurements stay unknown."""
import argparse
import csv
import json
import math
from pathlib import Path
import re
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from rtltrial.util import read_json,write_json,sha256_file,sha256_json,run_job

STAGES=('placement_pre_repair','grt_pre_repair','grt_post_repair','post_route')
PHYSICAL=('sky130_fd_sc_hd__tapvpwrvgnd','sky130_fd_sc_hd__fill','sky130_fd_sc_hd__decap')

def finite(v):
    return v if isinstance(v,(int,float)) and math.isfinite(v) and abs(v)<1e25 else None

def section(text,name):
    m=re.search(r'^.* '+re.escape(name)+r'\n-+\n([^\n]+)',text,re.M)
    if not m:return None
    try:return finite(float(m[1]))
    except ValueError:return None

def snapshot(metrics,label,report):
    prefix='screen_'+label+'__'
    d={k[len(prefix):]:finite(v) for k,v in metrics.items() if k.startswith(prefix)}
    ws=d.get('timing__setup__ws');hold=d.get('timing__hold__ws')
    out={'valid_timing':ws is not None and hold is not None,
         'setup_ws_ns':ws,'setup_wns_ns':min(0,ws) if ws is not None else None,
         'setup_tns_ns':d.get('timing__setup__tns') if ws is not None else None,
         'hold_ws_ns':hold,'hold_tns_ns':d.get('timing__hold__tns') if hold is not None else None,
         'setup_violating_endpoints':d.get('timing__drv__setup_violation_count') if ws is not None else None,
         'hold_violating_endpoints':d.get('timing__drv__hold_violation_count') if hold is not None else None,
         'instance_area_um2':d.get('design__instance__area'),
         'die_area_um2':d.get('design__die__area'),'core_area_um2':d.get('design__core__area')}
    for key,query,metric in [('slew','max_slew','max_slew'),('cap','max_capacitance','max_cap'),('fanout','max_fanout','max_fanout')]:
        bound=section(report,query+'_check_limit')
        out[key+'_limit_present']=bound is not None
        out[key+'_worst_margin']=section(report,query+'_check_slack') if bound is not None else None
        # The native metric counts violations. No limit is not evidence of safety.
        out[key+'_violations']=d.get('timing__drv__'+metric) if bound is not None else None
    return out

def inventory(path):
    with path.open() as f:
        return {r['name']:r for r in csv.DictReader(f,delimiter='\t') if not r['master'].startswith(PHYSICAL)}

def edits(before,after):
    ins=set(after)-set(before);rem=set(before)-set(after)
    changed={k for k in set(before)&set(after) if before[k]['master']!=after[k]['master']}
    return {'inserted':len(ins),'removed':len(rem),'master_changed':len(changed),
            'inserted_buffers':sum('__buf_' in after[k]['master'] or '__clkbuf_' in after[k]['master'] for k in ins),
            'area_delta_um2':round(sum(float(r['area_um2']) for r in after.values())-sum(float(r['area_um2']) for r in before.values()),6)}

def prove_routed(root,run,design,variant):
    rd=run/'routed_equivalence';rd.mkdir(exist_ok=True)
    out=rd/'result.json'
    res=run/'work/results/sky130hd'/design/'base/screen_post_route.v'
    gold=root/'inputs'/design/'runs'/f'{variant}-synth/netlist.v'
    lib=Path('/opt/OpenROAD-flow-scripts/flow/platforms/sky130hd/lib/sky130_fd_sc_hd__tt_025C_1v80.lib')
    hashes={str(p):sha256_file(p) for p in (gold,res,lib)}
    if out.exists():
        prev=read_json(out)
        if prev.get('hashes')==hashes:return prev
        raise ValueError('proof inputs changed')
    ys=f'''read_liberty -ignore_miss_func {lib}
read_verilog {gold}
rename TopModule Gold
read_verilog {res}
proc
opt
miter -equiv -flatten Gold TopModule equiv
prep -top equiv
flatten
sat -verify -prove trigger 0 -set-def-inputs -show-inputs -show-outputs
'''
    (rd/'prove.ys').write_text(ys)
    job=run_job(['/opt/OpenROAD/oss-cad-suite/bin/yosys','-s','prove.ys'],rd,rd/'formal.log',120)
    text=(rd/'formal.log').read_text()
    status='PASS' if job['exit_code']==0 and not job['timed_out'] and 'SAT proof finished - no model found: SUCCESS!' in text else 'UNKNOWN'
    if 'proof did fail' in text:status='FAIL'
    result={'status':status,'scope':'input mapped netlist versus routed netlist, expanded Liberty, all binary inputs','job':job,'hashes':hashes}
    write_json(out,result);return result

def read_run(root,run,prove=False):
    rec=read_json(run/'job.json');design=rec['design'];variant=rec['variant']
    res=run/'work/results/sky130hd'/design/'base';logs=run/'work/logs/sky130hd'/design/'base';reports=run/'work/reports/sky130hd'/design/'base'
    metrics={};evidence={}
    for p in sorted(logs.glob('*.json')):
        metrics.update(read_json(p));evidence[str(p.relative_to(root))]=sha256_file(p)
    snapshots={}
    for label in STAGES:
        p=reports/f'0_screen_{label}.rpt'
        snapshots[label]=snapshot(metrics,label,p.read_text() if p.exists() else '')
        if p.exists():evidence[str(p.relative_to(root))]=sha256_file(p)
    events=[]
    p=res/'screen_repair_events.tsv'
    if p.exists():
        for row in csv.reader(p.open(),delimiter='\t'):
            label,ms,code=row[:3]
            bef=res/f'screen_{label}_before.instances.tsv';aft=res/f'screen_{label}_after.instances.tsv'
            event={'label':label,'elapsed_ms':int(ms),'exit_code':int(code),'args':row[3] if len(row)>3 else ''}
            event['net_edits']=edits(inventory(bef),inventory(aft)) if bef.exists() and aft.exists() else None
            events.append(event)
    pins={}
    for label in STAGES:
        p=res/f'screen_{label}.pins.tsv'
        if p.exists():pins[label]=sha256_json(sorted(p.read_text().splitlines()))
    final_netlist=res/'screen_post_route.v'
    proof=prove_routed(root,run,design,variant) if prove and final_netlist.exists() else read_json(run/'routed_equivalence/result.json') if (run/'routed_equivalence/result.json').exists() else {'status':'NOT_RUN'}
    if proof.get('status')=='PASS' and (len(proof.get('hashes',{}))!=3 or any(not Path(p).is_file() or sha256_file(p)!=h for p,h in proof.get('hashes',{}).items())):
        proof=dict(proof,status='STALE')
    native={k:v for k,v in metrics.items() if not k.startswith('screen_') and any(s in k for s in ('drc','wirelength','via','antenna','overflow'))}
    # Native DRT metrics are available only after the completed detailed-route stage.
    drc=finite(metrics.get('detailedroute__route__drc_errors'))
    wire=finite(metrics.get('detailedroute__route__wirelength'))
    vias=finite(metrics.get('detailedroute__route__vias'))
    required=(res/'5_route.odb',res/'screen_post_route.spef',final_netlist,res/'5_route.sdc')
    route_complete=all(p.is_file() and p.stat().st_size>0 for p in required)
    grt_log=(logs/'5_1_grt.log').read_text() if (logs/'5_1_grt.log').exists() else ''
    overflow=[int(x) for x in re.findall(r'^Total\s+\d+\s+\d+\s+[\d.]+%\s+\d+\s*/\s*\d+\s*/\s*(\d+)',grt_log,re.M)]
    iterations={k.rsplit(':',1)[1]:v for k,v in metrics.items() if k.startswith('detailedroute__route__drc_errors__iter:')}
    drc_report=reports/'5_route_drc.rpt'
    drc_evidence=drc_report.is_file() and (drc!=0 or not drc_report.read_text().strip())
    sdc_hash=sha256_file(res/'5_route.sdc') if (res/'5_route.sdc').is_file() else None
    job_ok=rec['job']['exit_code']==0 and not rec['job']['timed_out']
    fields=('setup_ws_ns','setup_tns_ns','hold_ws_ns','hold_tns_ns','setup_violating_endpoints','hold_violating_endpoints','slew_violations','cap_violations','instance_area_um2','core_area_um2','die_area_um2')
    complete_metrics=all(all(s.get(k) is not None for k in fields) for s in snapshots.values())
    valid=job_ok and route_complete and complete_metrics and proof['status']=='PASS' and drc is not None and drc_evidence and len(pins)==len(STAGES) and len(set(pins.values()))==1 and bool(events) and all(e['exit_code']==0 and e['net_edits'] is not None for e in events)
    for p in [*required,run/'flow.log',run/'config.mk',res/'screen_repair_events.tsv',drc_report,*res.glob('screen_*.tsv')]:
        if p.is_file():evidence[str(p.relative_to(root))]=sha256_file(p)
    result={'name':run.name,'design':design,'variant':variant,'seed':rec['seed'],'status':'MEASURED' if valid else 'INCOMPLETE',
            'plan_sha256':rec.get('plan_sha256'),'job':rec['job'],'route_complete':route_complete,
            'snapshots':snapshots,'repair_events':events,'pin_hashes':pins,'routed_equivalence':proof,
            'detailed_route_drc_errors':drc,'wirelength_um':wire,'vias':vias,
            'grt_overflow_totals':overflow or None,'drc_by_iteration':iterations,'routed_sdc_sha256':sdc_hash,
            'native_route_metrics':native,'evidence':evidence,'analyzer_sha256':sha256_file(__file__)}
    write_json(run/'backend_risk.json',result)
    return result


def aggregate(root,results):
    rows=[]
    for r in results:
        row={k:r[k] for k in ('name','design','variant','seed','status','detailed_route_drc_errors','wirelength_um','vias')}
        for stage,s in r['snapshots'].items():
            for k in ('setup_ws_ns','setup_tns_ns','setup_violating_endpoints','hold_violating_endpoints','slew_violations','cap_violations','instance_area_um2'):
                row[stage+'__'+k]=s[k]
        # Policy-mandated I/O buffering is separate from risk-driven design/timing repairs.
        for category,words in [('design_timing',('repair_design','repair_timing')),('ports',('buffer_ports',))]:
            ev=[e for e in r['repair_events'] if any(e['label'].endswith(w) for w in words)]
            for k in ('inserted','removed','master_changed','inserted_buffers','area_delta_um2'):
                row[category+'__'+k]=sum(e['net_edits'][k] for e in ev) if ev and all(e['net_edits'] is not None for e in ev) else None
            row[category+'__elapsed_ms']=sum(e['elapsed_ms'] for e in ev)
        row['drc_iterations']=len(r['drc_by_iteration']) or None
        row['flow_elapsed_s']=r['job']['elapsed_s']
        rows.append(row)
    with (root/'summary.csv').open('w') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    expected={(d,v,s) for d in ('cla32','mul8') for v in ('original','candidate') for s in (17,29)}
    by_design={}
    for d in ('cla32','mul8'):
        rr=[r for r in results if r['design']==d]
        by_design[d]={
            'same_fixed_pins':len({h for r in rr for h in r['pin_hashes'].values()})==1,
            'same_routed_sdc':len({r['routed_sdc_sha256'] for r in rr})==1 and all(r['routed_sdc_sha256'] for r in rr),
            'same_actual_core_and_die':len({(s['core_area_um2'],s['die_area_um2']) for r in rr for s in r['snapshots'].values()})==1,
        }
    frozen_ok=False
    try:
        from experiments.backend_screen.screen import verify_frozen
        plan_hash=verify_frozen(root)
        frozen_ok=all(r['plan_sha256']==plan_hash for r in results)
    except (ValueError,OSError):pass
    audit={'expected_runs_present':{(r['design'],r['variant'],r['seed']) for r in results}==expected and len(results)==8,
           'all_measured':all(r['status']=='MEASURED' for r in results),
           'frozen_flow_inputs_match':frozen_ok,'design_contracts':by_design,'model_calls':0}
    audit['status']='PASS' if audit['expected_runs_present'] and audit['all_measured'] and frozen_ok and all(all(x.values()) for x in by_design.values()) else 'INCOMPLETE'
    write_json(root/'audit.json',audit)
    return audit

def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--root',type=Path,required=True);ap.add_argument('--run');ap.add_argument('--prove',action='store_true');a=ap.parse_args();root=a.root.resolve()
    names=[a.run] if a.run else sorted(p.name for p in (root/'runs').iterdir() if not p.name.startswith('bootstrap'))
    results=[read_run(root,root/'runs'/name,a.prove) for name in names]
    for r in results:print(r['name'],r['status'],r['routed_equivalence']['status'],r['snapshots']['post_route'])
    if not a.run:
        write_json(root/'results.json',results)
        print(json.dumps(aggregate(root,results),indent=2))
if __name__=='__main__':main()
