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


def simulate(root, cid, tools, extended=False):
    rd = root / "runs" / f"{cid}-{'extended' if extended else 'tb'}"
    rd.mkdir(parents=True)
    top = "extended_tb" if extended else "tb"
    tb = root / "frozen" / ("extended_tb.sv" if extended else STEM + "_test.sv")
    cmd = [tools["iverilog"], "-g2012", "-s", top, "-o", "sim.vvp",
           str(root / "rtl" / cid / "dut.v"), str(root / "frozen" / (STEM + "_ref.sv")), str(tb)]
    cj = run_job(cmd, rd, rd / "compile.log", 60)
    rec = {"status": "COMPILE_FAILED", "jobs": [cj]}
    if not successful(cj):
        return rec
    sj = run_job([tools["vvp"], "-n", "sim.vvp"], rd, rd / "sim.log", 30)
    rec["jobs"].append(sj)
    log = (rd / "sim.log").read_text()
    regex = r"^EXT_RESULT errors=(\d+) samples=(\d+)$" if extended else r"^Mismatches: (\d+) in (\d+) samples$"
    m = re.search(regex, log, re.M)
    normal_end = bool(re.search(r"^EXT_PASSED$", log, re.M)) if extended else bool(re.search(r"^Simulation finished at \d+ ps$", log, re.M))
    rec.update(errors=int(m[1]) if m else None, samples=int(m[2]) if m else None,
               completion_evidence=normal_end, status="INCONCLUSIVE")
    if sj["timed_out"] or re.search(r"^TIMEOUT$", log, re.M):
        rec["status"] = "TIMEOUT"
    elif m and int(m[1]) > 0:
        rec["status"] = "TB_FAILED"
    elif successful(sj) and m and normal_end and int(m[2]) >= (2048 if extended else 200) and not re.search(r"^(ERROR|FATAL|Error):", log, re.M):
        rec["status"] = "TB_PASSED"
    # VCD is a large disposable rendering; logs and the stimulus remain replayable.
    (rd / "wave.vcd").unlink(missing_ok=True)
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


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    root = args.output.resolve()
    if root.exists():
        sys.exit("Output exists: choose a new directory; previous evidence is never overwritten.")
    repo = S2G / "third_party" / "verilog-eval"
    head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    if head != COMMIT:
        sys.exit("Dataset commit mismatch")
    tracked = [f"dataset_spec-to-rtl/{STEM}{s}" for s in ("_ref.sv", "_test.sv", "_prompt.txt")]
    if subprocess.check_output(["git", "-C", str(repo), "status", "--porcelain", "--", *tracked], text=True):
        sys.exit("Dataset inputs have local modifications")
    tools = {"iverilog": str(S2G / "tools/iverilog-v12/bin/iverilog"),
             "vvp": str(S2G / "tools/iverilog-v12/bin/vvp"), "yosys": shutil.which("yosys"),
             "verilator": shutil.which("verilator")}
    for value in tools.values():
        if not value or not Path(value).is_file():
            sys.exit(f"Missing required tool: {value}")
    root.mkdir(parents=True)
    frozen = root / "frozen"
    frozen.mkdir()
    for name in tracked:
        shutil.copyfile(repo / name, frozen / Path(name).name)
    shutil.copyfile(repo / "LICENSE", frozen / "DATASET_LICENSE")
    (frozen / "extended_tb.sv").write_text(extension_tb())
    original = (frozen / (STEM + "_ref.sv")).read_text()
    baseline = original.replace("module RefModule", "module TopModule", 1)
    start, end = baseline.index("  always @(*) begin"), baseline.rindex("\nendmodule")
    bodies = {"B0": baseline, "T0-01": baseline[:start] + grouped_body(True) + baseline[end:],
              "T0-02": baseline[:start] + grouped_body(False) + baseline[end:],
              "NEG": baseline.replace("4'h0: out = a;", "4'h0: out = b;", 1)}
    for cid, text in bodies.items():
        rd = root / "rtl" / cid
        rd.mkdir(parents=True)
        (rd / "dut.v").write_text(text)
        if cid != "B0":
            assert text[:start] == baseline[:start] and text[text.rindex("\nendmodule"):] == baseline[end:]
            (rd / "patch.diff").write_text("".join(difflib.unified_diff(baseline.splitlines(True), text.splitlines(True), fromfile="B0/dut.v", tofile=f"{cid}/dut.v")))
    pilot = json.loads((PILOT / "pilot_config.json").read_text())
    libpath = Path("/opt/OpenROAD-flow-scripts/flow/platforms/sky130hd/lib/sky130_fd_sc_hd__tt_025C_1v80.lib")
    assert sha256_file(libpath) == pilot["synthesis"]["liberty_sha256"]
    dont = (PILOT / "flow/sky130hd_dont_use.txt").read_text()
    ys = Template((PILOT / "flow/synth.ys.tmpl").read_text()).substitute(
        read_cmds="read_verilog -sv rtl/dut.v", top="TopModule", liberty=str(libpath), abc_delay_ps=10000,
        dont_use=" ".join(f"-dont_use {l.strip()}" for l in dont.splitlines() if l.strip() and not l.startswith("#")))
    (frozen / "synth.ys").write_text(ys)
    manifest = {"schema": "rtltrial.dev.mux9.v1", "stage": "DEV_SCREEN_NOT_B1_B2",
                "source_url": "https://github.com/NVlabs/verilog-eval", "source_commit": head,
                "design": STEM, "baseline_edit": "RefModule renamed to TopModule only; TB/reference unchanged",
                "transform": "local exact-selector mux grouping, preserving every branch and default; no new don't-cares",
                "candidate_plan": {"T0-01": "group selector bits [3:2] then [1:0]", "T0-02": "group bit [0] then [3:1]"},
                "budget": {"template_attempts": 2, "baseline_synth_repeats": 3, "negative_controls": 1, "model_calls": 0},
                "rules": {"minimum_area_improvement": 0.005, "suspicious_area_drop": 0.10,
                          "require_original_tb": True, "require_extension_tb": True, "require_combinational_equivalence": True,
                          "require_zero_registers_and_latches": True, "require_no_new_synthesis_warnings": True},
                "pass_contract": {"original_tb": "Mismatches: 0 in N samples, N>=200; normal finish; no TIMEOUT or runtime failure",
                                  "extension": "EXT_PASSED and errors=0 samples=2048",
                                  "formal": "Yosys SAT SUCCESS and zero exit, for all binary inputs"},
                "tools": tools, "tool_versions": {k: tool_version([v, "-V"]) for k, v in tools.items()},
                "simulator_source_commit": "4fd5291632232fbe1ba49b2c26bb6b2bf1c6c9cf",
                "liberty": str(libpath), "liberty_sha256": sha256_file(libpath),
                "area_units": "Liberty area units (not assumed um^2)", "abc_delay_target_ps": 10000,
                "timing": "NOT_RUN: area-only combinational DEV; no timing/P&R/power claim",
                "extension_scope": "all 256 four-state selectors x 8 frozen data patterns; not exhaustive over data",
                "files": {str(p.relative_to(root)): sha256_file(p) for p in sorted(root.rglob("*")) if p.is_file()},
                "runner_hashes": {str(p.relative_to(PILOT)): sha256_file(p) for p in [Path(__file__), PILOT / "rtltrial/netlist.py", PILOT / "rtltrial/liberty.py", PILOT / "rtltrial/util.py"]}}
    manifest["contract_hash"] = sha256_json(manifest)
    write_json(root / "manifest.json", manifest)
    for p in list(frozen.iterdir()) + list((root / "rtl").rglob("dut.v")) + [root / "manifest.json"]:
        p.chmod(0o444)
    result = {"contract_hash": manifest["contract_hash"], "status": "RUNNING", "records": {}, "paid_model_calls": 0}

    def save(status=None):
        if status:
            result["status"] = status
        write_json(root / "results.json", result)

    def stop(reason):
        result["blocking_reason"] = reason
        save("BLOCKED")
        sys.exit(reason)

    b = result["records"]["B0"] = {}
    b["lint"] = lint(root, "B0", tools)
    b["original_tb"] = simulate(root, "B0", tools)
    b["extension_tb"] = simulate(root, "B0", tools, True)
    save()
    if b["lint"]["status"] != "PASS" or any(b[k]["status"] != "TB_PASSED" for k in ("original_tb", "extension_tb")):
        stop("Baseline test contract did not pass")
    lib = load_liberty(str(libpath), root / "cache")
    b["synth_repeats"] = [synth(root, "B0", tools, ys, lib, n) for n in (1, 2, 3)]
    save()
    if any(r["status"] != "SYNTH_DONE" or not r["check_clean"] for r in b["synth_repeats"]):
        stop("Baseline synthesis incomplete")
    bm = b["synth_repeats"][0]["metrics"]
    if len({(r["metrics"]["area"], r["metrics"]["fingerprint"]) for r in b["synth_repeats"]}) != 1:
        stop("Baseline synthesis not reproducible")
    neg = result["records"]["NEG"] = {"original_tb": simulate(root, "NEG", tools), "formal": formal(root, "NEG", tools)}
    save()
    if neg["original_tb"]["status"] != "TB_FAILED" or neg["formal"]["status"] != "FAIL":
        stop("Negative control not detected by both TB and equivalence")
    for cid in ("T0-01", "T0-02"):
        rec = result["records"][cid] = {"decision": "REJECT"}
        rec["lint"] = lint(root, cid, tools)
        rec["original_tb"] = simulate(root, cid, tools)
        rec["extension_tb"] = simulate(root, cid, tools, True)
        rec["formal"] = formal(root, cid, tools)
        save()
        if rec["lint"]["status"] != "PASS" or rec["original_tb"]["status"] != "TB_PASSED" or rec["extension_tb"]["status"] != "TB_PASSED" or rec["formal"]["status"] != "PASS":
            rec["reason"] = "validation failed; no synthesis"
            save()
            continue
        rec["synth"] = synth(root, cid, tools, ys, lib)
        sr = rec["synth"]
        if sr["status"] != "SYNTH_DONE":
            rec.update(decision="INCONCLUSIVE", reason="synthesis incomplete")
            save()
            continue
        cm = sr["metrics"]
        gain = (bm["area"] - cm["area"]) / bm["area"]
        rec["relative_area_improvement"] = gain
        new_lint = {tuple(w) for w in rec["lint"]["warnings"]} - {tuple(w) for w in b["lint"]["warnings"]}
        if new_lint or not sr["check_clean"] or cm["coarse_ff_bits"] or cm["coarse_latch_bits"] or cm["mapped_ff_cells"] or cm["mapped_latch_cells"] or cm["undef_pin_bits"] or set(sr["warnings"]) - set(b["synth_repeats"][0]["warnings"]):
            rec.update(decision="REJECT", reason="structural risk or new synthesis warnings")
        elif cm["fingerprint"] == bm["fingerprint"] and cm["cells_by_type"] == bm["cells_by_type"]:
            rec.update(decision="COLLAPSED", reason="same normalized fingerprint and cell distribution")
        elif gain > 0.10:
            rec.update(decision="INCONCLUSIVE", reason="area drop exceeds frozen 10% threshold; audit required despite equivalence pass")
        elif gain >= 0.005:
            rec.update(decision="PROVISIONAL_KEEP", reason="area improved with TB, extension and combinational binary equivalence evidence")
        else:
            rec.update(decision="REJECT", reason="distinct structure but area improvement below 0.5%")
        save()
        print(cid, rec["decision"], "area", cm["area"], "gain", f"{gain:.2%}", flush=True)
    for path, digest in manifest["files"].items():
        if sha256_file(root / path) != digest:
            stop(f"Frozen file changed: {path}")
    jobs = []

    def gather(obj):
        if isinstance(obj, dict):
            jobs.extend(obj.get("jobs", []))
            for k, value in obj.items():
                if k != "jobs":
                    gather(value)
        elif isinstance(obj, list):
            for value in obj:
                gather(value)
    gather(result["records"])
    result["cost"] = {"tool_invocations": len(jobs), "tool_wall_s": round(sum(j["elapsed_s"] for j in jobs), 3),
                      "dependency_build": "separate one-time Icarus v12 build; logs under S2G/third_party/iverilog-v12-*.log"}
    result["recommended_next_step"] = "freeze a separate B1/B2 protocol only after reviewing DEV evidence; no model comparison performed"
    save("COMPLETE")
    with (root / "summary.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["candidate", "original_tb", "extension_tb", "formal", "area", "cells", "depth", "decision"])
        writer.writeheader()
        for cid, rec in result["records"].items():
            m = rec.get("synth", {}).get("metrics", bm if cid == "B0" else {})
            writer.writerow({"candidate": cid, "original_tb": rec["original_tb"]["status"], "extension_tb": rec.get("extension_tb", {}).get("status", "NOT_RUN"),
                             "formal": rec.get("formal", {}).get("status", "NOT_RUN"), "area": m.get("area"), "cells": m.get("cells"),
                             "depth": m.get("logic_depth"), "decision": rec.get("decision", "BASELINE" if cid == "B0" else "DETECTED")})
    print(root / "summary.csv", flush=True)


if __name__ == "__main__":
    main()
