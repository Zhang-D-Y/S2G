import random,re
DESIGNS={
 'cla32':dict(stem='cla32',source='Arithmetic/Adder/adder_32bit',file='verified_adder_32bit.v',top='verified_adder_32bit',tb_module='adder_32bit',tb_top='adder32_tb',ports={'A':32,'B':32,'S':32,'C32':1},inputs=['A','B'],outputs=['S','C32'],stratum='arithmetic',region='four carry equations in CLA submodule; eight instances in whole design',neg=['c1 = g1','c1 = ~g1']),
 'mul8':dict(stem='mul8',source='Arithmetic/Multiplier/multi_8bit',file='verified_multi_8bit.v',top='multi_8bit',tb_module='multi_8bit',tb_top='testbench',ports={'A':8,'B':8,'product':16},inputs=['A','B'],outputs=['product'],stratum='arithmetic',region='single combinational conditional accumulation block; declarations fixed',neg=["product = 16'b0","product = 16'b1"]),
}
COMMIT='51ed553d0ffd32797a1a0a13e051656bf302c81f'

def region(text,d):
    if d['stem']=='cla32':
        sub=text.index('module CLA(');start=text.index('assign c1',sub);end=text.index('endmodule',start)
    else:
        start=text.index('always @*');end=text.index('endmodule',start)
    return start,end

def wrapper(d):
    names=list(d['ports']); lines=['module '+d['tb_module']+'('+','.join(names)+');']
    for p,w in d['ports'].items():lines.append(('input' if p in d['inputs'] else 'output')+f' [{w-1}:0] {p};')
    lines+=['TopModule impl('+','.join(f'.{p}({p})' for p in names)+');','endmodule']
    return '\n'.join(lines)+'\n'

def observer(d):
    tb=d['tb_top']; names=','.join(tb+'.'+p for p in d['inputs'])
    return f'''`timescale 1ns/1ns
module observer; integer samples=0,known=0; initial begin repeat(100) begin #9; samples=samples+1; if((^{{{names}}}) !== 1'bx) known=known+1; #1; end end
final $display("OBS samples=%0d known=%0d errors=%0d",samples,known,{tb}.error); endmodule
'''

def extension_tb(d):
    rng=random.Random(20261011);w=d['ports']['A'];outs=d['outputs']
    lines=['`timescale 1ns/1ps','module extended_tb;',f'reg [{w-1}:0] A,B;','integer samples=0,errors=0,i,j;']
    for p in outs:lines.append(f"wire [{d['ports'][p]-1}:0] good_{p},actual_{p};")
    for mod,prefix in [('RefModule','good'),('TopModule','actual')]:lines.append(mod+' '+prefix+'_dut(.A(A),.B(B),'+','.join(f'.{p}({prefix}_{p})' for p in outs)+');')
    check='if ({'+','.join('actual_'+p for p in outs)+'} !== {'+','.join('good_'+p for p in outs)+'}) errors=errors+1;'
    lines+=['task step; begin #1; samples=samples+1; '+check+' end endtask','initial begin']
    if w==8:
        lines+=['for(i=0;i<256;i=i+1) begin for(j=0;j<256;j=j+1) begin A=i[7:0];B=j[7:0];step;end end']
        n=65536
    else:
        pats=[0,(1<<w)-1,1,(1<<(w-1)),(1<<(w-1))-1,0xaaaaaaaa,0x55555555]
        pairs=[(a,b) for a in pats for b in pats]+[(rng.getrandbits(w),rng.getrandbits(w)) for _ in range(4096)]
        for a,b in pairs:lines.append(f"A={w}'h{a:x};B={w}'h{b:x};step;")
        n=len(pairs)
    # Isolate one X/Z bit against directed opposite operands, then mixed data.
    for port in ('A','B'):
        for bit in range(w):
            for c in 'xz':
                for fixed in (0,(1<<w)-1,1):
                    bits=list(format(fixed,f'0{w}b'));bits[w-1-bit]=c
                    other='B' if port=='A' else 'A'
                    lines.append(f"{port}={w}'b{''.join(bits)};{other}={w}'h{fixed:x};step;");n+=1
    for _ in range(128):
        a=''.join(rng.choice('01xz') for _ in range(w));b=''.join(rng.choice('01xz') for _ in range(w))
        lines.append(f"A={w}'b{a};B={w}'b{b};step;");n+=1
    lines+=['$display("EXT_RESULT errors=%0d samples=%0d",errors,samples);','if(errors!=0) $fatal(1,"EXT_FAILED");','$display("EXT_PASSED");','$finish;','end','endmodule']
    return '\n'.join(lines)+'\n',n
