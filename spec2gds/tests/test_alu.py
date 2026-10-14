"""ALU operations library and non-LLM baselines."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from s2g import alu, baselines
from s2g.common import GateError, read
from s2g.verify import rtl_violation

BASE = Path(__file__).resolve().parents[1]
VOCAB = alu.load_vocabulary(BASE / 'configs/alu-actions-v1.json')
V2 = read(BASE / 'configs/local-nangate45.json')['rtl_policy']
W = 4
# Every vocabulary op, two-port opcode (3+1 bits), default PASS_A on the unused codes.
TINY = {'schema': alu.IR_SCHEMA, 'top': 'tiny', 'width': W, 'a': 'a', 'b': 'b', 'y': 'y',
        'op_ports': [{'name': 'f', 'width': 3}, {'name': 'm', 'width': 1}],
        'shift_amount': {'operand': 'b', 'bits': 2}, 'default': 'PASS_A',
        'ops': [{'op': op, 'codes': [i]} for i, op in enumerate(o for o in alu.OPS if o != 'PASS_A')]}


def oracle(op, a, b):
    mask, sh = (1 << W) - 1, b & (W - 1)
    sa, sb = a - (1 << W) if a >> (W - 1) else a, b - (1 << W) if b >> (W - 1) else b
    return {'ADD': a + b, 'SUB': a - b, 'AND': a & b, 'OR': a | b, 'XOR': a ^ b, 'NOR': ~(a | b),
            'ANDN': ~a & b, 'SLL': a << sh, 'SRL': a >> sh, 'SRA': sa >> sh, 'SLT': int(sa < sb),
            'SLTU': int(a < b), 'PASS_A': a, 'PASS_B': b}[op] & mask


class Library(unittest.TestCase):
    def test_space_and_actions(self):
        ir = read(BASE / 'examples/riscv_alu32/alu_ir.json')
        space = alu.space(ir, VOCAB)
        self.assertEqual(len(space), 12 * 4 * 3 * 3)
        self.assertEqual(len({alu.config_id(c) for c in space}), len(space))
        origin = ir['origin_config']
        self.assertIn(origin, space)
        new = alu.apply(ir, VOCAB, origin, {'op': 'set', 'dimension': 'addsub_share', 'value': ['SLT', 'ADD', 'SUB']})
        self.assertEqual(new['addsub_share'], ['ADD', 'SUB', 'SLT'])  # canonical order
        for bad in ({'op': 'set', 'dimension': 'shifter', 'value': 'barrel'},
                    {'op': 'set', 'dimension': 'addsub_share', 'value': ['ADD']},
                    {'op': 'set', 'dimension': 'nope', 'value': 1},
                    {'op': 'set', 'dimension': 'select', 'value': origin['select']},
                    {'op': 'replace', 'text': 'module evil'}):
            with self.assertRaises(GateError):
                alu.apply(ir, VOCAB, origin, bad)

    def test_render_is_deterministic_and_inside_the_v2_contract(self):
        ir = read(BASE / 'examples/riscv_alu32/alu_ir.json')
        for cfg in alu.space(ir, VOCAB)[::17]:
            text = alu.render(ir, cfg)
            self.assertEqual(text, alu.render(ir, cfg))
            self.assertIsNone(rtl_violation(text, ir['top'], V2, ['tb']))

    def test_invalid_ir_is_refused(self):
        for patch in ({'width': 24}, {'default': 'MUL'}, {'top': 'bad name'},
                      {'ops': [{'op': 'ADD', 'codes': [1]}, {'op': 'SUB', 'codes': [1]}]},
                      {'shift_amount': {'operand': 'b', 'bits': 3}}):
            with self.assertRaises(GateError):
                alu.validate_ir(dict(TINY, **patch))

    @unittest.skipUnless(shutil.which('iverilog') and shutil.which('vvp'), 'Icarus Verilog not installed')
    def test_every_configuration_matches_the_oracle_exhaustively(self):
        """All 432 configurations of a 4-bit ALU with every vocabulary op, all inputs and opcodes."""
        codes = {c: row['op'] for row in TINY['ops'] for c in row['codes']}
        lines = ['module tb; reg [2:0] f; reg m; reg [3:0] a, b; wire [3:0] y; integer i, j, k, bad;',
                 '  tiny dut(.f(f), .m(m), .a(a), .b(b), .y(y));',
                 '  reg [3:0] exp [0:4095];', '  initial begin', '    bad = 0;']
        for k in range(16):
            for i in range(16):
                for j in range(16):
                    lines.append(f"    exp[{k * 256 + i * 16 + j}] = 4'd{oracle(codes.get(k, 'PASS_A'), i, j)};")
        lines += ['    for (k=0;k<16;k=k+1) for (i=0;i<16;i=i+1) for (j=0;j<16;j=j+1) begin',
                  '      {f, m} = k; a = i; b = j; #1;',
                  '      if (y !== exp[k*256+i*16+j]) bad = bad + 1;', '    end',
                  '    $display("BAD %0d", bad); $finish;', '  end', 'endmodule']
        with tempfile.TemporaryDirectory() as d:
            tb = Path(d) / 'tb.v'; tb.write_text('\n'.join(lines) + '\n')
            failures = []
            for cfg in alu.space(TINY, VOCAB):
                dut = Path(d) / 'dut.v'; dut.write_text(alu.render(TINY, cfg))
                c = subprocess.run(['iverilog', '-g2012', '-o', str(Path(d) / 'x'), str(dut), str(tb)],
                                   capture_output=True, text=True)
                out = subprocess.run(['vvp', '-n', str(Path(d) / 'x')], capture_output=True, text=True).stdout \
                    if c.returncode == 0 else 'COMPILE ' + c.stderr[:200]
                if 'BAD 0' not in out:
                    failures.append((alu.config_id(cfg), out[:200]))
            self.assertEqual(failures, [])


def row(area, ws=1.0, fp=None, ok=True):
    return {'status': 'COMPLETE' if ok else 'FAILED', 'functional': 'PASS' if ok else 'FAIL', 'mapped_proof': 'PASS',
            'area_um2': area, 'setup_ws_ns': ws, 'fingerprint': fp or f'fp{area}', 'logic_depth': 1}


class Baselines(unittest.TestCase):
    # 2x2 toy space a.b ; golden sits at x.x (area 100)
    TABLE = {'x.x': row(100), 'x.y': row(99.8), 'y.x': row(90), 'y.y': row(70, ws=-0.1)}
    RULE = {'min_rel_area_improvement': 0.005, 'setup_ws_min_ns': 0.0}

    def test_rule_and_reachability(self):
        g = row(100, fp='golden')
        self.assertFalse(baselines.promotable(g, self.TABLE['x.y'], self.RULE))   # below threshold
        self.assertFalse(baselines.promotable(g, self.TABLE['y.y'], self.RULE))   # negative slack
        self.assertTrue(baselines.promotable(g, self.TABLE['y.x'], self.RULE))
        nb = baselines.neighbours(list(self.TABLE))
        self.assertEqual(sorted(nb['x.x']), ['x.y', 'y.x'])
        r = baselines.reachability(self.TABLE, nb, g, 'x.x', self.RULE)
        self.assertEqual((r['best'], r['best_reachable'], r['min_steps_to_best']), ('y.x', True, 1))

    def test_simulation_is_seeded_and_spends_every_proposal(self):
        g = row(100, fp='golden')
        nb = baselines.neighbours(list(self.TABLE))
        a = baselines.simulate(self.TABLE, nb, g, 'x.x', self.RULE, 1, 200, 7)
        b = baselines.simulate(self.TABLE, nb, g, 'x.x', self.RULE, 1, 200, 7)
        self.assertEqual(a, b)
        walk = a['random_single_action_walk']
        self.assertGreater(walk['p_best'], 0.3); self.assertLess(walk['p_best'], 0.7)  # 1 of 2 neighbours
        self.assertEqual(a['greedy_coordinate_sweep']['evaluations_used'], 1)
        self.assertNotIn('y.y', [p['config'] for p in baselines.pareto(dict(self.TABLE, **{'y.y': row(70, ws=-0.1, ok=False)}))])


if __name__ == '__main__':
    unittest.main()
