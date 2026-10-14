"""Typed structural operations for single-output combinational integer ALUs.

A design is described by a semantic map (`s2g.alu-ir.v1`): ports, width, opcode -> operation and
the default for unlisted opcodes. A *configuration* picks one value per frozen dimension of the
action vocabulary (`configs/alu-actions-v1.json`); an *action* sets one dimension. `render` turns
(IR, configuration) into Verilog deterministically. Rendering proves nothing: every rendered
candidate still passes the frozen TB, RTL SAT and mapped SAT gates like any other candidate.
"""
import itertools
import re
from pathlib import Path
from .common import *

IR_SCHEMA = 's2g.alu-ir.v1'
OPS = ('ADD', 'SUB', 'AND', 'OR', 'XOR', 'NOR', 'ANDN', 'SLL', 'SRL', 'SRA', 'SLT', 'SLTU', 'PASS_A', 'PASS_B')
ADDER_OPS = ('ADD', 'SUB', 'SLTU', 'SLT')
SHIFT_OPS = ('SLL', 'SRL', 'SRA')
CLASSES = {'arith': ('ADD', 'SUB'), 'logic': ('AND', 'OR', 'XOR', 'NOR', 'ANDN', 'PASS_A', 'PASS_B'),
           'shift': SHIFT_OPS, 'compare': ('SLT', 'SLTU')}
IDENT = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
ZERO = "1'b0"


def load_vocabulary(path):
    v = read(path)
    require(v.get('schema') == 's2g.alu-actions.v1', 'unknown ALU action vocabulary')
    return v


def validate_ir(ir):
    require(ir.get('schema') == IR_SCHEMA, 'ALU IR schema must be ' + IR_SCHEMA)
    w = ir['width']
    require(isinstance(w, int) and w >= 2 and w & (w - 1) == 0, 'width must be a power of two')
    for p in ir['op_ports'] + [ir['a'], ir['b'], ir['y'], ir['top']]:
        name = p['name'] if isinstance(p, dict) else p
        require(IDENT.fullmatch(name), 'unsafe identifier ' + str(name))
    opw = sum(p['width'] for p in ir['op_ports'])
    seen = set()
    for row in ir['ops']:
        require(row['op'] in OPS, 'unknown op ' + row['op'])
        for c in row['codes']:
            require(0 <= c < 2 ** opw and c not in seen, f'bad or duplicate opcode {c}')
            seen.add(c)
    require(ir['default'] in OPS, 'default must be an op')
    require(ir['shift_amount']['operand'] in ('a', 'b') and 2 ** ir['shift_amount']['bits'] == w,
            'shift amount must be log2(width) bits of operand a or b')
    return ir


def present(ir):
    return {row['op'] for row in ir['ops']} | {ir['default']}


def space(ir, vocab):
    """Every valid configuration for this design, in a deterministic order."""
    ops = present(ir)
    adders = [o for o in ADDER_OPS if o in ops]
    shares = [[]] + [list(c) for n in range(2, len(adders) + 1) for c in itertools.combinations(adders, n)]
    d = vocab['dimensions']
    shifters = d['shifter']['values'] if any(o in ops for o in SHIFT_OPS) else ['separate']
    styles = d['shift_style']['values'] if any(o in ops for o in SHIFT_OPS) else ['native']
    return [{'addsub_share': s, 'shifter': sh, 'shift_style': st, 'select': se}
            for s in shares for sh in shifters for st in styles for se in d['select']['values']]


def config_id(cfg):
    return 'share-' + ('+'.join(cfg['addsub_share']) or 'none') + f".{cfg['shifter']}.{cfg['shift_style']}.{cfg['select']}"


def apply(ir, vocab, cfg, action):
    """One typed action: set one dimension to a value that is valid for this design."""
    require(isinstance(action, dict) and set(action) == {'op', 'dimension', 'value'}
            and action.get('op') == 'set', 'action must contain exactly op=set, dimension, value')
    dim, val = action.get('dimension'), action.get('value')
    require(isinstance(dim, str) and dim in vocab['dimensions'], f'unknown dimension {dim}')
    if dim == 'addsub_share':
        require(isinstance(val, list), 'addsub_share takes a list')
        require(all(isinstance(o, str) and o in ADDER_OPS and o in present(ir) for o in val)
                and len(set(val)) == len(val), 'unknown, absent or repeated adder-sharing member')
        val = [o for o in ADDER_OPS if o in val]
    new = dict(cfg, **{dim: val})
    require(new in space(ir, vocab), f'{dim}={val} is not a valid configuration for this design')
    require(new != cfg, 'action does not change the configuration')
    return new


# ------------------------------------------------------------------ rendering

def _rev(x, w):
    return '{' + ', '.join(f'{x}[{i}]' for i in range(w)) + '}'


def _stages(lines, name, x, amt, bits, w, left, fill, msb_first):
    """Explicit log-stage shifter; `fill` is a 1-bit expression shifted in (right shifts)."""
    order = range(bits - 1, -1, -1) if msb_first else range(bits)
    cur = x
    for n, k in enumerate(order):
        s = 1 << k
        nxt = f'{name}_s{n}'
        moved = f'{{{cur}[{w - 1 - s}:0], {s}\'b0}}' if left else f'{{{{{s}{{{fill}}}}}, {cur}[{w - 1}:{s}]}}'
        lines.append(f'  wire [{w - 1}:0] {nxt} = {amt}[{k}] ? {moved} : {cur};')
        cur = nxt
    return cur


def render(ir, cfg, preamble=''):
    validate_ir(ir)
    w, top = ir['width'], ir['top']
    a, b, y = ir['a'], ir['b'], ir['y']
    bits = ir['shift_amount']['bits']
    ops = present(ir)
    opw = sum(p['width'] for p in ir['op_ports'])
    L = []
    ports = [f"  input [{p['width'] - 1}:0] {p['name']}" for p in ir['op_ports']]
    ports += [f'  input [{w - 1}:0] {a}', f'  input [{w - 1}:0] {b}', f'  output reg [{w - 1}:0] {y}']
    L.append(f'// Rendered by s2g.alu ({config_id(cfg)}) from the {IR_SCHEMA} semantic map of `{top}`.')
    L.append(f'module {top}(\n' + ',\n'.join(ports) + '\n);')
    opc = '{' + ', '.join(p['name'] for p in ir['op_ports']) + '}' if len(ir['op_ports']) > 1 else ir['op_ports'][0]['name']
    L.append(f'  wire [{opw - 1}:0] opc = {opc};')
    codes = {}
    for row in ir['ops']:
        codes.setdefault(row['op'], []).extend(row['codes'])
    for op in OPS:
        if op in codes:
            L.append(f'  wire is_{op} = ' + ' || '.join(f"(opc == {opw}'d{c})" for c in sorted(codes[op])) + ';')
    amt = f"{a if ir['shift_amount']['operand'] == 'a' else b}[{bits - 1}:0]"
    L.append(f'  wire [{bits - 1}:0] sh = {amt};')
    res = {}
    # adder sharing
    share = [o for o in cfg['addsub_share'] if o in ops]
    if share:
        subs = [o for o in share if o != 'ADD']
        sub_en = "1'b1" if 'ADD' not in share else ('(' + ' || '.join(f'is_{o}' for o in subs) + ')' if subs else ZERO)
        L.append(f'  wire sub_en = {sub_en};')
        L.append(f'  wire [{w}:0] sum = {{1\'b0, {a}}} + {{1\'b0, {b} ^ {{{w}{{sub_en}}}}}} + sub_en;')
        if 'ADD' in share:
            res['ADD'] = f'sum[{w - 1}:0]'
        if 'SUB' in share:
            res['SUB'] = f'sum[{w - 1}:0]'
        if 'SLTU' in share:
            L.append(f'  wire lt_u = ~sum[{w}];')
        if 'SLT' in share:
            L.append(f'  wire lt_s = ({a}[{w - 1}] != {b}[{w - 1}]) ? {a}[{w - 1}] : sum[{w - 1}];')
    if 'ADD' in ops and 'ADD' not in share:
        L.append(f'  wire [{w - 1}:0] r_add = {a} + {b};'); res['ADD'] = 'r_add'
    if 'SUB' in ops and 'SUB' not in share:
        L.append(f'  wire [{w - 1}:0] r_sub = {a} - {b};'); res['SUB'] = 'r_sub'
    if 'SLTU' in ops and 'SLTU' not in share:
        L.append(f'  wire lt_u = {a} < {b};')
    if 'SLT' in ops and 'SLT' not in share:
        L.append(f'  wire lt_s = $signed({a}) < $signed({b});')
    if 'SLTU' in ops:
        res['SLTU'] = f"{{{w - 1}'b0, lt_u}}"
    if 'SLT' in ops:
        res['SLT'] = f"{{{w - 1}'b0, lt_s}}"
    for op, expr in (('AND', f'{a} & {b}'), ('OR', f'{a} | {b}'), ('XOR', f'{a} ^ {b}'), ('NOR', f'~({a} | {b})'),
                     ('ANDN', f'~{a} & {b}'), ('PASS_A', a), ('PASS_B', b)):
        if op in ops:
            res[op] = f'({expr})'
    # shifters
    style = cfg['shift_style']
    staged, msb = style != 'native', style == 'stages_msb_first'

    def right(name, x, fill):
        if staged:
            return _stages(L, name, x, 'sh', bits, w, False, fill, msb)
        L.append(f'  wire [{w}:0] {name}_x = $signed({{{fill}, {x}}}) >>> sh;')
        return f'{name}_x[{w - 1}:0]'

    def left(name, x):
        if staged:
            return _stages(L, name, x, 'sh', bits, w, True, None, msb)
        L.append(f'  wire [{w - 1}:0] {name}_x = {x} << sh;')
        return f'{name}_x'
    sops = [o for o in SHIFT_OPS if o in ops]
    org = cfg['shifter'] if sops else 'separate'
    if org == 'separate':
        if 'SLL' in sops:
            res['SLL'] = left('sll', a)
        if 'SRL' in sops:
            res['SRL'] = right('srl', a, ZERO)
        if 'SRA' in sops:
            res['SRA'] = right('sra', a, f'{a}[{w - 1}]')
    elif org == 'right_merged':
        if 'SLL' in sops:
            res['SLL'] = left('sll', a)
        fill = f'is_SRA & {a}[{w - 1}]' if 'SRA' in sops else ZERO
        L.append(f'  wire fill = {fill};')
        r = right('shr', a, 'fill')
        for o in ('SRL', 'SRA'):
            if o in sops:
                res[o] = r
    elif org == 'reverse_right':
        fill = f'is_SRA & {a}[{w - 1}]' if 'SRA' in sops else ZERO
        L.append(f'  wire fill = {fill};')
        L.append(f"  wire to_left = {'is_SLL' if 'SLL' in sops else ZERO};")
        L.append(f'  wire [{w - 1}:0] shin = to_left ? {_rev(a, w)} : {a};')
        r = right('shr', 'shin', 'fill')
        L.append(f'  wire [{w - 1}:0] shres = {r};')
        L.append(f'  wire [{w - 1}:0] shout = to_left ? {_rev("shres", w)} : shres;')
        for o in sops:
            res[o] = 'shout'
    else:  # reverse_left
        fill = f'is_SRA & {a}[{w - 1}]' if 'SRA' in sops else ZERO
        L.append(f'  wire fill = {fill};')
        rights = [f'is_{o}' for o in ('SRL', 'SRA') if o in sops]
        L.append(f"  wire to_right = {' || '.join(rights) if rights else ZERO};")
        L.append(f'  wire [{w - 1}:0] shin = to_right ? {_rev(a, w)} : {a};')
        r = left('shl', 'shin')
        # bits vacated by the left shift receive the fill (sign) bit for arithmetic right shifts
        L.append(f'  wire [{w - 1}:0] shmask = ~({{{w}{{1\'b1}}}} << sh);')
        L.append(f'  wire [{w - 1}:0] shres = {r} | ({{{w}{{fill}}}} & shmask);')
        L.append(f'  wire [{w - 1}:0] shout = to_right ? {_rev("shres", w)} : shres;')
        for o in sops:
            res[o] = 'shout'
    dflt = res.get(ir['default']) or {'PASS_A': a, 'PASS_B': b}.get(ir['default'])
    require(dflt is not None, 'default op must be a pass-through or an implemented op')
    # output select
    groups = {}
    for row in ir['ops']:
        groups.setdefault(res[row['op']], []).extend(row['codes'])
    sel = cfg['select']
    L.append('  always @* begin')
    if sel == 'case':
        L.append('    case (opc)')
        for expr, cs in groups.items():
            L.append('      ' + ', '.join(f"{opw}'d{c}" for c in sorted(cs)) + f': {y} = {expr};')
        L.append(f'      default: {y} = {dflt};')
        L.append('    endcase')
    elif sel == 'and_or':
        hit = ' || '.join(f'is_{op}' for op in OPS if op in codes)
        terms = [f'({{{w}{{(' + ' || '.join(f'is_{op}' for op in codes if res[op] == expr) + f')}}}} & {expr})'
                 for expr in groups]
        terms.append(f'({{{w}{{~({hit})}}}} & {dflt})')
        L.append(f'    {y} = ' + '\n      | '.join(terms) + ';')
    else:  # grouped
        first = True
        for cname, members in CLASSES.items():
            mine = [op for op in members if op in codes]
            if not mine:
                continue
            cond = ' || '.join(f'is_{op}' for op in mine)
            inner = {}
            for op in mine:
                inner.setdefault(res[op], []).append(f'is_{op}')
            chain = None
            for expr, conds in reversed(list(inner.items())):
                chain = expr if chain is None else f"({' || '.join(conds)}) ? {expr} : {chain}"
            L.append(f"    {'if' if first else 'else if'} ({cond}) {y} = {chain};")
            first = False
        L.append(f'    else {y} = {dflt};')
    L.append('  end')
    L.append('endmodule')
    return preamble + '\n'.join(L) + '\n'


def preamble_of(path):
    """Leading comment block of the original RTL (license notices travel with derivatives)."""
    out = []
    for line in Path(path).read_text().splitlines():
        if line.startswith('//') or not line.strip():
            out.append(line)
        else:
            break
    return '\n'.join(out).rstrip() + '\n\n' if out else ''


def testbench(ir, randoms=4096):
    """Self-checking TB whose behavioral oracle is generated from the semantic map. A golden RTL is
    qualified only if it passes this TB; every candidate is SAT-checked against that golden, so a
    wrong map cannot certify a wrong candidate. Covers every opcode value: 12x12 corners + randoms."""
    validate_ir(ir)
    w, bits = ir['width'], ir['shift_amount']['bits']
    opw = sum(p['width'] for p in ir['op_ports'])
    sh = f"{ir['shift_amount']['operand']}[{bits - 1}:0]"
    sem = {'ADD': 'a + b', 'SUB': 'a - b', 'AND': 'a & b', 'OR': 'a | b', 'XOR': 'a ^ b', 'NOR': '~(a | b)',
           'ANDN': '~a & b', 'SLL': f'a << {sh}', 'SRL': f'a >> {sh}', 'SRA': f'$signed(a) >>> {sh}',
           'SLT': f"{{{w - 1}'b0, $signed(a) < $signed(b)}}", 'SLTU': f"{{{w - 1}'b0, a < b}}", 'PASS_A': 'a', 'PASS_B': 'b'}
    cases = '\n'.join(f"        {opw}'d{c}: oracle = {sem[row['op']]};" for row in ir['ops'] for c in row['codes'])
    conns = ', '.join([f".{p['name']}(opc[{sum(q['width'] for q in ir['op_ports'][i + 1:]) + p['width'] - 1}:"
                       f"{sum(q['width'] for q in ir['op_ports'][i + 1:])}])" for i, p in enumerate(ir['op_ports'])]
                      + [f".{ir['a']}(a)", f".{ir['b']}(b)", f".{ir['y']}(actual)"])
    corners = [0, 1, (1 << w) - 1, 1 << (w - 1), (1 << (w - 1)) - 1, int('10' * (w // 2), 2), int('01' * (w // 2), 2),
               w - 1, w, 2 * w - 1, ((1 << (w // 2)) - 1) << (w // 2), (1 << (w // 2)) - 1]
    corner = '\n'.join(f"        {i}: corner = {w}'h{v:x};" for i, v in enumerate(corners))
    total = (2 ** opw) * (len(corners) ** 2 + randoms)
    return f"""module tb;
  reg [{opw - 1}:0] opc;
  reg [{w - 1}:0] a, b, expected, rng;
  wire [{w - 1}:0] actual;
  integer checks = 0, failures = 0, oi, i, j;
  {ir['top']} dut({conns});
  function [{w - 1}:0] oracle;
    input [{opw - 1}:0] f;
    input [{w - 1}:0] a, b;
    begin
      case (f)
{cases}
        default: oracle = {sem[ir['default']]};
      endcase
    end
  endfunction
  function [{w - 1}:0] corner;
    input integer n;
    begin
      case (n)
{corner}
        default: corner = 0;
      endcase
    end
  endfunction
  task next_random;
    begin
      rng = rng ^ (rng << 13); rng = rng ^ (rng >> 17); rng = rng ^ (rng << 5);
    end
  endtask
  task check;
    begin
      #1;
      expected = oracle(opc, a, b);
      checks = checks + 1;
      if (actual !== expected) begin
        failures = failures + 1;
        $display("S2G_FAIL op=%h a=%h b=%h actual=%h expected=%h", opc, a, b, actual, expected);
        $fatal(1, "semantic-map oracle mismatch");
      end
    end
  endtask
  initial begin
    rng = {w}'h13579bdf;
    for (oi = 0; oi < {2 ** opw}; oi = oi + 1) begin
      opc = oi;
      for (i = 0; i < {len(corners)}; i = i + 1)
        for (j = 0; j < {len(corners)}; j = j + 1) begin
          a = corner(i); b = corner(j); check;
        end
      for (i = 0; i < {randoms}; i = i + 1) begin
        next_random; a = rng; next_random; b = rng; check;
      end
    end
    $display("S2G_PASS checks=%0d failures=%0d", checks, failures);
    $finish;
  end
endmodule
// expected checks: {total}
"""
