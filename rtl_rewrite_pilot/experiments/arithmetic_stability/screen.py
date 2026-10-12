#!/usr/bin/env python3
"""Reproducible, model-free DEV screen; not a B1/B2 experiment.

Run from any directory: python3 run.py --output /path/to/new-run-directory
The upstream TB and reference are copied byte-for-byte. Only the DUT module
name is adapted. Both candidates are fixed before any metrics are measured.
"""
import argparse
import csv
import difflib
import json
import random
import re
import shutil
import subprocess
import sys
from pathlib import Path
from string import Template

HERE = Path(__file__).resolve().parent
PILOT = HERE.parents[1]
S2G = PILOT.parent
sys.path.insert(0, str(PILOT))
from rtltrial.liberty import load_liberty
from rtltrial.netlist import Coarse, Mapped
from rtltrial.util import run_job, sha256_file, sha256_json, sha256_text, tool_version, write_json

STEM = "Prob097_mux9to1v"
EXT_SAMPLES = 2048
COMMIT = "c498220d0a52248f8e3fdffe279075215bde2da6"
PORTS = list("abcdefghi")


def grouped_body(high_first):
    """Partition exact selector values; retain default=ffff at every level."""
    outer, inner = ("sel[3:2]", "sel[1:0]") if high_first else ("sel[0]", "sel[3:1]")
    ow, iw = (2, 2) if high_first else (1, 3)
    groups = {}
    for n, port in enumerate(PORTS):
        a, b = (n >> 2, n & 3) if high_first else (n & 1, n >> 1)
        groups.setdefault(a, []).append((b, port))
    lines = ["  always @(*) begin", f"    case ({outer})"]
    for a, entries in groups.items():
        lines += [f"      {ow}'d{a}: begin", f"        case ({inner})"]
        lines += [f"          {iw}'d{b}: out = {port};" for b, port in entries]
        lines += ["          default: out = 16'hffff;", "        endcase", "      end"]
    lines += ["      default: out = 16'hffff;", "    endcase", "  end"]
    return "\n".join(lines)


def extension_tb():
    # All 4^4 four-state selector values, with eight fixed data patterns each.
    # This exhausts selectors, not the entire 148-bit four-state input domain.
    rng = random.Random(20261011)
    patterns = [[f"16'h{rng.getrandbits(16):04x}" for _ in PORTS] for _ in range(4)]
    patterns += [["16'h0000"] * 9, ["16'hffff"] * 9,
                 ["16'b" + "".join("01xz"[(i + j) % 4] for j in range(16)) for i in range(9)],
                 ["16'b" + "".join("zx10"[(i + j) % 4] for j in range(16)) for i in range(9)]]
    conn = ", ".join(f".{p}({p})" for p in PORTS) + ", .sel(sel)"
    lines = ["`timescale 1ps/1ps", "module extended_tb;",
             "  reg [15:0] " + ", ".join(PORTS) + ";", "  reg [3:0] sel;",
             "  wire [15:0] good, actual;", "  integer samples=0, errors=0;",
             f"  RefModule ref_dut({conn}, .out(good));",
             f"  TopModule dut({conn}, .out(actual));", "  initial begin"]
    for pattern in patterns:
        lines += [f"    {p} = {v};" for p, v in zip(PORTS, pattern)]
        for n in range(256):
            bits = "".join("01xz"[(n >> (2 * k)) & 3] for k in (3, 2, 1, 0))
            lines += [f"    sel = 4'b{bits}; #1; samples=samples+1; if (actual !== good) errors=errors+1;"]
    lines += ['    $display("EXT_RESULT errors=%0d samples=%0d", errors, samples);',
              '    if (errors != 0) $fatal(1, "EXT_FAILED");', '    $display("EXT_PASSED");',
              "    $finish;", "  end", "endmodule"]
    return "\n".join(lines) + "\n"


def successful(job):
    return job["exit_code"] == 0 and not job["timed_out"]


def lint(root, cid, tools):
    rd = root / "runs" / f"{cid}-lint"
    rd.mkdir(parents=True)
    job = run_job([tools["verilator"], "--lint-only", "-Wall", "-Wno-fatal", "--top-module", "TopModule",
                   str(root / "rtl" / cid / "dut.v")], rd, rd / "lint.log", 60)
    log = (rd / "lint.log").read_text()
    warnings = re.findall(r"^%Warning-(\w+): [^\n]+?:\d+:\d+: (.*)$", log, re.M)
    return {"status": "PASS" if successful(job) and "%Error" not in log else "FAIL",
            "warnings": [list(w) for w in warnings], "jobs": [job]}


SPEC=None

def simulate(root,cid,tools,extended=False):
    rd=root/"runs"/f"{cid}-{'extended' if extended else 'tb'}";rd.mkdir(parents=True)
    files=[root/"rtl"/cid/"dut.v"]
    if extended:
        tops=['extended_tb'];files += [root/'frozen'/ (STEM+'_ref.sv'),root/'frozen/extended_tb.sv']
    else:
        tops=[SPEC['tb_top'],'observer'];files += [root/'frozen/wrapper.sv',root/'frozen/observer.sv',root/'frozen'/ (STEM+'_test.sv')]
    cmd=[tools['iverilog'],'-g2012']
    for top in tops:cmd+=['-s',top]
    cmd+=['-o','sim.vvp']+[str(f) for f in files]
    cj=run_job(cmd,rd,rd/'compile.log',60);rec={'status':'COMPILE_FAILED','jobs':[cj]}
    if not successful(cj):return rec
    sj=run_job([tools['vvp'],'-n','sim.vvp'],rd,rd/'sim.log',60);rec['jobs'].append(sj)
    log=(rd/'sim.log').read_text();rec['status']='INCONCLUSIVE'
    if extended:
        m=re.search(r'^EXT_RESULT errors=(\d+) samples=(\d+)$',log,re.M)
        if m:rec.update(errors=int(m[1]),samples=int(m[2]))
        positive=m and int(m[1])==0 and int(m[2])==EXT_SAMPLES and 'EXT_PASSED' in log
    else:
        m=re.search(r'^OBS samples=(\d+) known=(\d+) errors=(\d+)$',log,re.M)
        if m:rec.update(samples=int(m[1]),known=int(m[2]),errors=int(m[3]))
        positive=m and int(m[1])==int(m[2])==100 and int(m[3])==0 and 'Your Design Passed' in log
    if sj['timed_out']:rec['status']='TIMEOUT'
    elif rec.get('errors',0)>0:rec['status']='TB_FAILED'
    elif successful(sj) and positive and not re.search(r'^(ERROR|FATAL|Error):',log,re.M):rec['status']='TB_PASSED'
    return rec


def formal(root, cid, tools):
    rd = root / "runs" / f"{cid}-formal"
    rd.mkdir(parents=True)
    script = (f"read_verilog -sv {root / 'frozen' / (STEM + '_ref.sv')} {root / 'rtl' / cid / 'dut.v'}\n"
              "proc\nopt\n"
              "miter -equiv -flatten RefModule TopModule equiv\n"
              "prep -top equiv\nflatten\n"
              "sat -verify -prove trigger 0 -set-def-inputs -show-inputs -show-outputs\n")
    (rd / "prove.ys").write_text(script)
    job = run_job([tools["yosys"], "-s", "prove.ys"], rd, rd / "formal.log", 60)
    log = (rd / "formal.log").read_text()
    status = "TIMEOUT" if job["timed_out"] else "UNKNOWN"
    if successful(job) and "SAT proof finished - no model found: SUCCESS!" in log:
        status = "PASS"
    elif "proof did fail" in log or "model found: FAIL!" in log:
        status = "FAIL"
    return {"status": status, "scope": "unbounded combinational equivalence for all defined binary inputs; no sequential state", "jobs": [job]}


def synth(root, cid, tools, ys_template, lib, repeat=None):
    rd = root / "runs" / (f"{cid}-synth" + (f"-{repeat}" if repeat else ""))
    (rd / "rtl").mkdir(parents=True)
    shutil.copyfile(root / "rtl" / cid / "dut.v", rd / "rtl" / "dut.v")
    (rd / "synth.ys").write_text(ys_template)
    job = run_job([tools["yosys"], "-s", "synth.ys"], rd, rd / "synth.log", 120)
    rec = {"status": "TIMEOUT" if job["timed_out"] else "FAILED", "jobs": [job]}
    if not successful(job):
        return rec
    stat = next(iter(json.loads((rd / "stat.json").read_text())["modules"].values()))
    mapped = Mapped(json.loads((rd / "mapped.json").read_text()), "TopModule", lib)
    coarse = Coarse(json.loads((rd / "coarse.json").read_text()), "TopModule")
    unresolved = {k: v for k, v in stat["num_cells_by_type"].items() if k.startswith("$")}
    if unresolved or mapped.unknown_cells:
        rec["reason"] = "incomplete mapping"
        return rec
    ff, latch = coarse.reg_bits()
    check = (rd / "check.txt").read_text()
    rec.update(status="SYNTH_DONE", metrics={"area": stat["area"], "cells": stat["num_cells"],
               "cells_by_type": stat["num_cells_by_type"], "logic_depth": mapped.logic_depth(),
               "coarse_ff_bits": ff, "coarse_latch_bits": latch, **mapped.counts(),
               "fingerprint": mapped.fingerprint(), "netlist_sha256": sha256_file(rd / "netlist.v")},
               check_clean="Found and reported 0 problems." in check,
               warnings=re.findall(r"^Warning:.*$", (rd / "synth.log").read_text(), re.M),
               timing={"status": "NOT_RUN", "reason": "combinational design without an owner-provided timing contract; area-only DEV"})
    write_json(rd / "synth_result.json", rec)
    return rec

