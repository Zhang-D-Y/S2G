from pathlib import Path
import re
from .common import *

def yosys_script(ctx, folder, name, script, marker=None):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    p=folder/(name+'.ys');p.write_text(script+'\n')
    return checked_job([ctx['tools']['yosys'],'-s',p],folder,folder/(name+'.log'),ctx['timeout_s'],marker)

def functional(root, run, rtl, spec, ctx):
    require(not spec['clocks'] and not spec['resets'] and spec['latency_cycles']==0,
            'sequential contract requires a qualified sequential equivalence adapter; unsupported in v0.1')
    run=Path(run);top=spec['top'];tools=ctx['tools'];gold=Path(root)/'frozen/reference.v'
    for label,path in [('candidate',rtl),('reference',gold)]:
        text=Path(path).read_text()
        text=re.sub(r'/\*.*?\*/|//[^\n]*','',text,flags=re.S)
        text=re.sub(r'`timescale[^\n]*','',text)
        require('$' not in text and '`' not in text and not re.search(r'\binitial\b|#\s*\d',text),
                'combinational RTL must not contain system tasks, directives, initial blocks or delays')
        yosys_script(ctx,run,'lint_'+label,
            f'read_verilog -sv {ys(path)}\nhierarchy -check -top {top}\nproc\nflatten\nopt\ncheck -assert\nwrite_json {ys(run/(label+".json"))}')
        mod=read(run/(label+'.json'))['modules'][top]
        actual={k:{'direction':p['direction'],'width':len(p['bits']),'signed':bool(p.get('signed',0))} for k,p in mod['ports'].items()}
        require(actual==spec['ports'],'interface differs from frozen spec')
        require(not any(any(x in cell['type'].lower() for x in ('dff','latch','mem','$ff')) for cell in mod['cells'].values()), 'state inferred in combinational contract')
        sim=run/(label+'.vvp')
        checked_job([tools['iverilog'],'-g2012','-s',spec['verification']['tb_top'],'-o',sim,path,Path(root)/'frozen/tb.v'],run,run/(label+'_compile.log'))
        log=run/(label+'_sim.log')
        checked_job([tools['vvp'],sim],run,log,ctx['timeout_s'])
        text=log.read_text();v=spec['verification']
        matches=re.findall(re.escape(v['pass_marker'])+r' checks=(\d+) failures=(\d+)',text)
        require(matches and all(int(n)>=v['minimum_checks'] and int(f)==0 for n,f in matches)
                and not re.search(r'\b(?:FATAL|ERROR|S2G_FAIL)\b',text), 'TB lacks positive passing evidence')
    proof(ctx,run,'rtl_equivalence',gold,rtl,top)
    return {'status':'PASS','domain':'all binary inputs, combinational','reference_also_tested':True}

def proof(ctx, run, name, golden, candidate, top, mapped=False):
    lib=Path(ctx['platform_dir'])/ctx['liberty']
    # Separate synthesis stashes prevent module/name collisions. Library is expanded only in gate.
    def side(path,label,celllib=False):
        s='design -reset\n'
        if celllib:s+=f'read_liberty -ignore_miss_func {ys(lib)}\n'
        s+=f'read_verilog -sv {ys(path)}\nhierarchy -top {top}\nproc\nflatten\nopt\n'
        s+=f'rename {top} {label}\ndesign -stash {label}\n'
        return s
    script=side(golden,'gold')+side(candidate,'gate',mapped)
    script+='design -reset\ndesign -copy-from gold -as gold gold\ndesign -copy-from gate -as gate gate\n'
    script+='miter -equiv -flatten gold gate miter\nhierarchy -top miter\nopt_clean\n'
    script+='sat -verify -prove trigger 0 -show-inputs -show-outputs miter\n'
    yosys_script(ctx,run,name,script,'SAT proof finished - no model found: SUCCESS!')
    return {'status':'PASS','method':'exhaustive symbolic combinational equivalence','two_state':True}

def synth(root,run,rtl,spec,ctx):
    top=spec['top'];lib=Path(ctx['platform_dir'])/ctx['liberty'];mapped=run/'mapped.v'
    excludes=' '.join('-dont_use '+n for n in ctx['dont_use'])
    # Split synth so the coarse netlist (with src attributes) feeds RTL provenance; same passes as one call.
    script=f'read_verilog -sv {ys(rtl)}\nsynth -top {top} -flatten -run begin:fine\nwrite_json {ys(run/"coarse.json")}\n'
    script+=f'synth -top {top} -flatten -run fine:\n'  # tee -o below is cwd-relative: Yosys rejects quoted tee paths
    script+=f'dfflibmap -liberty {ys(lib)}\nabc -liberty {ys(lib)} {excludes}\n'
    script+='hilomap -singleton -hicell '+' '.join(ctx['tiehi'])+' -locell '+' '.join(ctx['tielo'])+'\n'
    script+='insbuf -buf '+' '.join(ctx['buffer'])+'\n'
    script+=f'clean\nread_liberty -lib {ys(lib)}\ncheck -assert\ntee -q -o stat.json stat -json -liberty {ys(lib)}\nstat -liberty {ys(lib)}\nwrite_verilog -noattr -noexpr {ys(mapped)}\nwrite_json {ys(run/"mapped.json")}\n'
    yosys_script(ctx,run,'synthesis',script)
    # write_verilog renames internal $abc identifiers. Read the emitted netlist
    # back so graph instance IDs are the exact IDs consumed by OpenROAD.
    yosys_script(ctx,run,'canonical_netlist',
                 f'read_liberty -lib {ys(lib)}\nread_verilog {ys(mapped)}\nhierarchy -check -top {top}\nwrite_json {ys(run/"mapped.json")}')
    proof(ctx,run,'mapped_equivalence',Path(root)/'frozen/reference.v',mapped,top,True)
    mod=read(run/'mapped.json')['modules'][top]
    return {'status':'PASS','netlist_sha256':digest(mapped),'cells':len(mod['cells']), 'mapped_proof':'PASS'}
