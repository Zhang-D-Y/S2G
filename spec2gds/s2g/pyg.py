"""Optional local JSON-to-PyG exporter; no upstream script execution."""
from pathlib import Path
from .common import *
from .graphs import validate

def export(graph,out,python):
    validate(graph)
    out=Path(out).resolve();require(not out.exists(),'PyG destination must be new')
    out.mkdir(parents=True)
    j=job([python,Path(__file__).parent/'assets/pyg_export.py',Path(graph).resolve(),out/'graph.pt'],out,out/'export.log',120)
    status='PASS' if j['exit_code']==0 and (out/'graph.pt').is_file() and 'S2G_PYG_EXPORTED' in (out/'export.log').read_text() else 'BLOCKED_OR_FAILED'
    rec={'schema':'s2g.pyg-export.v1','status':status,'job':j,'input_sha256':digest(graph),
         'output_sha256':digest(out/'graph.pt') if (out/'graph.pt').is_file() else None,
         'semantics':'S2G incidence graph, not the upstream b-f or 8-relation R2G schema'}
    write(out/'record.json',rec);return rec
