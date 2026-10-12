"""Minimal Liberty reader: per-cell area, sequential kind and pin directions.

Only what the netlist metrics need (logic depth, fingerprint, register/latch
counts). Parsed once per library hash and cached next to the run outputs.
"""
import re
from pathlib import Path

from .util import read_json, sha256_file, write_json

_CELL = re.compile(r'^\s*cell\s*\(\s*"?([^")\s]+)"?\s*\)')
_PIN = re.compile(r'^\s*pin\s*\(\s*"?([^")\s]+)"?\s*\)')
_DIR = re.compile(r'^\s*direction\s*:\s*"?(\w+)"?')
_AREA = re.compile(r'^\s*area\s*:\s*([0-9.eE+-]+)')
_SEQ = re.compile(r'^\s*(ff|latch|statetable)\s*\(')


def parse_liberty(path):
    cells, cell, pin = {}, None, None
    depth = cell_depth = pin_depth = 0
    for line in Path(path).read_text(errors="replace").splitlines():
        if cell is None and (m := _CELL.match(line)):
            cell = cells.setdefault(m.group(1), {"area": None, "seq": None, "pins": {}})
            cell_depth = depth
        elif cell is not None and pin is None and (m := _PIN.match(line)):
            pin, pin_depth = m.group(1), depth
        elif pin is not None and (m := _DIR.match(line)):
            cell["pins"][pin] = m.group(1)
        elif cell is not None and pin is None and (m := _AREA.match(line)):
            cell["area"] = float(m.group(1))
        elif cell is not None and pin is None and (m := _SEQ.match(line)):
            cell["seq"] = cell["seq"] or m.group(1)
        depth += line.count("{") - line.count("}")
        if pin is not None and depth <= pin_depth:
            pin = None
        if cell is not None and depth <= cell_depth:
            cell = None
    return cells


def load_liberty(path, cache_dir):
    digest = sha256_file(path)
    cache = Path(cache_dir) / f"liberty_{digest[:16]}.json"
    if cache.exists():
        return read_json(cache)
    cells = parse_liberty(path)
    write_json(cache, cells)
    return cells
