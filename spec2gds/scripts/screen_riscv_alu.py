#!/usr/bin/env python3
"""Import and qualify a local 32-bit RISC-V ALU without model calls.

External RTL is read only during --prepare. Every subsequent dependency is inside
S2G except the EDA tools and process assets named in the implementation context.
The fixed 6 ns virtual clock is a development stress constraint, not a production
requirement. All four candidates use the same contract and physical schedule.
"""
import argparse
import re
import shutil
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from s2g import pipeline, workspace
from s2g.common import checked_job, digest, identity, read, require, stamp, write


TB = r"""module tb;
  reg [3:0] op;
  reg [31:0] a, b;
  wire [31:0] actual;
  reg [31:0] expected, rng;
  integer checks = 0, failures = 0, oi, i, j;
  riscv_alu dut(.alu_op_i(op), .alu_a_i(a), .alu_b_i(b), .alu_p_o(actual));

  function [31:0] oracle;
    input [3:0] f;
    input [31:0] x, y;
    begin
      case (f)
        1: oracle = x << y[4:0];
        2: oracle = x >> y[4:0];
        3: oracle = $signed(x) >>> y[4:0];
        4: oracle = x + y;
        6: oracle = x - y;
        7: oracle = x & y;
        8: oracle = x | y;
        9: oracle = x ^ y;
        10: oracle = {31'b0, x < y};
        11: oracle = {31'b0, $signed(x) < $signed(y)};
        default: oracle = x;
      endcase
    end
  endfunction

  function [31:0] corner;
    input integer n;
    begin
      case (n)
        0: corner=0; 1: corner=1; 2: corner=32'hffffffff;
        3: corner=32'h80000000; 4: corner=32'h7fffffff;
        5: corner=32'haaaaaaaa; 6: corner=32'h55555555;
        7: corner=31; 8: corner=32; 9: corner=63;
        10: corner=32'hffff0000; default: corner=32'h0000ffff;
      endcase
    end
  endfunction

  task next_random;
    begin
      rng = rng ^ (rng << 13);
      rng = rng ^ (rng >> 17);
      rng = rng ^ (rng << 5);
    end
  endtask

  task check;
    begin
      #1;
      expected = oracle(op, a, b);
      checks = checks + 1;
      if (actual !== expected) begin
        failures = failures + 1;
        $display("S2G_FAIL op=%h a=%h b=%h actual=%h expected=%h", op,a,b,actual,expected);
        $fatal(1, "independent ALU oracle mismatch");
      end
    end
  endtask

  initial begin
    rng = 32'h13579bdf;
    for (oi=0; oi<16; oi=oi+1) begin
      op=oi;
      for (i=0; i<12; i=i+1)
        for (j=0; j<12; j=j+1) begin
          a=corner(i); b=corner(j); check;
        end
      for (i=0; i<4096; i=i+1) begin
        next_random; a=rng;
        next_random; b=rng; check;
      end
    end
    $display("S2G_PASS checks=%0d failures=%0d", checks, failures);
    $finish;
  end
endmodule
"""


def candidate(header, share_adder, share_shift):
    text = header + "\nmodule riscv_alu(input [3:0] alu_op_i,\n"
    text += "  input [31:0] alu_a_i, alu_b_i, output reg [31:0] alu_p_o);\n"
    if share_adder:
        text += """  wire subtract = (alu_op_i == 6) || (alu_op_i == 10) || (alu_op_i == 11);
  wire [31:0] b_add = alu_b_i ^ {32{subtract}};
  wire [32:0] sum = {1'b0, alu_a_i} + {1'b0, b_add} + subtract;
  wire unsigned_lt = ~sum[32];
  wire signed_lt = (alu_a_i[31] != alu_b_i[31]) ? alu_a_i[31] : sum[31];
"""
    if share_shift:
        text += """  wire left = (alu_op_i == 1);
  wire fill = (alu_op_i == 3) && alu_a_i[31];
  wire [31:0] reversed_a, shift_input, shifted, shift_result;
  genvar k;
  generate for (k=0; k<32; k=k+1) begin: reverse_bits
    assign reversed_a[k] = alu_a_i[31-k];
    assign shift_result[k] = left ? shifted[31-k] : shifted[k];
  end endgenerate
  assign shift_input = left ? reversed_a : alu_a_i;
  wire signed [32:0] signed_input = {fill, shift_input};
  assign shifted = signed_input >>> alu_b_i[4:0];
"""
    text += "  always @* begin\n    case (alu_op_i)\n"
    text += ("      1, 2, 3: alu_p_o = shift_result;\n" if share_shift else
             "      1: alu_p_o = alu_a_i << alu_b_i[4:0];\n"
             "      2: alu_p_o = alu_a_i >> alu_b_i[4:0];\n"
             "      3: alu_p_o = $signed(alu_a_i) >>> alu_b_i[4:0];\n")
    text += ("      4, 6: alu_p_o = sum[31:0];\n" if share_adder else
             "      4: alu_p_o = alu_a_i + alu_b_i;\n"
             "      6: alu_p_o = alu_a_i - alu_b_i;\n")
    text += """      7: alu_p_o = alu_a_i & alu_b_i;
      8: alu_p_o = alu_a_i | alu_b_i;
      9: alu_p_o = alu_a_i ^ alu_b_i;
"""
    text += ("      10: alu_p_o = {31'b0, unsigned_lt};\n"
             "      11: alu_p_o = {31'b0, signed_lt};\n" if share_adder else
             "      10: alu_p_o = {31'b0, alu_a_i < alu_b_i};\n"
             "      11: alu_p_o = {31'b0, $signed(alu_a_i) < $signed(alu_b_i)};\n")
    return text + "      default: alu_p_o = alu_a_i;\n    endcase\n  end\nendmodule\n"


def prepare(root, source_dir):
    require(not root.exists(), "screen root must be new")
    root.mkdir(parents=True)
    inputs = root / 'inputs'
    original = inputs / 'original'
    original.mkdir(parents=True)
    for name in ('riscv_alu.v', 'riscv_defs.v'):
        shutil.copy2(source_dir / name, original / name)
    source = (original / 'riscv_alu.v').read_text()
    defs = dict(re.findall(r'^`define\s+(ALU_\w+)\s+(4\'b[01]{4})',
                           (original / 'riscv_defs.v').read_text(), flags=re.M))
    golden = source.replace('`include "riscv_defs.v"', '// ALU opcode macros expanded from the copied header.')
    golden = re.sub(r'`(ALU_\w+)', lambda m: defs[m[1]], golden)
    require('`' not in re.sub(r'//[^\n]*|/\*.*?\*/', '', golden, flags=re.S), "unresolved macro")
    (inputs / 'golden.v').write_text(golden)
    (inputs / 'tb.v').write_text(TB)
    header = source[:source.index('module riscv_alu')]
    variants = [('original', golden), ('shared_adder', candidate(header, True, False)),
                ('shared_shifter', candidate(header, False, True)),
                ('shared_both', candidate(header, True, True))]
    # System functions are prohibited in candidate RTL. Explicit signed wires
    # preserve the intended shift/comparison semantics without $signed casts.
    for name, text in variants[1:]:
        text = text.replace('  always @* begin',
                            '  wire signed [31:0] signed_a = alu_a_i, signed_b = alu_b_i;\n  always @* begin')
        text = text.replace('$signed(alu_a_i)', 'signed_a').replace('$signed(alu_b_i)', 'signed_b')
        (inputs / (name + '.v')).write_text(text)
    (inputs / 'constraints.sdc').write_text(
        '# Development stress target, not an owner production requirement.\n'
        'create_clock -name virtual_io -period 6.0\n'
        'set_input_delay -clock virtual_io -max 2.0 [all_inputs]\n'
        'set_input_delay -clock virtual_io -min 0.2 [all_inputs]\n'
        'set_output_delay -clock virtual_io -max 2.0 [all_outputs]\n'
        'set_output_delay -clock virtual_io -min 0.2 [all_outputs]\n'
        'set_driving_cell -lib_cell BUF_X1 -pin Z [all_inputs]\n'
        'set_load 0.005 [all_outputs]\n')
    spec = dict(schema='s2g.spec.v1', design_family='ultraembedded_riscv_alu32', top='riscv_alu',
                ports={n: dict(direction=d, width=w, signed=False) for n,d,w in
                       [('alu_op_i','input',4), ('alu_a_i','input',32), ('alu_b_i','input',32), ('alu_p_o','output',32)]},
                behavior='Original RV32 combinational ALU: shifts, add/sub, Boolean operations, signed/unsigned comparisons; unused opcodes return A.',
                arithmetic='32-bit modular arithmetic; shift amount B[4:0]; signed comparisons interpret two-complement 32-bit operands.',
                clocks=[], resets=[], latency_cycles=0, throughput='combinational', protocol='always active',
                legal_inputs='all 68-bit binary input combinations; X/Z excluded', parameters={}, open_questions=[],
                requirements=[dict(id='ALU', text='Preserve all original outputs for every binary opcode and operand.')],
                verification=dict(tb_top='tb', pass_marker='S2G_PASS', minimum_checks=67840,
                                  reference_origin='Local user-supplied RTL copy; source hashes in provenance.json',
                                  test_origin='S2G independent behavioral ALU oracle; 2304 directed plus 65536 seeded tests covering all 16 opcodes'))
    write(inputs / 'spec.json', spec)
    write(inputs / 'spec.json.review.json', dict(schema='s2g.review.v1', spec_hash=identity(spec),
          scope='engineering_screen', reviewer='S2G screening author; not a user production-spec attestation', reviewed_at=stamp()))
    ctx = read(BASE / 'configs/local-nangate45.json')
    # The 40 um demo core cannot fit this ALU at the frozen 40% density.
    # Use one common, fixed 70 um core for every variant and retain 40% density.
    ctx.update(candidate_budget=8, tool_run_budget=16, timeout_s=600,
               die_area=[0, 0, 90, 90], core_area=[10, 10, 80, 80])
    write(inputs / 'context.json', ctx)
    write(inputs / 'provenance.json', dict(source_files={str(p):digest(p) for p in source_dir.glob('riscv_*.v')
          if p.name in ('riscv_alu.v','riscv_defs.v')}, imported_files={p.name:digest(p) for p in original.iterdir()},
          preparation='Copy unmodified sources; inline only referenced ALU opcode constants; preserve original license notices.',
          macros=defs, golden_sha256=digest(inputs/'golden.v'), tb_sha256=digest(inputs/'tb.v')))
    # Qualify the unmodified, include-based original against the same independent oracle.
    q = root / 'original_qualification'
    q.mkdir()
    sim = q / 'original.vvp'
    checked_job([ctx['tools']['iverilog'], '-g2012', '-I', original, '-s', 'tb', '-o', sim,
                 original/'riscv_alu.v', inputs/'tb.v'], q, q/'compile.log')
    checked_job([ctx['tools']['vvp'], sim], q, q/'simulation.log', 600, 'S2G_PASS checks=67840 failures=0')
    # A real mutation must fail the independent checker before backend work.
    bad = q/'negative.v'
    bad.write_text(candidate(header, True, True).replace('alu_p_o = alu_a_i;', 'alu_p_o = ~alu_a_i;'))
    checked_job([ctx['tools']['iverilog'], '-g2012', '-s', 'tb', '-o', q/'negative.vvp', bad, inputs/'tb.v'], q, q/'negative_compile.log')
    from s2g.common import job
    result = job([ctx['tools']['vvp'], q/'negative.vvp'], q, q/'negative_sim.log', 600)
    require(result['exit_code'] != 0 and 'S2G_FAIL' in (q/'negative_sim.log').read_text(), 'negative control was not caught')
    write(q/'negative_control.json', result)
    ws = root/'workspace'
    workspace.init(ws, inputs/'spec.json', inputs/'golden.v', inputs/'tb.v', inputs/'constraints.sdc', inputs/'context.json')
    records = []
    for name, _ in variants:
        path = inputs/'golden.v' if name=='original' else inputs/(name+'.v')
        rec = workspace.add(ws, path, action={'op':'deterministic_screen','variant':name})
        records.append({'variant':name,'candidate':rec['id'],'rtl_sha256':rec['rtl_hash']})
    write(root/'screen.json', dict(schema='s2g.alu-screen.v1', model_calls=0, created_at=stamp(),
          variants=records, workspace=str(ws), constraints='6 ns stress clock, max IO delays 2 ns, all variants frozen together',
          schedule='synthesize all four; physically evaluate all four that pass functional/mapped checks regardless of area ranking'))
    print('PREPARED', str(root), flush=True)


def evaluate(root, stage):
    screen = read(root/'screen.json')
    ws = Path(screen['workspace'])
    for rec in screen['variants']:
        cid = rec['candidate']
        receipt = root/'results'/stage/(cid+'.json')
        if receipt.exists():
            old = read(receipt)
            if old.get('run'):
                pipeline.run_evidence(ws, cid, Path(old['run']).name)
            print(stage, rec['variant'], 'CACHED', flush=True)
            continue
        if stage=='physical':
            syn = read(root/'results/synthesis'/(cid+'.json'))
            if syn['status'] != 'COMPLETE':
                write(receipt, dict(status='NOT_RUN', reason='mandatory synthesis gates did not complete'))
                continue
        result = pipeline.evaluate(ws, cid, 'synthesis' if stage=='synthesis' else 'finish')
        write(receipt, result)
        run = Path(result['run'])
        evidence = read(run/'synthesis_evidence.json') if (run/'synthesis_evidence.json').exists() else None
        label = result['acceptance']['status'] if stage == 'physical' else 'SYNTHESIS_ONLY'
        print(stage, rec['variant'], result['status'], label,
              {k:v['value'] for k,v in evidence['metrics'].items() if k in ('mapped_area','logic_depth','setup_ws')} if evidence else result['error'], flush=True)


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--root', required=True, type=Path)
    ap.add_argument('--prepare', type=Path, metavar='SOURCE_RTL_DIR')
    ap.add_argument('--stage', choices=('synthesis','physical'))
    args=ap.parse_args();root=args.root.resolve()
    require(bool(args.prepare) != bool(args.stage), 'choose --prepare or --stage')
    if args.prepare:prepare(root,args.prepare.resolve())
    else:evaluate(root,args.stage)


if __name__=='__main__':
    main()
