"""Two deterministic structural probes; never included in model prompts."""
import re
import experiment as e
for design in ('cla32','mul8'):
    e.configure(design,1);r=e.PILOT/'dev/complex_screen'/f'{design}_evaluator';m=e.read_json(r/'protocol.json')
    if design=='cla32':
        terms=[]
        for n in range(1,5):
            expr='c0'
            for k in range(1,n+1):expr=f'(g{k} ^ (p{k} & {expr}))'
            terms.append(f'assign c{n} = {expr};')
        block='\n'.join(terms)
    else:
        terms=[f"((B[{i}] === 1'b1) ? ({{8'b0,A}} << {i}) : 16'b0)" for i in range(8)]
        while len(terms)>1:terms=[f'({terms[i]} + {terms[i+1]})' for i in range(0,len(terms),2)]
        block='always @* begin multiplicand=A; shift_count=8; product='+terms[0]+'; end'
    rec={'id':'T0','arm':'T0','round':0,'index':0,'parent':'B0','block':block}
    print(design,'scope',e.check(block),flush=True)
    e.evaluate(r,rec,m,e.load_liberty(m['liberty'],r/'cache'))
    e.write_json(r/'template.json',rec)
    print(design,rec.get('decision'),rec.get('reason'),rec.get('synth',{}).get('metrics',{}).get('area'),flush=True)
