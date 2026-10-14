#!/usr/bin/env python3
"""Qualify a held-out ALU: the combinational core of kleine-riscv's two-stage ALU (no model calls).

The local copy has no license file, so the derived RTL stays under git-ignored runs/. Derivation
(clk, pipeline registers and the first-cycle add_result output removed; the second-cycle select
uses function_select directly; every expression unchanged) is checked by co-simulating the original
registered ALU against the derived core on seeded random vectors, independently of the semantic map.
The golden then must pass the map-derived oracle TB and full physical acceptance before any search.
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from s2g import alu, pipeline, workspace
from s2g.common import digest, identity, read, require, stamp, write

SOURCE = 'kleine_riscv_core/rtl/alu.v'
DERIVED = """// Derived combinational core of kleine-riscv src/units/alu.v (local copy sha256 {sha}).
// Removed: clk, pipeline registers result_* and old_function, first-cycle output add_result.
// The second-cycle select uses function_select directly. Every result expression is unchanged.
module kleine_alu (
    input [31:0] input_a,
    input [31:0] input_b,
    input [2:0] function_select,
    input function_modifier,
    output reg [31:0] result
);
localparam ALU_ADD_SUB = 3'b000;
localparam ALU_SLL     = 3'b001;
localparam ALU_SLT     = 3'b010;
localparam ALU_SLTU    = 3'b011;
localparam ALU_XOR     = 3'b100;
localparam ALU_SRL_SRA = 3'b101;
localparam ALU_OR      = 3'b110;
localparam ALU_AND_CLR = 3'b111;
wire [32:0] tmp_shifted = $signed({{function_modifier ? input_a[31] : 1'b0, input_a}}) >>> input_b[4:0];
wire [31:0] result_add_sub = input_a + (function_modifier ? -input_b : input_b);
wire [31:0] result_sll = input_a << input_b[4:0];
wire [31:0] result_slt = {{
    {{31{{1'b0}}}},
    (
        $signed({{function_select[0] ? 1'b0 : input_a[31], input_a}})
        < $signed({{function_select[0] ? 1'b0 : input_b[31], input_b}})
    )
}};
wire [31:0] result_xor = input_a ^ input_b;
wire [31:0] result_srl_sra = tmp_shifted[31:0];
wire [31:0] result_or = input_a | input_b;
wire [31:0] result_and_clr = (function_modifier ? ~input_a : input_a) & input_b;
always @(*) begin
    case (function_select)
        ALU_ADD_SUB: result = result_add_sub;
        ALU_SLL:     result = result_sll;
        ALU_SLT,
        ALU_SLTU:    result = result_slt;
        ALU_XOR:     result = result_xor;
        ALU_SRL_SRA: result = result_srl_sra;
        ALU_OR:      result = result_or;
        ALU_AND_CLR: result = result_and_clr;
    endcase
end
endmodule
"""

COSIM = """module cosim;
  reg clk = 0; reg [31:0] a, b, pa, pb; reg [2:0] f, pf; reg m, pm; reg [31:0] rng = 32'h2468ace1;
  wire [31:0] add_result, seq_result, comb_result, prev_comb;
  integer n, bad = 0, checks = 0;
  alu orig(.clk(clk), .input_a(a), .input_b(b), .function_select(f), .function_modifier(m),
           .add_result(add_result), .result(seq_result));
  kleine_alu prev(.input_a(pa), .input_b(pb), .function_select(pf), .function_modifier(pm), .result(prev_comb));
  task rnd; begin rng = rng ^ (rng << 13); rng = rng ^ (rng >> 17); rng = rng ^ (rng << 5); end endtask
  initial begin
    for (n = 0; n < 200000; n = n + 1) begin
      rnd; a = rng; rnd; b = (n % 3 == 0) ? (rng & 32'h3f) : rng; rnd; {f, m} = rng[3:0];
      #1 clk = 1; #1 clk = 0;
      pa = a; pb = b; pf = f; pm = m; #1;
      checks = checks + 1;
      if (seq_result !== prev_comb) begin bad = bad + 1; if (bad < 5) $display("MISMATCH f=%d m=%b", f, m); end
    end
    $display("S2G_COSIM checks=%0d mismatches=%0d", checks, bad); $finish;
  end
endmodule
"""


def prepare(root, source_root, ir_path, context_path):
    root = Path(root).resolve()
    require(not root.exists(), 'root must be new')
    inputs = root / 'inputs'
    (inputs / 'original').mkdir(parents=True)
    src = Path(source_root) / SOURCE
    shutil.copy2(src, inputs / 'original/alu.v')
    ir = alu.validate_ir(read(ir_path))
    (inputs / 'golden.v').write_text(DERIVED.format(sha=digest(src)))
    (inputs / 'tb.v').write_text(alu.testbench(ir))
    (inputs / 'cosim.v').write_text(COSIM)
    # Icarus requires declaration before use; the original assigns add_result before declaring
    # result_add_sub. A simulation-only copy moves that one assign to the end (same semantics).
    text = (inputs / 'original/alu.v').read_text()
    line = 'assign add_result = result_add_sub;'
    require(text.count(line) == 1, 'unexpected original layout')
    (inputs / 'original_for_sim.v').write_text(text.replace(line, '').replace('endmodule', line + '\nendmodule'))
    sim = root / 'cosim.vvp'
    c = subprocess.run(['iverilog', '-g2012', '-s', 'cosim', '-o', str(sim), str(inputs / 'original_for_sim.v'),
                        str(inputs / 'golden.v'), str(inputs / 'cosim.v')], capture_output=True, text=True)
    require(c.returncode == 0, 'cosim compile failed: ' + c.stderr[:300])
    out = subprocess.run(['vvp', '-n', str(sim)], capture_output=True, text=True).stdout
    (root / 'cosim.log').write_text(out)
    require('S2G_COSIM checks=200000 mismatches=0' in out, 'derived core differs from the registered original')
    (inputs / 'constraints.sdc').write_text(
        (BASE / 'runs/riscv-alu32-screen-v2/inputs/constraints.sdc').read_text())
    ports = {'input_a': ('input', 32), 'input_b': ('input', 32), 'function_select': ('input', 3),
             'function_modifier': ('input', 1), 'result': ('output', 32)}
    spec = dict(schema='s2g.spec.v1', design_family='kleine_riscv_alu32', top='kleine_alu',
                ports={n: dict(direction=d, width=w, signed=False) for n, (d, w) in ports.items()},
                behavior='Combinational core of the kleine-riscv ALU: add/sub, SLL, SLT/SLTU, XOR, SRL/SRA, OR, AND/ANDN selected by function_select and function_modifier.',
                arithmetic='32-bit modular arithmetic; shift amount input_b[4:0]; signed comparisons on 32-bit two-complement operands.',
                clocks=[], resets=[], latency_cycles=0, throughput='combinational', protocol='always active',
                legal_inputs='all 68-bit binary input combinations; X/Z excluded', parameters={}, open_questions=[],
                requirements=[dict(id='ALU', text='Preserve the derived core output for every binary input.')],
                verification=dict(tb_top='tb', pass_marker='S2G_PASS', minimum_checks=67840,
                                  reference_origin='Derived from the local kleine-riscv alu.v copy; derivation co-simulated (200000 vectors) against the registered original',
                                  test_origin='Oracle generated from the frozen s2g.alu-ir.v1 semantic map; 2304 directed plus 65536 seeded cases over all 16 opcode values'))
    write(inputs / 'spec.json', spec)
    write(inputs / 'spec.json.review.json', dict(schema='s2g.review.v1', spec_hash=identity(spec), scope='engineering_screen',
          reviewer='S2G held-out qualification author; not a user production-spec attestation', reviewed_at=stamp()))
    ctx = read(context_path)
    ctx.update(candidate_budget=8, tool_run_budget=16, timeout_s=600, die_area=[0, 0, 90, 90], core_area=[10, 10, 80, 80])
    write(inputs / 'context.json', ctx)
    write(root / 'provenance.json', {'source': str(src), 'source_sha256': digest(src), 'license': 'no license file in the local copy',
                                     'derivation': 'registers and add_result removed; expressions unchanged; co-simulation PASS',
                                     'simulation_shim': 'original_for_sim.v moves `assign add_result` after its declaration (Icarus declaration order); simulation only',
                                     'ir_sha256': digest(ir_path), 'created_at': stamp()})
    return {'root': str(root), 'cosim': out.strip().splitlines()[-1]}


def qualify(root):
    """Golden through the full flow (TB, SAT, synthesis, routed acceptance) in its own workspace."""
    root, inputs = Path(root).resolve(), Path(root).resolve() / 'inputs'
    ws = root / 'workspace'
    if not ws.exists():
        workspace.init(ws, inputs / 'spec.json', inputs / 'golden.v', inputs / 'tb.v', inputs / 'constraints.sdc',
                       inputs / 'context.json', inputs / 'spec.json.review.json')
        workspace.add(ws, inputs / 'golden.v')
    out = {}
    for until in ('synthesis', 'finish'):
        r = pipeline.evaluate(ws, 'c0000', until)
        out[until] = {'run': Path(r['run']).name, 'status': r['status'], 'acceptance': r['acceptance']['status'],
                      'error': r.get('error')}
    write(root / 'qualification.json', out)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--root', required=True)
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('prepare')
    p.add_argument('--source-root', required=True)
    p.add_argument('--ir', default=str(BASE / 'examples/kleine_alu32/alu_ir.json'))
    p.add_argument('--context', default=str(BASE / 'configs/local-nangate45.json'))
    sub.add_parser('qualify')
    a = ap.parse_args()
    print(prepare(a.root, a.source_root, a.ir, a.context) if a.cmd == 'prepare' else qualify(a.root))


if __name__ == '__main__':
    main()
