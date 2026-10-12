#!/usr/bin/env python3
"""Real local tool integration with builtin review; never invokes a model provider."""
import argparse
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from s2g import contracts,workspace,pipeline,search
from s2g.common import identity,read,write

p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);a=p.parse_args()
b=Path(__file__).resolve().parents[1];e=b/'examples/adder8';r=a.root.resolve()
contracts.review(e/'spec.json','builtin demonstration author',identity(read(e/'spec.json')),True)
workspace.init(r,e/'spec.json',e/'golden.v',e/'tb.v',e/'constraints.sdc',b/'configs/local-nangate45.json')
summary={'schema':'s2g.demo.v1','model_calls':0,'runs':[]}
for name in ('golden.v','ripple.v'):
    cid=workspace.add(r,e/name)['id']
    result=pipeline.evaluate(r,cid)
    summary['runs'].append(result);print(result,flush=True)
# Reuse the ordinary request/response boundary with an explicitly offline fixture.
request=search.request(r,'c0000','graph');cid=request['candidate']
workspace.submit(r,cid,e/'ripple.v')
summary['offline_transport_run']=pipeline.evaluate(r,cid,'synthesis')
bad=r/'negative_control.v';bad.write_text((e/'golden.v').read_text().replace(' + ',' - '))
cid=workspace.add(r,bad,action={'op':'negative_control_subtract'})['id']
summary['negative_control']=pipeline.evaluate(r,cid,'synthesis')
summary['pool']=search.pool(r)
write(r/'splits.json',{'demo_adder8':'dev'})
search.dataset(r,r/'dataset.json',r/'splits.json')
write(r/'DEMO.json',summary)
print('Demo evidence:',r/'DEMO.json',flush=True)
