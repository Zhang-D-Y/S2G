"""Stdlib research graphs using pinned R2G parsers; not a PyG/b-f compatibility claim."""
import importlib.util
from pathlib import Path
import re
from .common import *

BOUNDARIES={'floorplan':None,'placement':'floorplan','cts':'placement','route':'cts'}

def parser(ctx,name):
    path=Path(ctx['graph_parser_dir'])/(name+'.py')
    spec=importlib.util.spec_from_file_location('s2g_r2g_'+name,path)
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    return mod

def norm(name):
    return name.replace('\\','')

def build(root,run,spec,c,ledger):
    base=read(run/'mapped.json')['modules'][spec['top']]
    lib=parser(c,'liberty').load_liberty_db([str(Path(c['platform_dir'])/c['liberty'])])
    sizes=parser(c,'lef').macro_sizes(str(Path(c['platform_dir'])/c['cell_lef']))
    dp=parser(c,'def_parse');source=read(run/'candidate.json')['modules'][spec['top']]
    sources={norm(k):v.get('attributes',{}).get('src') for k,v in source.get('netnames',{}).items()}
    # Only same-name signal attributes establish a source association. ABC cells usually lose src.
    nodes=[];edges=[];bits=set();portnodes=[]
    for name,p in base['ports'].items():
        portnodes.append({'id':'port:'+name,'kind':'port','name':name,'direction':p['direction']})
        for i,b in enumerate(p['bits']):
            bits.add(str(b));edges.append({'node':'port:'+name,'net':'net:'+str(b),'pin':str(i)})
    for name,cell in sorted(base['cells'].items()):
        master=cell['type'];area=lib['cells'].get(master.upper(),{}).get('area');size=sizes.get(master)
        netlist_src=cell.get('attributes',{}).get('src')
        # Re-read mapped Verilog locations are not original RTL locations.
        src=None
        nodes.append({'id':'cell:'+name,'kind':'cell','name':name,'master':master,'liberty_area_um2':area,'area_valid':area is not None,
                      'lef_size_um':size,'lef_valid':size is not None,'source':src,'netlist_source':netlist_src,'source_confidence':'module_only'})
        for pin,bb in cell['connections'].items():
            for b in bb:bits.add(str(b));edges.append({'node':'cell:'+name,'net':'net:'+str(b),'pin':pin})
    net_sources={}
    for name,n in base.get('netnames',{}).items():
        if sources.get(norm(name)):
            for bit in n['bits']:net_sources.setdefault(str(bit),set()).add(sources[norm(name)])
    nets=[{'id':'net:'+b,'kind':'net','source_regions':sorted(net_sources.get(b,set())),
           'source_confidence':'same_signal_name' if b in net_sources else 'module_only'} for b in sorted(bits)]
    rec=read(run.parent/'record.json');output=[]
    for target,available in BOUNDARIES.items():
        if available and available not in ledger['stages']:continue
        components={};dbu=1;artifacts=[run/'mapped.json']
        if available:
            stage=ledger['stages'][available];verify_manifest(stage['artifacts'])
            defpath=run/'stages'/available/'design.def'
            require(re.search(r'UNITS\s+DISTANCE\s+MICRONS\s+\d+',defpath.read_text()),'DEF units missing')
            components={norm(k):v for k,v in dp.parse_components(str(defpath)).items()};dbu=dp.parse_units(str(defpath))
            require(len(components)==len(dp.parse_components(str(defpath))),'normalized DEF instance collision')
            artifacts+=list(map(Path,stage['artifacts']))
        gate_nodes=[]
        for n in nodes:
            node=dict(n);comp=components.get(norm(n['name']));valid=bool(comp and comp['x'] is not None and comp['y'] is not None)
            if target=='placement':valid=bool(valid and comp['status']=='FIXED')
            node.update(x_um=comp['x']/dbu if valid else None,y_um=comp['y']/dbu if valid else None,
                        placement_valid=valid,present_in_stage=comp is not None if available else None,
                        stage_master=comp['master'] if comp else None)
            gate_nodes.append(node)
        if available and nodes:
            require(any(n['present_in_stage'] for n in gate_nodes),'no canonical instances join to DEF; graph identity adapter failed')
        graph={'schema':'s2g.stage-graph.v1','identity_version':2,
               'sample_id':identity([rec['freeze_hash'],rec['rtl_hash'],rec['id'],run.name,target]),
               'freeze_hash':rec['freeze_hash'],'rtl_hash':rec['rtl_hash'],
               'candidate':rec['id'],'run':run.name,'spec_hash':rec['spec_hash'],'context_hash':rec['context_hash'],
               'prediction_target':target,'available_after':available or 'synthesis','r2g_commit':read(root/'freeze.json')['r2g_commit'],
               'sources':files_manifest(artifacts),'nodes':gate_nodes+portnodes+nets,'edges':edges,
               'feature_semantics':'fixed post-synthesis incidence topology; surviving instance origins only, not pin geometry; no labels or final-route features',
               'coverage':{'cells':len(nodes),'placed_cells':sum(n['placement_valid'] for n in gate_nodes),
                           'cell_src_attributes':sum(bool(n['source']) for n in nodes),'signal_src_nets':len(net_sources)}}
        path=run/'graphs'/f'{target}.json';write(path,graph);validate(path);output.append(str(path))
    # Label store intentionally separate from every graph and provider early-feature payload.
    labels={'schema':'s2g.labels.v1','candidate':rec['id'],'run':run.name,
            'risk':read(run/'risk.json') if (run/'risk.json').is_file() else None,
            'signoff':read(run/'signoff.json') if (run/'signoff.json').is_file() else None,
            'outcome':ledger['status'],'valid':(run/'risk.json').is_file()}
    write(run/'labels.json',labels)
    return output

def validate(path):
    g=read(path);require(g['schema']=='s2g.stage-graph.v1','graph schema mismatch')
    if g.get('identity_version')==2:
        require(g['sample_id']==identity([g['freeze_hash'],g['rtl_hash'],g['candidate'],g['run'],g['prediction_target']]),'graph identity mismatch')
    require(g['prediction_target'] in BOUNDARIES,'unknown prediction target')
    require(g['available_after']==(BOUNDARIES[g['prediction_target']] or 'synthesis'),'future-stage feature leakage')
    require('labels' not in g and 'risk' not in g and 'signoff' not in g,'labels inside feature graph')
    verify_manifest(g['sources']);ids={n['id'] for n in g['nodes']}
    run=Path(path).resolve().parent.parent
    if (run/'ledger.json').is_file():
        expected=[run/'mapped.json']
        available=BOUNDARIES[g['prediction_target']]
        if available:
            rec=read(run/'stages'/available/'record.json')
            require(rec['stage']==available and rec['status']=='COMPLETE','stage identity mismatch')
            expected+=list(map(Path,rec['artifacts']))
        require(g['sources']==files_manifest(expected),'graph contains artifacts outside its stage boundary')
        if available:
            c=read(run.parents[2]/'frozen/context.json');dp=parser(c,'def_parse')
            path=run/'stages'/available/'design.def'
            comps={norm(k):v for k,v in dp.parse_components(str(path)).items()};dbu=dp.parse_units(str(path))
            for n in g['nodes']:
                if n['kind']!='cell':continue
                comp=comps.get(norm(n['name']))
                valid=bool(comp and comp['x'] is not None and comp['y'] is not None)
                if g['prediction_target']=='placement':valid=bool(valid and comp['status']=='FIXED')
                require(n['placement_valid']==valid,'DEF truth differs from graph mask')
                if valid:require(n['x_um']==comp['x']/dbu and n['y_um']==comp['y']/dbu,'DEF truth differs from graph coordinates')
    require(len(ids)==len(g['nodes']),'duplicate node identities')
    for e in g['edges']:require(e['node'] in ids and e['net'] in ids,'dangling graph edge')
    for n in g['nodes']:
        if n['kind']=='cell':
            require(n['placement_valid']==(n['x_um'] is not None and n['y_um'] is not None),'placement mask mismatch')
            if g['prediction_target']=='floorplan':require(not n['placement_valid'],'synthesis graph contains placement')
    return {'status':'PASS','nodes':len(ids),'edges':len(g['edges'])}
