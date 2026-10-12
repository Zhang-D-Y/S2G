"""Frozen synthesis + mapped STA for one RTL version, and metric extraction."""
import re
import shutil
from pathlib import Path
from string import Template

from .liberty import load_liberty
from .netlist import Coarse, Mapped
from .util import read_json, run_job, sha256_file, sha256_json, sha256_text, write_json

FLOW = Path(__file__).resolve().parent.parent / "flow"


def dont_use_cells():
    return [l.strip() for l in (FLOW / "sky130hd_dont_use.txt").read_text().splitlines()
            if l.strip() and not l.startswith("#")]


def render_scripts(man, rtl_files):
    syn = man["synthesis"]
    defines = " ".join(f"-D{k}={v}" for k, v in man["defines"].items())
    read_cmds = "\n".join(f"read_verilog -sv {defines} {f}".replace("  ", " ") for f in rtl_files)
    params = man.get("parameters") or {}
    if params:
        read_cmds += "\nchparam " + " ".join(f"-set {k} {v}" for k, v in params.items()) + f" {man['top']}"
    dont_use = " ".join(f"-dont_use {c}" for c in dont_use_cells())
    ys = Template((FLOW / "synth.ys.tmpl").read_text()).substitute(
        read_cmds=read_cmds, top=man["top"], liberty=syn["liberty"], dont_use=dont_use,
        abc_delay_ps=syn["abc_delay_ps"])
    sdc = Template((FLOW / "pilot.sdc.tmpl").read_text()).substitute(
        clock_port=man["clock_port"], period_ns=syn["sdc"]["period_ns"])
    sta = Template((FLOW / "sta.tcl.tmpl").read_text()).substitute(
        liberty=syn["liberty"], top=man["top"], sdc="constraints.sdc", npaths=syn["sta_paths"])
    return ys, sdc, sta


def synth_contract_hash(man, rtl_files):
    """Hash of everything that defines the synthesis result except the RTL content."""
    ys, sdc, sta = render_scripts(man, rtl_files)
    return sha256_json({"ys": ys, "sdc": sdc, "sta": sta,
                        "liberty": man["synthesis"]["liberty_sha256"],
                        "tools": [man["tools"]["yosys"], man["tools"]["sta"]]})


def run_synth(man, rtl_dir, rtl_files, out_dir, cache_dir):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for f in rtl_files:
        dst = out / "rtl" / f
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(rtl_dir) / f, dst)
    ys, sdc, sta = render_scripts(man, [f"rtl/{f}" for f in rtl_files])
    (out / "synth.ys").write_text(ys)
    (out / "constraints.sdc").write_text(sdc)
    (out / "sta.tcl").write_text(sta)
    syn = man["synthesis"]
    jobs = [run_job([man["tools"]["yosys_bin"], "-q", "-l", "yosys_full.log", "-s", "synth.ys"],
                    out, out / "yosys.log", syn["timeout_s"])]
    res = {"jobs": jobs, "status": None, "reason": None, "metrics": None, "timing": None,
           "warnings": [], "structure": [], "critical_paths": []}
    y = jobs[0]
    if y["timed_out"]:
        res.update(status="TIMEOUT", reason="yosys timeout")
        return res
    if y["exit_code"] != 0 or not (out / "stat.json").exists():
        res.update(status="FAILED", reason=f"yosys exit {y['exit_code']}")
        return res

    lib = load_liberty(syn["liberty"], cache_dir)
    stat = next(iter(read_json(out / "stat.json")["modules"].values()))
    mapped = Mapped(read_json(out / "mapped.json"), man["top"], lib)
    coarse = Coarse(read_json(out / "coarse.json"), man["top"])
    by_type = stat.get("num_cells_by_type", {})
    unmapped = {t: n for t, n in by_type.items() if t.startswith("$")}
    if unmapped or mapped.unknown_cells:
        res.update(status="FAILED", reason=f"incomplete technology mapping: {unmapped or dict(mapped.unknown_cells)}")
        return res
    ff_bits, latch_bits = coarse.reg_bits()
    m = {"area": stat["area"], "area_unit": syn["area_unit"], "cells": stat["num_cells"],
         "cells_by_type": by_type, "coarse_ff_bits": ff_bits, "coarse_latch_bits": latch_bits,
         "coarse_cells_by_type": coarse.type_counts(), **mapped.counts()}
    try:
        m["logic_depth"] = mapped.logic_depth()
    except ValueError as e:
        res.update(status="FAILED", reason=str(e))
        return res
    m["logic_depth_definition"] = ("max comb std cells between startpoints (inputs, sequential outputs) "
                                   "and endpoints (outputs, sequential inputs); tie cells excluded; rtltrial/netlist.py")
    m["fingerprint"] = mapped.fingerprint()
    m["netlist_sha256"] = sha256_file(out / "netlist.v")
    res["metrics"] = m
    res["structure"] = coarse.structure()
    res["warnings"] = sorted(set(_yosys_warnings(out / "yosys_full.log") + _check_problems(out / "check.txt")))

    s = run_job([man["tools"]["sta_bin"], "-no_init", "-no_splash", "-exit", "sta.tcl"],
                out, out / "sta.log", syn["timeout_s"])
    jobs.append(s)
    res["timing"] = _parse_sta(out, s, man)
    res["critical_paths"] = _localize(out, mapped, coarse, man)
    res["status"] = "SYNTH_DONE"
    return res


def _norm_warning(line):
    line = re.sub(r"\S+\.s?v:\d+(\.\d+(-\d+\.\d+)?)?", "<src>", line)
    return re.sub(r"\s+", " ", line).strip()


def _yosys_warnings(log):
    if not Path(log).exists():
        return []
    return [_norm_warning(l) for l in Path(log).read_text(errors="replace").splitlines()
            if l.startswith("Warning:")]


def _check_problems(check_txt):
    if not Path(check_txt).exists():
        return []
    return [_norm_warning(l) for l in Path(check_txt).read_text().splitlines()
            if l.strip().startswith(("Warning", "found and reported"))
            and "found and reported 0 problems" not in l]


def _parse_sta(out, job, man):
    log = Path(job["log"]).read_text(errors="replace")
    t = {"status": "UNKNOWN", "wns_ns": None, "tns_ns": None, "unit": "ns",
         "trusted": False, "sdc_provenance": man["synthesis"]["sdc"]["provenance"],
         "wire_model": "none: Liberty NLDM cell delays with pin-capacitance loads only; no wire load, no parasitics",
         "constraint_gaps": list(man["synthesis"]["sdc"]["known_gaps"])}
    errs = re.findall(r"^Error.*$", log, re.M)
    if job["timed_out"] or job["exit_code"] != 0 or errs:
        t["reason"] = f"sta exit {job['exit_code']}: {errs[:3]}"
        return t
    w = re.search(r"RTLTRIAL_WNS (\S+)", log)
    n = re.search(r"RTLTRIAL_TNS (\S+)", log)
    cs = re.search(r"RTLTRIAL_CHECK_SETUP_BEGIN\n(.*?)RTLTRIAL_CHECK_SETUP_END", log, re.S)
    if cs and cs.group(1).strip():
        t["constraint_gaps"].append("check_setup: " + " / ".join(cs.group(1).strip().splitlines()[:10]))
    if w and n:
        wns = float(w.group(1))
        t.update(status="REPORTED_UNTRUSTED", worst_slack_ns=round(wns * 1e9, 4),
                 wns_ns=round(min(wns, 0.0) * 1e9, 4), tns_ns=round(float(n.group(1)) * 1e9, 4))
    return t


def _localize(out, mapped, coarse, man):
    p = Path(out) / "sta_paths.json"
    if not p.exists():
        return []
    try:
        checks = read_json(p).get("checks", [])
    except ValueError:
        return []
    paths = []
    def inst(pin):
        return pin.split("/")[0] if "/" in pin else None

    def name(pin):
        i = inst(pin) or (pin if pin.lstrip("\\") in mapped.cells or f"\\{pin}" in mapped.cells else None)
        if i is None:
            return {"kind": "port", "signal": pin, "confidence": "high", "basis": "top-level port"}
        cell = next((k for k in (i, f"\\{i}", i.lstrip("\\")) if k in mapped.cells), None)
        sig = mapped.instance_signal(cell) if cell and mapped.seq_kind(cell) else None
        if not sig:
            return {"kind": "cell", "signal": None, "instance": i, "confidence": "low",
                    "basis": "instance has no named sequential output"}
        return {"kind": "register", "signal": sig[0].lstrip("\\"), "bit": sig[1], "confidence": "high",
                "basis": "sequential cell output net name"}

    for c in checks:
        if c.get("path_group") != man["clock_port"]:
            continue
        start, end = name(c.get("startpoint", "")), name(c.get("endpoint", ""))
        loc = coarse.drivers_of_signal(end["signal"]) if end["kind"] == "register" else \
            {"src": [], "confidence": "low", "basis": "endpoint is not a named register"}
        ends = {inst(c.get("startpoint", "")) or c.get("startpoint"), inst(c.get("endpoint", ""))}
        comb = {inst(s["pin"]) for s in c.get("source_path", [])} - ends - {None}
        paths.append({"slack_ns": round(c["slack"] * 1e9, 4), "startpoint": start, "endpoint": end,
                      "comb_cells_on_path": len(comb), "endpoint_cone_src": loc})
    return paths


def synth_job_key(man, rtl_hash, rtl_files):
    return sha256_text(f"synth|{rtl_hash}|{synth_contract_hash(man, rtl_files)}")


def cached_or_run(man, rtl_dir, rtl_files, rtl_hash, run_dir, cache_dir, force=False):
    """Reuse a completed run only when its job key (RTL + full contract) matches."""
    key = synth_job_key(man, rtl_hash, rtl_files)
    rec_path = Path(run_dir) / "synth_result.json"
    if rec_path.exists() and not force:
        rec = read_json(rec_path)
        if rec.get("job_key") == key and rec.get("status") == "SYNTH_DONE":
            rec["reused"] = True
            return rec
    rec = run_synth(man, rtl_dir, rtl_files, run_dir, cache_dir)
    rec.update(job_key=key, rtl_hash=rtl_hash, reused=False)
    write_json(rec_path, rec)
    return rec
