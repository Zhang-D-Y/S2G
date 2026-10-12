#!/usr/bin/env python3
"""Positive and signal/body negative controls for the S2G Nangate45 LVS adapter."""
import argparse
from pathlib import Path
import re
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from s2g.common import read,write,digest,job,require


def qualify(run,context,out):
    run=Path(run).resolve();out=Path(out).resolve();c=read(context)
    require(not out.exists(),'qualification output must be new');out.mkdir(parents=True)
    signoff=read(run/'signoff.json');gds=Path(signoff['gds']['path']);cdl=run/'signoff/reference.cdl'
    original=cdl.read_text();before={str(p):digest(p) for p in (gds,cdl)}
    # The last SUBCKT is the top emitted by OpenROAD after the cell library.
    starts=list(re.finditer(r'^\.SUBCKT\s+',original,re.M));require(starts,'CDL has no subcircuit')
    start=starts[-1].start();prefix,top=original[:start],original[start:]
    lines=top.splitlines();idx=next(i for i,line in enumerate(lines) if line.startswith('X'))
    tokens=lines[idx].split();require(tokens[1]!='VSS','negative-control target already grounded')
    tokens[1]='VSS';lines[idx]=' '.join(tokens)
    marker='M_i_2 VSS A Z_neg VSS NMOS_VTL'
    require(original.count(marker)>=1,'expected BUF_X1 NMOS model missing')
    variants={'positive':original,'signal_fault':prefix+'\n'.join(lines)+'\n',
              'body_fault':original.replace(marker,'M_i_2 VSS A Z_neg VDD NMOS_VTL',1)}
    deck=Path(c['platform_dir'])/c['lvs_deck'];checks={}
    for name,text in variants.items():
        case=out/name;case.mkdir();ref=case/'reference.cdl';ref.write_text(text);report=case/'report.lvsdb'
        args=[c['tools']['klayout'],'-b','-rd',f'input={gds}','-rd',f'schematic={ref}',
              '-rd',f'target_netlist={case/"extracted.cir"}','-rd',f'report_file={report}','-r',deck]
        result=job(args,case,case/'lvs.log',c['timeout_s']);log=(case/'lvs.log').read_text()
        expected='CONGRATULATIONS! Netlists match.' if name=='positive' else "ERROR : Netlists don't match"
        passed=result['exit_code']==0 and not result['timed_out'] and report.is_file() and expected in log
        checks[name]={'status':'PASS' if passed else 'FAIL','expected':'MATCH' if name=='positive' else 'MISMATCH',
                      'job':result,'reference_sha256':digest(ref),'report_sha256':digest(report) if report.exists() else None}
    require(before=={str(p):digest(p) for p in (gds,cdl)},'qualification modified original input')
    result={'schema':'s2g.lvs-qualification.v1','status':'PASS' if all(x['status']=='PASS' for x in checks.values()) else 'FAIL',
            'source_artifacts':before,'deck_sha256':digest(deck),'checks':checks,
            'scope':'Nangate45 research standard-cell body convention; no manufacturing tap-connectivity qualification'}
    write(out/'qualification.json',result)
    return result

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('run','context','out'):p.add_argument('--'+name,required=True)
    a=p.parse_args();r=qualify(a.run,a.context,a.out);print(r['status'],a.out)
    sys.exit(0 if r['status']=='PASS' else 1)
