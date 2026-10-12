#!/usr/bin/env python3
"""No-model OpenROAD/ORFS DEV backend screen. All writes stay in a fresh workspace."""
import argparse
import csv
import json
import os
import signal
from pathlib import Path
import shutil
import subprocess
import sys
import time

PILOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(PILOT))
from rtltrial.util import read_json,write_json,sha256_file,sha256_json,run_job
from rtltrial.search import engine,adapter
from rtltrial.liberty import load_liberty
ORFS=Path('/opt/OpenROAD-flow-scripts/flow')
HERE=Path(__file__).resolve().parent
SOURCES={'mul8':PILOT/'dev/structure_search_v1/mul8','cla32':PILOT/'dev/structure_search_v2/cla32'}
DESIGNS={'mul8':{'die':100,'core':80},'cla32':{'die':72,'core':52}}
SDC='''# Experimental interface contract, not an owner-provided timing specification.
create_clock -name virtual_io -period 10.0
set_input_delay -clock virtual_io -max 2.0 [all_inputs]
set_input_delay -clock virtual_io -min 0.2 [all_inputs]
set_output_delay -clock virtual_io -max 2.0 [all_outputs]
set_output_delay -clock virtual_io -min 0.2 [all_outputs]
set_driving_cell -lib_cell sky130_fd_sc_hd__buf_1 -pin X [all_inputs]
set_load 0.005 [all_outputs]
'''


def require(ok,msg):
    if not ok:raise ValueError(msg)


def prepare(root):
    require(not root.exists(),'choose a new root')
    root.mkdir(parents=True)
    scripts=root/'orfs_scripts';shutil.copytree(ORFS/'scripts',scripts)
    shutil.copytree(ORFS/'util',root/'orfs_util')
    for folder in (scripts,root/'orfs_util'):
        for executable in folder.rglob('*'):
            if executable.is_file() and executable.read_bytes().startswith(b'#!'):
                executable.chmod(executable.stat().st_mode | 0o111)
    shutil.copyfile(HERE/'screen_metrics.tcl',scripts/'screen_metrics.tcl')
    with (scripts/'load.tcl').open('a') as f:f.write('\nsource $::env(SCRIPTS_DIR)/screen_metrics.tcl\n')
    # Each repair gets a stage prefix so inventories from different processes do not collide.
    p=scripts/'screen_metrics.tcl';s=p.read_text().replace('set label "repair${::screen_serial}_@COMMAND@"','set label "${::screen_stage}_repair${::screen_serial}_@COMMAND@"');p.write_text(s)
    for name in ('floorplan','resize','cts','global_route','detail_route'):
        p=scripts/(name+'.tcl');p.write_text('set ::screen_stage '+name+'\n'+p.read_text())
    # Placement/GRT entry snapshots identify un-repaired risk at reproducible boundaries.
    p=scripts/'resize.tcl';s=p.read_text().replace('set instance_count_before','screen_snapshot placement_pre_repair\n\nset instance_count_before',1);p.write_text(s)
    p=scripts/'global_route.tcl';s=p.read_text().replace('  if { !$::env(SKIP_INCREMENTAL_REPAIR) } {','  screen_snapshot grt_pre_repair\n\n  if { !$::env(SKIP_INCREMENTAL_REPAIR) } {',1).replace('  report_metrics 5 "global route"','  report_metrics 5 "global route"\n  screen_snapshot grt_post_repair');p.write_text(s)
    prepare_flow_home(root)
    (root/'constraints.sdc').write_text(SDC)
    post='''global_connect
define_process_corner -ext_model_index 0 X
extract_parasitics -ext_model_file /opt/OpenROAD-flow-scripts/flow/platforms/sky130hd/rcx_patterns.rules
write_spef $::env(RESULTS_DIR)/screen_post_route.spef
read_spef $::env(RESULTS_DIR)/screen_post_route.spef
screen_snapshot post_route
'''
    (root/'post_route.tcl').write_text(post)
    evidence={}
    for design,src in SOURCES.items():
        engine.verify(src,require_current=False)
        e=adapter.load(design)
        man=read_json(src/'protocol.json')
        eroot=root/'inputs'/design
        shutil.copytree(src/'frozen',eroot/'frozen')
        lib=load_liberty(man['liberty'],eroot/'cache')
        for variant,cid in [('original','B0'),('candidate','B1-001')]:
            rtl=src/'rtl'/cid/'dut.v'
            if cid!='B0':
                rec=engine.record(src,cid)
                require(rec.get('validated') and sha256_file(rtl)==rec['rtl_sha256'],'unvalidated candidate')
            out=eroot/'rtl'/variant;out.mkdir(parents=True);shutil.copyfile(rtl,out/'dut.v')
            r={'source':str(rtl),'rtl_sha256':sha256_file(rtl),'lint':e.screen.lint(eroot,variant,man['tools'])}
            r['original_tb']=e.screen.simulate(eroot,variant,man['tools'])
            r['extension_tb']=e.screen.simulate(eroot,variant,man['tools'],True)
            r['formal']=e.screen.formal(eroot,variant,man['tools'])
            require(r['lint']['status']=='PASS' and r['original_tb']['status']==r['extension_tb']['status']=='TB_PASSED' and r['formal']['status']=='PASS','input validation failed')
            r['synth']=e.screen.synth(eroot,variant,man['tools'],(eroot/'frozen/synth.ys').read_text(),lib)
            require(r['synth']['status']=='SYNTH_DONE' and r['synth']['check_clean'],'synthesis failed')
            require(r['synth']['metrics']['netlist_sha256']==engine.metrics(src,cid)['netlist_sha256'],'fresh synthesis differs from validated source')
            write_json(eroot/(variant+'_validation.json'),r);evidence[design+'/'+variant]=r
            mapped_proof=e.gate_audit(eroot,variant,man)
            write_json(eroot/(variant+'_mapped_validation.json'),mapped_proof)
            require(mapped_proof['status']=='PASS','mapped proof failed')
        mapped=read_json(eroot/'runs/original-synth/mapped.json')['modules']['TopModule']['ports']
        die=DESIGNS[design]['die'];core=DESIGNS[design]['core'];lines=[]
        for port,desc in mapped.items():
            n=len(desc['bits']);offset=desc.get('offset',0)
            for i in range(n):
                name=f'{port}[{i+offset}]' if n>1 else port
                coordinate=10+(i+0.5)*core/n
                if port=='A':
                    x,y=0,0.34+0.68*round((coordinate-0.34)/0.68);layer='met3';size=.68
                elif port=='B':
                    x,y=0.23+0.46*round((coordinate-.23)/.46),0;layer='met2';size=.46
                elif port=='C32':
                    x,y=.23+.46*round((die/2-.23)/.46),die;layer='met2';size=.46
                else:
                    x,y=die,.34+.68*round((coordinate-.34)/.68);layer='met3';size=.68
                lines.append(f'place_pin -pin_name {{{name}}} -layer {layer} -location {{{x:.4f} {y:.4f}}} -pin_size {{{size} {size}}} -force_to_die_boundary')
        (eroot/'pins.tcl').write_text('\n'.join(lines)+'\n')
    return finalize_plan(root)


def prepare_flow_home(root):
    flow=root/'orfs_flow';flow.mkdir(exist_ok=True)
    shutil.copyfile(ORFS/'Makefile',flow/'Makefile')
    for name,target in [('scripts',root/'orfs_scripts'),('util',root/'orfs_util'),('platforms',ORFS/'platforms')]:
        if not (flow/name).exists():(flow/name).symlink_to(target,target_is_directory=True)


def finalize_plan(root):
    evidence={d+'/'+v:read_json(root/'inputs'/d/(v+'_validation.json')) for d in DESIGNS for v in ('original','candidate')}
    rev=subprocess.run(['git','-C',str(ORFS),'rev-parse','HEAD'],capture_output=True,text=True)
    plan={'schema':'rtltrial.backend-screen.v1','status':'BOOTSTRAP','designs':DESIGNS,
          'variants':['original','candidate'],'route_seeds':[17,29], 'model_calls':0,
          'timing':'experimental virtual IO clock 10ns; input/output max 2ns/min 0.2ns; input buf_1; output 0.005pF; not an owner timing target',
          'objective':'screen observable physical risk and net repair edits under a fixed contract, no LLM comparison',
          'evidence':{k:{'rtl_sha256':v['rtl_sha256'],'synth_netlist_sha256':v['synth']['metrics']['netlist_sha256']} for k,v in evidence.items()},
          'openroad_version':subprocess.check_output(['/usr/bin/openroad','-version'],text=True).strip(),
          'orfs_commit':rev.stdout.strip() if rev.returncode==0 else None,
          'orfs_provenance':'installed tree; source file hashes recorded',
          'orfs_makefile_sha256':sha256_file(ORFS/'Makefile')}
    write_json(root/'plan.json',plan)
    return plan


def freeze(root):
    plan=read_json(root/'plan.json')
    require(plan['status']=='BOOTSTRAP','plan already frozen')
    for design in DESIGNS:
        for variant in ('original','candidate'):
            require(read_json(root/'inputs'/design/(variant+'_mapped_validation.json'))['status']=='PASS','mapped proof required')
    paths=[HERE/'screen.py',HERE/'screen_metrics.tcl',root/'constraints.sdc',root/'post_route.tcl',root/'orfs_flow/Makefile',Path('/usr/bin/openroad')]
    # Analysis is read-only and separately hashed in the result; freeze all flow inputs here.
    for folder in (root/'orfs_scripts',root/'orfs_util',ORFS/'platforms/sky130hd'):
        paths.extend(p for p in folder.rglob('*') if p.is_file() and '__pycache__' not in p.parts)
    for design in DESIGNS:
        paths.append(root/'inputs'/design/'pins.tcl')
        for variant in ('original','candidate'):
            paths.extend([root/'inputs'/design/'rtl'/variant/'dut.v',root/'inputs'/design/'runs'/f'{variant}-synth/netlist.v'])
    plan['frozen_files']={str(p):sha256_file(p) for p in sorted(set(paths))}
    plan['status']='FROZEN'
    plan['seed_scope']='OR_SEED affects detailed routing only; these are not independent placement or RTL samples'
    plan['repair_cost_scope']='net inventory edits per wrapped repair call; excludes transient edits, pin swaps and internal GPL/router edits; elapsed repair time excludes snapshots'
    plan['frozen_at']=time.strftime('%Y-%m-%dT%H:%M:%S%z')
    write_json(root/'plan.json',plan)
    return {'status':'FROZEN','plan_sha256':sha256_file(root/'plan.json'),'files':len(paths)}


def verify_frozen(root):
    plan=read_json(root/'plan.json')
    require(plan['status']=='FROZEN','plan not frozen')
    for p,h in plan['frozen_files'].items():require(sha256_file(p)==h,'frozen input changed: '+p)
    return sha256_file(root/'plan.json')


def config(root,run,design,variant,seed):
    run.mkdir(parents=True,exist_ok=False)
    d=DESIGNS[design];die=d['die'];core=d['core']
    cfg={
        'DESIGN_NAME':'TopModule','DESIGN_NICKNAME':design,'PLATFORM':'sky130hd',
        'VERILOG_FILES':str(root/'inputs'/design/'rtl'/variant/'dut.v'),
        'SYNTH_NETLIST_FILES':str(root/'inputs'/design/'runs'/f'{variant}-synth/netlist.v'),
        'SDC_FILE':str(root/'constraints.sdc'), 'SCRIPTS_DIR':str(root/'orfs_scripts'), 'UTILS_DIR':str(root/'orfs_util'),
        'DIE_AREA':f'0 0 {die} {die}', 'CORE_AREA':f'10 10 {10+core} {10+core}',
        'FOOTPRINT_TCL':str(root/'inputs'/design/'pins.tcl'),
        'PLACE_DENSITY':'0.60','DETAILED_METRICS':'1','NUM_CORES':'1','OR_SEED':str(seed),
        'TNS_END_PERCENT':'100','EQUIVALENCE_CHECK':'0','RECOVER_POWER':'0',
        'POST_DETAIL_ROUTE_TCL':str(root/'post_route.tcl'),
        'OPENROAD_EXE':'/usr/bin/openroad','YOSYS_EXE':'/opt/OpenROAD/oss-cad-suite/bin/yosys',
        'WORK_HOME':str(run/'work'), 'FLOW_VARIANT':'base'}
    (run/'config.mk').write_text('\n'.join('export '+k+' = '+v for k,v in cfg.items())+'\n')
    return cfg


def run_flow(root,name,design,variant,seed):
    require(design in DESIGNS and variant in ('original','candidate') and seed in (17,29),'run outside plan')
    plan_hash=verify_frozen(root) if not name.startswith('bootstrap') else None
    run=root/'runs'/name
    config(root,run,design,variant,seed)
    command=['make','-f',str(root/'orfs_flow/Makefile'),f'FLOW_HOME={root/"orfs_flow"}',f'DESIGN_CONFIG={run/"config.mk"}','route']
    env={k:v for k,v in os.environ.items() if not k.startswith('LLM_') and 'API_KEY' not in k}
    for key in ('PYTHONHOME','PYTHONPATH','LD_LIBRARY_PATH'):env.pop(key,None)
    env['QT_QPA_PLATFORM']='offscreen'
    env['PATH']='/usr/bin:/bin:'+env.get('PATH','')
    # Kill the entire tool process group on timeout, including make children.
    start=time.monotonic();timeout=False
    with (run/'flow.log').open('w') as log:
        proc=subprocess.Popen(command,cwd=run,stdout=log,stderr=subprocess.STDOUT,env=env,start_new_session=True)
        try:code=proc.wait(timeout=900)
        except subprocess.TimeoutExpired:
            timeout=True;os.killpg(proc.pid,signal.SIGKILL);proc.wait();code=None
    job={'cmd':command,'exit_code':code,'timed_out':timeout,'elapsed_s':round(time.monotonic()-start,3),'log':str(run/'flow.log')}
    write_json(run/'job.json',{'design':design,'variant':variant,'seed':seed,'plan_sha256':plan_hash,'job':job})
    return job


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('command',choices=['prepare','freeze','run']);ap.add_argument('--root',type=Path,required=True)
    ap.add_argument('--name');ap.add_argument('--design',choices=DESIGNS);ap.add_argument('--variant',choices=['original','candidate']);ap.add_argument('--seed',type=int,default=17)
    a=ap.parse_args();root=a.root.resolve()
    if a.command=='prepare':print(json.dumps(prepare(root),indent=2))
    elif a.command=='freeze':print(json.dumps(freeze(root),indent=2))
    else:print(json.dumps(run_flow(root,a.name,a.design,a.variant,a.seed),indent=2))
if __name__=='__main__':main()
