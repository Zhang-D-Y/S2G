import fcntl
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path
from .common import *
from .contracts import validate_spec

@contextmanager
def locked(root):
    with (Path(root)/'.lock').open('a') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield

def doctor(context):
    c = read(context) if not isinstance(context, dict) else context
    results = {}
    for name, exe in c['tools'].items():
        arg = {'yosys':'-V','openroad':'-version','iverilog':'-V','vvp':'-V','klayout':'-v','make':'--version'}[name]
        try:
            r = subprocess.run([exe,arg], capture_output=True, text=True, env=clean_env(), timeout=15)
            results[name] = {'path':exe, 'exists':Path(exe).is_file(), 'version':(r.stdout+r.stderr).splitlines()[:2], 'exit_code':r.returncode}
        except (OSError, subprocess.TimeoutExpired) as e:
            results[name] = {'path':exe, 'error':str(e)}
    results['r2g_parsers'] = (Path(c['graph_parser_dir'])/'def_parse.py').is_file()
    results['pyg'] = 'optional local S2G exporter; requires a separate torch/PyG environment'
    results['profile_note'] = c.get('provenance_note')
    return results

def init(root, spec_path, reference, tb, sdc, context, review_path=None):
    root = Path(root).resolve()
    require(not root.exists(), 'workspace must be new')
    spec = read(spec_path); sha = validate_spec(spec)
    review = read(review_path or str(spec_path)+'.review.json')
    require(review['spec_hash'] == sha and not spec['open_questions'], 'review does not match resolved spec')
    c = read(context)
    require(c['schema']=='s2g.context.v1' and c['candidate_budget']>0, 'invalid implementation context')
    s2g_root=Path(__file__).resolve().parents[2]
    for key in ('graph_parser_dir','typed_rules','typed_guard'):
        if key in c:require(Path(c[key]).resolve().is_relative_to(s2g_root),'non-tool scripts must reside under S2G: '+key)
    for p in [root, *[Path(v) for v in c['tools'].values()], Path(c['orfs_flow']), Path(c['platform_dir'])]:
        require(not any(ch in str(p) for ch in ' \n\r#$;{}'), 'flow paths require simple non-whitespace paths')
    require(c['platform'] == 'nangate45', 'v0.1 backend profile currently supports nangate45')
    root.mkdir(parents=True)
    frozen=root/'frozen'; frozen.mkdir()
    for name, src in [('spec.json',spec_path),('reference.v',reference),('tb.v',tb),('constraints.sdc',sdc)]:
        shutil.copyfile(src, frozen/name)
    write(frozen/'review.json',review); write(frozen/'context.json',c)
    flow=root/'flow';flow.mkdir()
    shutil.copyfile(Path(c['orfs_flow'])/'Makefile',flow/'Makefile')
    for folder in ('scripts','util'):
        shutil.copytree(Path(c['orfs_flow'])/folder, flow/folder)
        for f in tree_files(flow/folder):
            if f.read_bytes().startswith(b'#!'): f.chmod(f.stat().st_mode|0o111)
    # Instrument copies only. The repair policy and source installation remain intact.
    instrument=Path(__file__).parent/'assets/instrument.tcl'
    shutil.copyfile(instrument,flow/'scripts/s2g_instrument.tcl')
    with (flow/'scripts/load.tcl').open('a') as f:
        f.write('\nsource $::env(SCRIPTS_DIR)/s2g_instrument.tcl\n')
    (flow/'platforms').mkdir()
    (flow/'platforms'/c['platform']).symlink_to(Path(c['platform_dir']),target_is_directory=True)
    deps=tree_files(frozen)+tree_files(flow/'scripts')+tree_files(flow/'util')+[flow/'Makefile']
    deps+=tree_files(c['platform_dir'])+[Path(x) for x in c['tools'].values()]
    # Explicit overrides may be under S2G rather than inside the platform tree.
    for key in ('liberty','tech_lef','cell_lef','drc_deck','lvs_deck','cdl','rcx_rules'):
        path=Path(c['platform_dir'])/c[key]
        require(path.is_file(),'missing implementation input: '+str(path))
        deps.append(path)
    parsers=Path(c['graph_parser_dir'])
    deps+=tree_files(parsers.parent)
    for key in ('typed_rules','typed_guard'):
        if key in c:deps.append(Path(c[key]))
    freeze={'schema':'s2g.freeze.v1','spec_hash':sha,'context_hash':identity(c),'created_at':stamp(),
            'files':files_manifest(deps),'r2g_commit':read(parsers.parent/'PROVENANCE.json')['commit'],
            'tools':doctor(c)}
    write(root/'freeze.json',freeze)
    write(root/'state.json',{'schema':'s2g.pool.v1','attempts':[]})
    return {'workspace':str(root),'spec_hash':sha,'freeze_hash':digest(root/'freeze.json')}

def verify(root):
    root=Path(root); freeze=read(root/'freeze.json');verify_manifest(freeze['files'])
    return read(root/'frozen/spec.json'), read(root/'frozen/context.json'), freeze

def reserve(root, parent=None, action=None):
    root=Path(root); spec,c,freeze=verify(root)
    with locked(root):
        state=read(root/'state.json')
        require(len(state['attempts'])<c['candidate_budget'], 'candidate budget exhausted (failed attempts count)')
        if parent:
            require((root/'candidates'/parent/'record.json').is_file(), 'unknown parent')
        cid='c%04d'%len(state['attempts'])
        record={'schema':'s2g.candidate.v1','id':cid,'design_family':spec['design_family'], 'spec_hash':freeze['spec_hash'],
                'context_hash':freeze['context_hash'],'freeze_hash':digest(root/'freeze.json'), 'parent':parent,
                'action':action or {'op':'import'},'status':'RESERVED','created_at':stamp()}
        write(root/'candidates'/cid/'record.json',record)
        state['attempts'].append(cid);write(root/'state.json',state)
    return cid

def submit(root,cid,rtl):
    root=Path(root);verify(root)
    with locked(root):
        p=root/'candidates'/cid;rec=read(p/'record.json')
        require(rec['status']=='RESERVED','candidate already submitted')
        try:
            data=Path(rtl).read_bytes();require(data.strip(), 'empty candidate')
            (p/'rtl.v').write_bytes(data);rec['rtl_hash']=digest(p/'rtl.v')
            duplicates=[]
            for other in read(root/'state.json')['attempts']:
                r=read(root/'candidates'/other/'record.json')
                if other!=cid and r.get('rtl_hash')==rec['rtl_hash']:duplicates.append(other)
            rec.update(status='SUBMITTED',source_duplicates=duplicates)
        except (OSError, GateError) as e:
            rec.update(status='GENERATION_FAILED',error=str(e));write(p/'record.json',rec);raise
        write(p/'record.json',rec)
        return rec

def add(root,rtl,parent=None,action=None):
    return submit(root,reserve(root,parent,action),rtl)

def abort(root,cid,reason):
    root=Path(root);verify(root)
    with locked(root):
        p=root/'candidates'/cid;rec=read(p/'record.json')
        require(rec['status']=='RESERVED','only an unsubmitted reservation can be aborted')
        rec.update(status='GENERATION_FAILED',error=reason);write(p/'record.json',rec)
    return rec
