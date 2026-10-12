from pathlib import Path
import subprocess,json
P=Path('/data1/zhangdy/S2G');root=P/'rtl_rewrite_pilot/dev/complex_screen';repo=P/'third_party/RTLLM';iver=P/'tools/iverilog-v12/bin'
items=[('fixed_add','Arithmetic/Other/fixed_point_adder','verified_fixed_point_adder.v','fixed_point_adder','testbench','a','b'),('fixed_sub','Arithmetic/Other/fixed_point_substractor','verified_fixed_point_substractor.v','fixed_point_subtractor','tb_fixed_point_subtractor','a','b'),('mul8','Arithmetic/Multiplier/multi_8bit','verified_multi_8bit.v','multi_8bit','testbench','A','B'),('cla32','Arithmetic/Adder/adder_32bit','verified_adder_32bit.v','adder_32bit','adder32_tb','A','B')]
rows=[]
for name,folder,rtl,top,tb,a,b in items:
 d=root/name;d.mkdir(exist_ok=False);src=repo/folder
 text=(src/rtl).read_text().replace('module verified_'+top,'module '+top,1);(d/'dut.v').write_text(text)
 (d/'observer.sv').write_text(f'''`timescale 1ns/1ns
module observer; integer known=0,samples=0; initial begin repeat(100) begin #9; samples=samples+1; if((^{{{tb}.{a},{tb}.{b}}}) !== 1'bx) known=known+1; #1; end end
final $display("OBS samples=%0d known=%0d",samples,known); endmodule
''')
 cmd=[str(iver/'iverilog'),'-g2012','-s',tb,'-s','observer','-o',str(d/'sim'),str(d/'dut.v'),str(src/'testbench.v'),str(d/'observer.sv')]
 c=subprocess.run(cmd,capture_output=True,text=True,timeout=60);(d/'compile.log').write_text(c.stdout+c.stderr)
 run=subprocess.run([str(iver/'vvp'),str(d/'sim')],cwd=d,capture_output=True,text=True,timeout=30) if c.returncode==0 else None
 log=(run.stdout+run.stderr) if run else '';(d/'sim.log').write_text(log)
 row={'design':name,'source':str(src),'compile':c.returncode,'simulation':run.returncode if run else None,'tail':log.splitlines()[-4:]};rows.append(row);print(row,flush=True)
(root/'tb_screen.json').write_text(json.dumps(rows,indent=2)+'\n')
