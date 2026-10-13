from pathlib import Path
import json
import math
import re
import shutil
import xml.etree.ElementTree as ET
from .common import *
from .verify import proof

STAGES=[('floorplan','2_floorplan.odb'),('placement','3_place.odb'),('cts','4_cts.odb'),('route','5_route.odb'),('finish','6_final.odb')]

def configure(root,run,spec,c):
    root=Path(root);flow=root/'flow';backend=run/'backend';backend.mkdir()
    cfg={'DESIGN_NAME':spec['top'],'DESIGN_NICKNAME':spec['top'],'PLATFORM':c['platform'],
         'VERILOG_FILES':str(run.parent/'rtl.v'),'SYNTH_NETLIST_FILES':str(run/'mapped.v'),
         'SDC_FILE':str(root/'frozen/constraints.sdc'), 'DIE_AREA':' '.join(map(str,c['die_area'])),
         'CORE_AREA':' '.join(map(str,c['core_area'])),'PLACE_DENSITY':c['place_density'],
         'NUM_CORES':c['threads'],'OR_SEED':c['route_seed'],'DETAILED_METRICS':1,
         'TNS_END_PERCENT':100,'EQUIVALENCE_CHECK':0,'RECOVER_POWER':0,
         'OPENROAD_EXE':c['tools']['openroad'],'YOSYS_EXE':c['tools']['yosys'],
         'KLAYOUT_CMD':c['tools']['klayout'],'WORK_HOME':str(backend/'work'),'FLOW_VARIANT':'base',
         'SCRIPTS_DIR':str(flow/'scripts'),'UTILS_DIR':str(flow/'util')}
    config=backend/'config.mk';config.write_text(''.join(f'export {k} = {v}\n' for k,v in cfg.items()))
    return backend,config

def invoke(root,backend,config,c,target,log=None):
    cmd=[c['tools']['make'],'-f',str(Path(root)/'flow/Makefile'),f'FLOW_HOME={Path(root)/"flow"}',f'DESIGN_CONFIG={config}',target]
    # Cached-netlist ORFS exits before creating 1_synth.rtlil. Without these
    # boundaries make would rerun all earlier stages at every invocation.
    for path in sorted((backend/'work/results').rglob('*')):
        if path.is_file():
            cmd.extend(['-o', str(path)])
    return checked_job(cmd,backend,log or backend/(target+'.log'),c['timeout_s'])

def export_stage(run,spec,c,stage,odb,results):
    out=run/'stages'/stage;out.mkdir(parents=True)
    shutil.copyfile(odb,out/'design.odb')
    odb=out/'design.odb'
    plat=Path(c['platform_dir']);script=out/'export.tcl'
    s=f'read_liberty {tcl(plat/c["liberty"])}\nread_db {tcl(odb)}\n'
    s+=f'write_def {tcl(out/"design.def")}\n'
    s+=f'write_verilog -remove_cells {{FILLCELL* TAPCELL*}} {tcl(out/"logical.v")}\n'
    if stage=='finish':
        s+=f'read_sdc {tcl(results/"6_final.sdc")}\nread_spef {tcl(results/"6_final.spef")}\n'
        s+='report_worst_slack -max\nreport_worst_slack -min\nreport_tns\nreport_check_types -max_slew -max_capacitance -max_fanout -violators\n'
        s+='puts "S2G_SLEW_COUNT [sta::max_slew_violation_count]"\nputs "S2G_CAP_COUNT [sta::max_capacitance_violation_count]"\nputs "S2G_FANOUT_COUNT [sta::max_fanout_violation_count]"\n'
        s+=f'write_cdl -masters {tcl(plat/c["cdl"])} {tcl(out/"design.cdl")}\n'
    s+='puts "S2G_EXPORT_COMPLETE"\n';script.write_text(s)
    checked_job([c['tools']['openroad'],'-exit',script],out,out/'export.log',c['timeout_s'],'S2G_EXPORT_COMPLETE')
    sources=files_manifest([odb,out/'design.def',out/'logical.v'])
    record={'schema':'s2g.stage.v1','stage':stage,'status':'COMPLETE','artifacts':sources,'completed_at':stamp()}
    write(out/'record.json',record)
    return record

def repair_cost(results):
    events=results/'s2g_repair_events.tsv';records=[]
    if not events.exists():return {'valid':False,'net_edits':None,'events':[]}
    def inv(path):
        return dict(line.split('\t') for line in path.read_text().splitlines())
    for line in events.read_text().splitlines():
        label,command,ms,code,*args=line.split('\t')
        before=inv(results/f's2g_{label}_before.tsv');after=inv(results/f's2g_{label}_after.tsv')
        records.append({'command':command,'inserted':len(after.keys()-before.keys()),'removed':len(before.keys()-after.keys()),
                        'master_changed':sum(before[k]!=after[k] for k in before.keys()&after.keys()),
                        'elapsed_ms':int(ms),'return_code':int(code)})
    return {'valid':True,'net_edits':sum(r['inserted']+r['removed']+r['master_changed'] for r in records),'events':records,
            'scope':'net instance insert/remove/master changes per wrapped command; excludes pin swaps, transients, and edits inside unwrapped placer/router commands'}

def parasitic_provenance(run,results,c):
    """Classify the final SPEF from what the tools actually logged, not from its extension.
    detailed_route_extraction requires an OpenRCX run on routed geometry with a named rules file."""
    spef=results/'6_final.spef'
    logs=sorted((run/'backend/work/logs'/c['platform']).rglob('6_report.log'))
    text=logs[-1].read_text(errors='replace') if logs else ''
    rules=re.findall(r'RCX-0435\] Reading extraction model file (\S+)',text)
    extracted=re.search(r'RCX-0045\] Extract \d+ nets',text) is not None
    routed='route' in ledger_stages(run)
    expected=Path(c['platform_dir'])/c['rcx_rules'] if c.get('platform_dir') and c.get('rcx_rules') else None
    rec={'spef_present':spef.is_file(),'spef_sha256':digest(spef) if spef.is_file() else None,
         'rules_file':rules[-1] if rules else None,'expected_rules_sha256':digest(expected) if expected and expected.is_file() else None,
         'rules_sha256':digest(rules[-1]) if rules and Path(rules[-1]).is_file() else None,
         'generator_log':str(logs[-1]) if logs else None,
         'loaded_by_s2g_sta':'read_spef' in (run/'stages/finish/export.tcl').read_text() if (run/'stages/finish/export.tcl').is_file() else False}
    ok=rec['spef_present'] and extracted and routed and rec['rules_sha256'] and rec['rules_sha256']==rec['expected_rules_sha256']
    rec['provenance']='detailed_route_extraction' if ok else ('unknown' if rec['spef_present'] else None)
    rec['reason']=None if ok else 'no OpenRCX extraction with the frozen rules file on routed geometry was logged'
    return rec

def ledger_stages(run):
    try:return read(run/'ledger.json').get('stages',{})
    except (OSError,ValueError):return {}

def measure(run,results,c):
    metrics={}
    for path in sorted((run/'backend/work/logs'/c['platform']).rglob('6_report.json')):
        metrics.update(read(path))
    def pick(suffix):
        vals=[v for k,v in metrics.items() if k.endswith(suffix) and isinstance(v,(int,float)) and math.isfinite(v)]
        return vals[-1] if vals else None
    exported=(run/'stages/finish/export.log').read_text()
    def count(marker):
        found=re.findall(r'^'+marker+r' (\d+)\s*$',exported,re.M)
        return int(found[0]) if len(found)==1 else None
    setup=pick('timing__setup__ws');hold=pick('timing__hold__ws')
    para=parasitic_provenance(run,results,c)
    report={'schema':'s2g.risk.v2','source':'measured_final_'+str(para['provenance']),'parasitics':para,'prediction':None,
            'setup_ws':setup,'hold_ws':hold,
            'setup_wns':min(0,setup) if setup is not None else None,
            'hold_wns':min(0,hold) if hold is not None else None,
            'setup_tns':pick('timing__setup__tns'),'area_um2':pick('design__instance__area'),
            'setup_violations':pick('timing__drv__setup_violation_count'),'hold_violations':pick('timing__drv__hold_violation_count'),
            'slew_violations':count('S2G_SLEW_COUNT'),'cap_violations':count('S2G_CAP_COUNT'),
            'fanout_violations':count('S2G_FANOUT_COUNT'),
            'repair':repair_cost(results),'power':{'value':None,'valid':False,'reason':'no activity contract'},
            'antenna':{'value':None,'valid':False,'reason':'not yet parsed by this adapter'}}
    report['valid']={k:v is not None for k,v in report.items() if k not in ('schema','source','parasitics','prediction','repair','power','antenna')}
    write(run/'risk.json',report)
    return report

def signoff(run,c,results):
    out=run/'signoff';out.mkdir();plat=Path(c['platform_dir']);gds=results/'6_final.gds'
    require(gds.is_file() and gds.stat().st_size>0,'GDS missing')
    record={'schema':'s2g.signoff.v1','gds':{'status':'PASS','path':str(gds),'sha256':digest(gds)},
            'scope':'Nangate45 research deck checks; not tapeout qualification'}
    for kind in ('drc','lvs'):
        deck=plat/c[kind+'_deck'];log=out/(kind+'.log');report=out/(kind+'.lyrdb' if kind=='drc' else kind+'.lvsdb')
        if not deck.is_file():
            record[kind]={'status':'UNSUPPORTED','reason':'missing deck'};continue
        args=[c['tools']['klayout'],'-b']
        if kind=='drc':
            args+=['-rd',f'in_gds={gds}','-rd',f'report_file={report}']
        else:
            cdl=out/'reference.cdl'
            cdl.write_text((plat/c['cdl']).read_text()+'\n'+(run/'stages/finish/design.cdl').read_text())
            args+=['-rd',f'input={gds}','-rd',f'schematic={cdl}','-rd',f'target_netlist={out/"extracted.cir"}',
                   '-rd',f'report_file={report}']
        args+=['-r',deck]
        j=job(args,out,log,c['timeout_s']);write(out/(kind+'.job.json'),j)
        result={'status':'FAIL','job':j,'deck_sha256':digest(deck),'gds_sha256':digest(gds)}
        if j['exit_code']==0 and not j['timed_out']:
            if kind=='drc' and report.is_file():
                try:
                    tree=ET.parse(report);require(tree.getroot().tag=='report-database','unexpected DRC report schema')
                    require(tree.find('.//items') is not None,'missing DRC items section')
                    count=len(tree.findall('.//items/item'));result.update(status='PASS' if count==0 else 'FAIL',violations=count,report_sha256=digest(report))
                except (ET.ParseError,GateError) as e:result['error']=str(e)
            elif kind=='lvs':
                text=log.read_text(errors='replace');db=report if report.is_file() else results/'lvs_report.lvsdb'
                if db.is_file() and db!=report:
                    shutil.copyfile(db,report)
                if report.is_file():result['report_sha256']=digest(report)
                if "CONGRATULATIONS! Netlists match." in text and "ERROR" not in text and db.is_file():
                    result.update(status='PASS',report_sha256=digest(db))
        record[kind]=result
    write(run/'signoff.json',record)
    return record

def run_backend(root,run,spec,c,ledger):
    backend,config=configure(root,run,spec,c)
    results=backend/'work/results'/c['platform']/spec['top']/'base'
    for stage,odb_name in STAGES:
        invoke(root,backend,config,c,{'placement':'place'}.get(stage,stage))
        odb=results/odb_name;require(odb.is_file(),f'{stage} ODB missing')
        rec=export_stage(run,spec,c,stage,odb,results)
        ledger['stages'][stage]=rec;write(run/'ledger.json',ledger)
    require((results/'6_final.spef').is_file(),'RCX SPEF missing')
    proof(c,run,'postroute_equivalence',Path(root)/'frozen/reference.v',run/'stages/finish/logical.v',spec['top'],True)
    ledger['postroute_equivalence']={'status':'PASS'};write(run/'ledger.json',ledger)
    return measure(run,results,c),signoff(run,c,results)
