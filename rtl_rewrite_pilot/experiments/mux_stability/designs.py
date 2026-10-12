"""Cohort chosen by source structure before model outcomes; no derivative designs."""
import random
DESIGNS = {
 'mux6': dict(stem='Prob076_always_case', selector='sel', selector_width=3, output='out', width=4, ports=[f'data{i}' for i in range(6)], mapping=[f'data{i}' for i in range(6)], default=0, stratum='new_design'),
 'mux4': dict(stem='Prob130_circuit5', selector='c', selector_width=4, output='q', width=4, ports=['a','b','d','e'], mapping=['b','e','a','d'], default=15, stratum='new_design'),
 'mux9': dict(stem='Prob097_mux9to1v', selector='sel', selector_width=4, output='out', width=16, ports=list('abcdefghi'), mapping=list('abcdefghi'), default=65535, stratum='dev_replication'),
}

def extension_tb(d):
    rng=random.Random(20261011); w=d['width']; ports=d['ports']; sw=d['selector_width']; sel=d['selector']; out=d['output']
    patterns=[[f"{w}'h{rng.getrandbits(w):x}" for _ in ports] for _ in range(4)]
    patterns += [[f"{w}'h0"]*len(ports),[f"{w}'h{(1<<w)-1:x}"]*len(ports)]
    patterns += [[f"{w}'b"+''.join(chars[(i+j)%4] for j in range(w)) for i in range(len(ports))] for chars in ('01xz','zx10')]
    conn=', '.join(f'.{p}({p})' for p in ports)+f', .{sel}({sel})'
    lines=['`timescale 1ps/1ps','module extended_tb;',f"reg [{w-1}:0] "+', '.join(ports)+';',f'reg [{sw-1}:0] {sel};',f'wire [{w-1}:0] good,actual;','integer samples=0,errors=0;',f'RefModule ref_dut({conn}, .{out}(good));',f'TopModule dut({conn}, .{out}(actual));','initial begin']
    for pattern in patterns:
        lines += [f'{p}={v};' for p,v in zip(ports,pattern)]
        for n in range(4**sw):
            bits=''.join('01xz'[(n>>(2*k))&3] for k in reversed(range(sw)))
            lines += [f"{sel}={sw}'b{bits}; #1; samples=samples+1; if(actual !== good) errors=errors+1;"]
    lines += ['$display("EXT_RESULT errors=%0d samples=%0d", errors,samples);','if(errors != 0) $fatal(1,"EXT_FAILED");','$display("EXT_PASSED");','$finish;','end','endmodule']
    return '\n'.join(lines)+'\n'
