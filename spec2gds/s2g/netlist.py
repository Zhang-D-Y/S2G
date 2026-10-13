"""Mapped/coarse netlist analysis for synthesis evidence.

Liberty cell facts (area, sequential kind, pin directions), name-insensitive structural
fingerprints, logic depth and best-effort RTL provenance through Yosys src attributes.
"""
import hashlib
import re
from collections import Counter, defaultdict
from pathlib import Path
from .common import *

_CELL = re.compile(r'^\s*cell\s*\(\s*"?([^")\s]+)"?\s*\)')
_PIN = re.compile(r'^\s*pin\s*\(\s*"?([^")\s]+)"?\s*\)')
_DIR = re.compile(r'^\s*direction\s*:\s*"?(\w+)"?')
_AREA = re.compile(r'^\s*area\s*:\s*([0-9.eE+-]+)')
_SEQ = re.compile(r'^\s*(ff|latch|statetable)\s*\(')
CONST = {'0', '1', 'x', 'z'}
COARSE_STATE = ('$dff', '$adff', '$sdff', '$dffe', '$adffe', '$sdffe', '$sdffce', '$dffsr', '$dffsre',
                '$aldff', '$aldffe', '$dlatch', '$adlatch', '$dlatchsr', '$mem', '$mem_v2')
STRUCT = {'$add', '$sub', '$alu', '$macc', '$macc_v2', '$mul', '$neg', '$mux', '$pmux', '$shl', '$shr',
          '$sshl', '$sshr', '$shift', '$shiftx', '$eq', '$ne', '$lt', '$le', '$gt', '$ge', '$lcu', '$div',
          '$mod', '$reduce_and', '$reduce_or', '$reduce_xor', '$logic_not', '$logic_and', '$logic_or'}


def liberty_cells(path):
    """Brace-depth Liberty reader; only the facts needed by the metrics below."""
    cells, cell, pin, depth, cdepth, pdepth = {}, None, None, 0, 0, 0
    for line in Path(path).read_text(errors='replace').splitlines():
        if cell is None and (m := _CELL.match(line)):
            cell = cells.setdefault(m.group(1), {'area': None, 'seq': None, 'pins': {}}); cdepth = depth
        elif cell is not None and pin is None and (m := _PIN.match(line)):
            pin, pdepth = m.group(1), depth
        elif pin is not None and (m := _DIR.match(line)):
            cell['pins'][pin] = m.group(1)
        elif cell is not None and pin is None and (m := _AREA.match(line)):
            cell['area'] = float(m.group(1))
        elif cell is not None and pin is None and (m := _SEQ.match(line)):
            cell['seq'] = cell['seq'] or m.group(1)
        depth += line.count('{') - line.count('}')
        if pin is not None and depth <= pdepth:
            pin = None
        if cell is not None and depth <= cdepth:
            cell = None
    return cells


def area_unit(lib, lef_sizes):
    """Liberty libraries often omit an area unit. Infer um^2 only when Liberty area equals
    LEF width*height (um) for every cell present in both; otherwise the unit is unknown."""
    both = [(c, v['area']) for c, v in lib.items() if v['area'] is not None and lef_sizes.get(c)]
    if not both:
        return {'unit': None, 'basis': 'no cells with both Liberty area and LEF size'}
    bad = [c for c, a in both if abs(a - lef_sizes[c][0] * lef_sizes[c][1]) > 1e-3 * max(a, 1e-9)]
    if bad:
        return {'unit': None, 'basis': f'{len(bad)}/{len(both)} cells disagree with LEF width*height'}
    return {'unit': 'um^2', 'basis': f'Liberty area equals LEF width*height (um) for {len(both)}/{len(both)} cells'}


def _h(*parts):
    return hashlib.sha256('|'.join(map(str, parts)).encode()).hexdigest()[:24]


def module(netjson, top):
    mods = netjson['modules']
    m = mods.get(top) or mods.get('\\' + top)
    require(m is not None, f'top module {top} missing from netlist JSON')
    return m


class Mapped:
    """Bit-level view of a flattened technology-mapped top module."""

    def __init__(self, netjson, top, lib):
        self.lib, self.mod = lib, module(netjson, top)
        self.cells = self.mod.get('cells', {})
        self.driver, self.sinks, self.unknown = {}, defaultdict(list), Counter()
        for pname, p in self.mod.get('ports', {}).items():
            for i, b in enumerate(p['bits']):
                node = f"port:{p['direction']}:{pname}[{i}]"
                if p['direction'] == 'input':
                    self.driver[b] = (node, '')
                else:
                    self.sinks[b].append((node, ''))
        for name, c in self.cells.items():
            pins = self._pins(name)
            if pins is None:
                self.unknown[c['type']] += 1
                pins = {}
            for pin, bits in c['connections'].items():
                for b in bits:
                    if pins.get(pin) == 'output':
                        self.driver[b] = (name, pin)
                    else:
                        self.sinks[b].append((name, pin))
        self.netname = {}
        for n, w in sorted(self.mod.get('netnames', {}).items(), key=lambda kv: kv[1].get('hide_name', 0)):
            for i, b in enumerate(w['bits']):
                if isinstance(b, int) and b not in self.netname:
                    self.netname[b] = (n, i, w.get('hide_name', 0))

    def _pins(self, name):
        c = self.cells[name]
        info = self.lib.get(c['type'])
        return info['pins'] if info else (c.get('port_directions') or None)

    def seq(self, name):
        info = self.lib.get(self.cells[name]['type'])
        return info['seq'] if info else None

    def inputs(self, name):
        pins = self._pins(name) or {}
        return [(p, bits) for p, bits in sorted(self.cells[name]['connections'].items()) if pins.get(p) != 'output']

    def counts(self):
        out = Counter()
        for name, c in self.cells.items():
            out['sequential_cells'] += self.seq(name) == 'ff'
            out['latch_cells'] += self.seq(name) == 'latch'
            for bits in c['connections'].values():
                out['const_pin_bits'] += sum(b in ('0', '1') for b in bits)
                out['undefined_pin_bits'] += sum(b in ('x', 'z') for b in bits)
        undriven = sum(1 for b in self.sinks if isinstance(b, int) and b not in self.driver)
        return {**dict(out), 'undriven_bits': undriven, 'unknown_cell_types': dict(self.unknown)}

    def logic_depth(self):
        memo, stack = {}, set()

        def depth(name):
            if name in memo:
                return memo[name]
            require(name not in stack, 'combinational loop through ' + name)
            stack.add(name); best = 0
            for _, bits in self.inputs(name):
                for b in bits:
                    d = self.driver.get(b)
                    if d and not d[0].startswith('port:') and self.seq(d[0]) is None:
                        best = max(best, depth(d[0]))
            stack.discard(name)
            memo[name] = best + (1 if self.inputs(name) else 0)  # tie cells have no inputs
            return memo[name]
        return max((depth(n) for n in self.cells if self.seq(n) is None), default=0)

    def fingerprint(self, rounds=6):
        """Weisfeiler-Lehman hash; instance/net names ignored, fixed port names kept."""
        label = {c: _h('cell', v['type']) for c, v in self.cells.items()}

        def src(b, lab):
            if b in CONST:
                return 'const:' + b
            d = self.driver.get(b)
            return 'undriven' if d is None else (d[0] if d[0].startswith('port:') else _h(lab[d[0]], d[1]))
        for _ in range(rounds):
            fanout = defaultdict(list)
            for c in self.cells:
                for pin, bits in self.inputs(c):
                    for b in bits:
                        d = self.driver.get(b)
                        if d and not d[0].startswith('port:'):
                            fanout[d[0]].append(_h(d[1], pin, label[c]))
            label = {c: _h(label[c], *[_h(p, i, src(b, label)) for p, bits in self.inputs(c) for i, b in enumerate(bits)],
                           '|fo', *sorted(fanout[c])) for c in self.cells}
        outs = [_h(n, i, src(b, label)) for n, p in sorted(self.mod.get('ports', {}).items())
                if p['direction'] != 'input' for i, b in enumerate(p['bits'])]
        return _h(*sorted(label.values()), '|po', *outs)


def src_list(cell):
    """Yosys src attribute entries, with directories dropped so locations are workspace-independent."""
    return [Path(x).name for x in cell.get('attributes', {}).get('src', '').split('|') if x]


class Coarse:
    """Coarse netlist (after `synth -run begin:fine`) that still carries src attributes."""

    def __init__(self, netjson, top):
        self.mod = module(netjson, top)
        self.cells = self.mod.get('cells', {})
        self.driver = {}
        for name, c in self.cells.items():
            for pin, bits in c['connections'].items():
                if c.get('port_directions', {}).get(pin) == 'output':
                    for b in bits:
                        self.driver[b] = name
        self.bits = {n.lstrip('\\'): w['bits'] for n, w in self.mod.get('netnames', {}).items()}

    def state_cells(self):
        return sum(c['type'] in COARSE_STATE for c in self.cells.values())

    def structure(self):
        out = []
        for name, c in sorted(self.cells.items()):
            if c['type'] not in STRUCT:
                continue
            width = c['parameters'].get('Y_WIDTH') or c['parameters'].get('WIDTH')
            locs = src_list(c)
            out.append({'type': c['type'], 'y_width': int(str(width), 2) if width else None, 'src': locs,
                        'mapping_confidence': 'high' if len(locs) == 1 else ('medium' if locs else 'none')})
        return out

    def cone_sources(self, signal, max_hops=4):
        """src locations of coarse logic driving `signal` (a port or named net), walking back
        through glue cells and stopping at structural cells. Many-to-many by nature."""
        bits = self.bits.get(signal)
        if not bits:
            return {'src': [], 'mapping_status': 'unresolved', 'confidence': 'none',
                    'basis': f'signal {signal} not present in coarse netlist'}
        frontier, seen, locs = {self.driver.get(b) for b in bits} - {None}, set(), []
        for _ in range(max_hops + 1):
            nxt = set()
            for name in sorted(frontier - seen):
                seen.add(name); c = self.cells[name]
                locs += [f"{c['type']}@{s}" for s in src_list(c)]
                if c['type'] not in STRUCT and c['type'] not in COARSE_STATE:
                    for pin, bs in c['connections'].items():
                        if c.get('port_directions', {}).get(pin) == 'input':
                            nxt |= {self.driver.get(b) for b in bs} - {None}
            frontier = nxt
            if not frontier:
                break
        locs = sorted(set(locs))
        lines = {re.sub(r'\.\d+-.*$', '', l.split('@', 1)[1]) for l in locs}
        conf = 'high' if len(lines) == 1 else ('medium' if 1 < len(lines) <= 3 else ('low' if lines else 'none'))
        return {'src': locs, 'mapping_status': 'partial' if locs else 'unresolved', 'confidence': conf,
                'basis': 'endpoint signal -> coarse driver cone (<= %d glue hops) -> Yosys src attributes' % max_hops}
