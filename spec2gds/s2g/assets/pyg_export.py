"""Run only in the selected torch/PyG interpreter. Missing values carry masks."""
import json
from pathlib import Path
import sys
import torch
from torch_geometric.data import HeteroData

g=json.loads(Path(sys.argv[1]).read_text());data=HeteroData();index={}
for kind in ('cell','port','net'):
    nodes=[n for n in g['nodes'] if n['kind']==kind]
    data[kind].num_nodes=len(nodes)
    data[kind].names=[n['id'] for n in nodes]
    for i,n in enumerate(nodes):index[n['id']]=(kind,i)
    if kind=='cell':
        fields=('liberty_area_um2','x_um','y_um')
        data[kind].x=torch.tensor([[float(n[k]) if n[k] is not None else float('nan') for k in fields] for n in nodes],dtype=torch.float32).reshape(-1,3)
        data[kind].valid=torch.tensor([[n[k] is not None for k in fields] for n in nodes],dtype=torch.bool).reshape(-1,3)
for kind in ('cell','port'):
    edges=[(index[e['node']][1],index[e['net']][1]) for e in g['edges'] if index[e['node']][0]==kind]
    data[kind,'connects','net'].edge_index=torch.tensor(edges,dtype=torch.long).reshape(-1,2).T.contiguous()
data.sample_id=g['sample_id'];data.prediction_target=g['prediction_target'];data.available_after=g['available_after']
data.validate(raise_on_error=True)
torch.save(data,sys.argv[2]);print('S2G_PYG_EXPORTED')
