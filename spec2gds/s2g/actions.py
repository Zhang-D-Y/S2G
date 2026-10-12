"""Read-only bridge to the existing ROVER-inspired typed arithmetic rules."""
import importlib.util
from pathlib import Path
from .common import *
from .workspace import verify,reserve,submit,locked

def load(path,name):
    spec=importlib.util.spec_from_file_location(name,path);mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    return mod

def propose(root,action,parent=None):
    root=Path(root);spec,c,_=verify(root)
    require(spec.get('rewrite',{}).get('design') in ('mul8','cla32'),'spec must declare the supported rewrite design')
    require('typed_rules' in c and 'typed_guard' in c,'freeze typed_rules and typed_guard in context')
    cid=reserve(root,parent,action);p=root/'candidates'/cid
    try:
        design=spec['rewrite']['design'];rules=load(c['typed_rules'],'s2g_prior_rules');guard=load(c['typed_guard'],'s2g_prior_guard')
        state=rules.initial(design)
        if parent:state=read(root/'candidates'/parent/'record.json').get('structure_state',state)
        state,block=rules.apply(design,state,action);guard.configure({'stem':design})
        valid,why=guard.check(block);require(valid,'typed guard rejected: '+why)
        original=(root/'frozen/reference.v').read_text()
        start=original.index('assign c1',original.index('module CLA(')) if design=='cla32' else original.index('always @*')
        end=original.index('endmodule',start)
        path=p/'rendered.v';path.write_text(original[:start]+block+'\n'+original[end:])
        rec=submit(root,cid,path)
        with locked(root):
            rec.update(structure_state=state,rules_sha256=digest(c['typed_rules']),guard_sha256=digest(c['typed_guard']))
            write(p/'record.json',rec)
        return rec
    except (ValueError,KeyError,OSError,GateError) as e:
        with locked(root):
            rec=read(p/'record.json');rec.update(status='GENERATION_FAILED',error=str(e));write(p/'record.json',rec)
        raise GateError(str(e)) from e
