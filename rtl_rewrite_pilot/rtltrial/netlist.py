"""Mapped-netlist analysis: logic depth, normalized fingerprint, register/latch/constant
counts, and source localization of critical-path endpoints through the coarse netlist."""
import hashlib
import re
from collections import Counter, defaultdict

CONST_BITS = {"0", "1", "x", "z"}
COARSE_FF = {"$dff", "$adff", "$sdff", "$sdffe", "$sdffce", "$dffe", "$adffe", "$dffsr",
             "$dffsre", "$aldff", "$aldffe"}
COARSE_LATCH = {"$dlatch", "$adlatch", "$dlatchsr"}
# Coarse cells worth naming in feedback (arithmetic / selection / comparison structure).
COARSE_STRUCT = {"$add", "$sub", "$alu", "$macc", "$macc_v2", "$mul", "$neg", "$mux", "$pmux",
                 "$shl", "$shr", "$sshl", "$sshr", "$shift", "$shiftx", "$eq", "$ne", "$lt",
                 "$le", "$gt", "$ge", "$lcu", "$div", "$mod"}


def _top(netjson, top):
    mods = netjson["modules"]
    return mods.get(top) or mods.get(f"\\{top}")


def _h(*parts):
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:24]


class Mapped:
    """Bit-level view of the flattened, technology-mapped top module."""

    def __init__(self, netjson, top, lib):
        mod = _top(netjson, top)
        if mod is None:
            raise ValueError(f"top module {top} not in netlist JSON")
        self.lib, self.mod = lib, mod
        self.cells = mod.get("cells", {})
        self.driver = {}               # bit -> (node, pin)
        self.sinks = defaultdict(list)  # bit -> [(node, pin)]
        self.unknown_cells = Counter()
        for pname, p in mod.get("ports", {}).items():
            for i, b in enumerate(p["bits"]):
                node = f"port:{p['direction']}:{pname}[{i}]"
                if p["direction"] == "input":
                    self.driver[b] = (node, "")
                else:
                    self.sinks[b].append((node, ""))
        for cname, c in self.cells.items():
            info = lib.get(c["type"])
            if info is None:
                self.unknown_cells[c["type"]] += 1
            pins = info["pins"] if info else c.get("port_directions", {})
            for pin, bits in c["connections"].items():
                d = pins.get(pin, "input")
                for b in bits:
                    if d == "output":
                        self.driver[b] = (cname, pin)
                    else:
                        self.sinks[b].append((cname, pin))
        self.netname = {}
        for n, w in sorted(mod.get("netnames", {}).items(), key=lambda kv: kv[1].get("hide_name", 0)):
            for i, b in enumerate(w["bits"]):
                if isinstance(b, int) and b not in self.netname:
                    self.netname[b] = (n, i, w.get("hide_name", 0))

    def seq_kind(self, cname):
        info = self.lib.get(self.cells[cname]["type"])
        return info["seq"] if info else None

    def in_pins(self, cname):
        c = self.cells[cname]
        info = self.lib.get(c["type"])
        pins = info["pins"] if info else c.get("port_directions", {})
        return [(p, bits) for p, bits in sorted(c["connections"].items()) if pins.get(p, "input") != "output"]

    def counts(self):
        ff = latch = const_bits = undef_bits = 0
        for cname, c in self.cells.items():
            k = self.seq_kind(cname)
            ff += k == "ff"
            latch += k == "latch"
            for bits in c["connections"].values():
                const_bits += sum(1 for b in bits if b in ("0", "1"))
                undef_bits += sum(1 for b in bits if b in ("x", "z"))
        tie_cells = sum(1 for c in self.cells.values() if c["type"].endswith("__conb_1"))
        return {"mapped_ff_cells": ff, "mapped_latch_cells": latch, "const_pin_bits": const_bits,
                "undef_pin_bits": undef_bits, "tie_cells": tie_cells}

    def logic_depth(self):
        """Max number of combinational cells on a path from a timing startpoint (input port,
        sequential output, tie cell) to an endpoint (output port, sequential input)."""
        memo, onstack = {}, set()

        def depth(cname):
            if cname in memo:
                return memo[cname]
            if cname in onstack:
                raise ValueError(f"combinational loop through {cname}")
            onstack.add(cname)
            best = 0
            for _, bits in self.in_pins(cname):
                for b in bits:
                    drv = self.driver.get(b)
                    if drv and not drv[0].startswith("port:") and self.seq_kind(drv[0]) is None:
                        best = max(best, depth(drv[0]))
            onstack.discard(cname)
            is_tie = self.cells[cname]["type"].endswith("__conb_1")
            memo[cname] = best + (0 if is_tie else 1)
            return memo[cname]

        worst = 0
        for cname in self.cells:
            if self.seq_kind(cname) is None:
                worst = max(worst, depth(cname))
        return worst

    def fingerprint(self, rounds=6):
        """Weisfeiler-Lehman hash over the cell graph with instance and net names ignored.
        Port names are kept because the interface is fixed across candidates."""
        label = {c: _h("cell", v["type"]) for c, v in self.cells.items()}

        def src_label(b, lab):
            if b in CONST_BITS:
                return f"const:{b}"
            drv = self.driver.get(b)
            if drv is None:
                return "undriven"
            node, pin = drv
            return node if node.startswith("port:") else _h(lab[node], pin)

        for _ in range(rounds):
            fanout = defaultdict(list)
            for c in self.cells:
                for pin, bits in self.in_pins(c):
                    for i, b in enumerate(bits):
                        drv = self.driver.get(b)
                        if drv and not drv[0].startswith("port:"):
                            fanout[drv[0]].append(_h(drv[1], pin, label[c]))
            new = {}
            for c in self.cells:
                ins = [_h(pin, i, src_label(b, label)) for pin, bits in self.in_pins(c)
                       for i, b in enumerate(bits)]
                new[c] = _h(label[c], *ins, "|fo", *sorted(fanout[c]))
            label = new
        outs = []
        for pname, p in sorted(self.mod.get("ports", {}).items()):
            if p["direction"] != "input":
                outs.extend(_h(pname, i, src_label(b, label)) for i, b in enumerate(p["bits"]))
        return _h(*sorted(label.values()), "|po", *outs)

    def instance_signal(self, cname):
        """Public signal name on a sequential cell's output, used to name STA endpoints."""
        for pin, bits in self.cells[cname]["connections"].items():
            info = self.lib.get(self.cells[cname]["type"])
            if info and info["pins"].get(pin) == "output":
                for b in bits:
                    nm = self.netname.get(b)
                    if nm and not nm[2]:
                        return nm[0], nm[1]
        return None


class Coarse:
    """Coarse-grained netlist (after `synth -run begin:fine`) carrying Yosys src attributes."""

    def __init__(self, netjson, top):
        self.mod = _top(netjson, top)
        self.cells = self.mod.get("cells", {})
        self.driver = {}
        for cname, c in self.cells.items():
            dirs = c.get("port_directions", {})
            for pin, bits in c["connections"].items():
                if dirs.get(pin) == "output":
                    for b in bits:
                        self.driver[b] = cname
        self.bits_of = {n: w["bits"] for n, w in self.mod.get("netnames", {}).items()}

    def reg_bits(self):
        ff = latch = 0
        for c in self.cells.values():
            w = int(str(c["parameters"].get("WIDTH", "1")), 2) if c["type"] in COARSE_FF | COARSE_LATCH else 0
            ff += w if c["type"] in COARSE_FF else 0
            latch += w if c["type"] in COARSE_LATCH else 0
        return ff, latch

    def type_counts(self):
        return dict(sorted(Counter(c["type"] for c in self.cells.values()).items()))

    def structure(self):
        out = []
        for cname, c in sorted(self.cells.items()):
            if c["type"] not in COARSE_STRUCT:
                continue
            width = c["parameters"].get("Y_WIDTH") or c["parameters"].get("WIDTH")
            src = c.get("attributes", {}).get("src", "")
            locs = [s for s in src.split("|") if s]
            out.append({"type": c["type"], "y_width": int(str(width), 2) if width else None,
                        "src": locs, "confidence": "high" if len(locs) == 1 else ("medium" if locs else "low")})
        return out

    def drivers_of_signal(self, signal, max_hops=3):
        """Source locations of coarse logic feeding the register that drives `signal`.

        Walks from the signal's register D input back through at most `max_hops` coarse
        cells. Returns src locations and a confidence level with its basis."""
        bits = self.bits_of.get(signal) or self.bits_of.get(f"\\{signal}")
        if not bits:
            return {"src": [], "confidence": "low", "basis": f"signal {signal} not found in coarse netlist"}
        frontier = {self.driver.get(b) for b in bits} - {None}
        seen, locs, hops = set(), [], 0
        while frontier and hops <= max_hops:
            nxt = set()
            for cname in sorted(frontier - seen):
                seen.add(cname)
                c = self.cells[cname]
                src = [s for s in c.get("attributes", {}).get("src", "").split("|") if s]
                if c["type"] in COARSE_FF | COARSE_LATCH:
                    nxt |= {self.driver.get(b) for b in c["connections"].get("D", [])} - {None}
                    continue
                locs.extend(f"{c['type']}@{s}" for s in src)
                if c["type"] not in COARSE_STRUCT:
                    for pin, bs in c["connections"].items():
                        if c.get("port_directions", {}).get(pin) == "input":
                            nxt |= {self.driver.get(b) for b in bs} - {None}
            frontier, hops = nxt, hops + 1
        locs = sorted(set(locs))
        files_lines = {re.sub(r"\.\d+-.*$", "", l.split("@", 1)[1]) for l in locs}
        conf = "high" if len(files_lines) == 1 else ("medium" if 1 < len(files_lines) <= 3 else "low")
        return {"src": locs, "confidence": conf,
                "basis": "register Q name -> coarse register -> D-cone coarse cells' src attributes"}
