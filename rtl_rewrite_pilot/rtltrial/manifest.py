"""Workspace layout, baseline import (read-only snapshot) and the frozen run manifest."""
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

from .synth import dont_use_cells, render_scripts, synth_contract_hash
from .tb import monitor_source
from .util import hash_tree, now_iso, read_json, sha256_file, sha256_json, sha256_text, tool_version, write_json

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = "rtltrial.run_manifest.v1"


def expand(s):
    """Expand ${VAR} and ${VAR:-default}."""
    return re.sub(r"\$\{(\w+)(?::-([^}]*))?\}", lambda m: os.environ.get(m.group(1)) or (m.group(2) or ""), s)


def ws_dir(design_id):
    return ROOT / "work" / design_id


def load_pilot():
    return read_json(ROOT / "pilot_config.json")


def load_design(design_id):
    return read_json(ROOT / "designs" / design_id / "design.json")


def manifest_path(design_id):
    return ws_dir(design_id) / "config" / "run_manifest.json"


def load_manifest(design_id):
    p = manifest_path(design_id)
    if not p.exists():
        raise SystemExit(f"no frozen manifest at {p}; run `rtltrial baseline` first")
    return read_json(p)


def _tools(pilot):
    bins = {k: shutil.which(v) or v for k, v in pilot["tools"].items()}
    vers = {
        "yosys": tool_version([bins["yosys_bin"], "-V"]),
        "sta": tool_version([bins["sta_bin"], "-version"]),
        "iverilog": tool_version([bins["iverilog_bin"], "-V"]),
        "verilator": tool_version([bins["verilator_bin"], "--version"]),
    }
    return {**bins, **vers}


def import_baseline(design, source_root):
    """Copy the correct original RTL/TB into a read-only snapshot and record provenance."""
    ws = ws_dir(design["design_id"])
    src = Path(source_root) / design["source"]["dir"]
    base = ws / "baseline"
    if base.exists():
        raise SystemExit(f"baseline snapshot already exists at {base}; it is never overwritten")
    try:
        head = subprocess.run(["git", "-C", str(source_root), "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
    except OSError:
        head = ""
    if head != design["source"]["commit"]:
        raise SystemExit(f"source commit mismatch: expected {design['source']['commit']}, found {head or 'none'}")
    prov = {"imported_at": now_iso(), "source": design["source"], "source_commit_verified": head,
            "correctness_basis": design["correctness_basis"], "baseline_edits": design["baseline_edits"],
            "known_quirks": design.get("known_quirks", []), "files": []}
    for kind in ("rtl", "tb"):
        for item in design["import"][kind]:
            s = src / item["from"]
            text = s.read_text()
            if "rename" in item:
                old, new = item["rename"]
                if text.count(old) != 1:
                    raise SystemExit(f"rename anchor {old!r} occurs {text.count(old)} times in {s}")
                text = text.replace(old, new)
            d = base / kind / item["to"]
            d.parent.mkdir(parents=True, exist_ok=True)
            d.write_text(text)
            prov["files"].append({"kind": kind, "from": item["from"], "to": f"baseline/{kind}/{item['to']}",
                                  "source_sha256": sha256_file(s), "snapshot_sha256": sha256_file(d)})
    write_json(base / "provenance.json", prov)
    for p in base.rglob("*"):
        if p.is_file():
            p.chmod(p.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    return prov


def freeze(design, pilot):
    ws = ws_dir(design["design_id"])
    syn = dict(pilot["synthesis"])
    syn["liberty"] = expand(syn["liberty"])
    tools = _tools(pilot)
    tb = {**design["tb"], **pilot["tb"]}
    man = {
        "schema_version": SCHEMA,
        "frozen_at": now_iso(),
        "design_id": design["design_id"],
        "top": design["top"],
        "clock_port": design["clock_port"],
        "parameters": design["parameters"],
        "defines": design["defines"],
        "rtl_files": design["rtl_files"],
        "baseline_rtl_hash": hash_tree(ws / "baseline" / "rtl", design["rtl_files"]),
        "tb_hash": hash_tree(ws / "baseline" / "tb", tb["files"]),
        "tb": tb,
        "monitor_hash": sha256_text(monitor_source(tb["monitor"])),
        "simulator_version": tools["iverilog"],
        "compile_cmd": f"iverilog -g2012 -s {tb['top']} -s rtltrial_monitor -o sim.vvp <rtl_files> <tb_files> rtltrial_monitor.v",
        "simulate_cmd": "vvp -n sim.vvp",
        "test_inputs": tb["test_inputs"],
        "pass_contract": tb["pass_contract"],
        "timeout_s": tb["timeout_s"],
        "tools": tools,
        "synthesis": syn,
        "synth_tool_version": tools["yosys"],
        "target_library_hash": sha256_file(syn["liberty"]),
        "dont_use_hash": sha256_json(dont_use_cells()),
        "metric_units": {"area": syn["area_unit"], "timing": "ns", "logic_depth": "std cells"},
        "regions": design["regions"],
        "allowed_transform": design["allowed_transform"],
        "negative_controls": design.get("negative_controls", []),
        "budget": pilot["budget"],
        "acceptance_rule": pilot["acceptance_rule"],
        "llm": pilot["llm"],
    }
    if man["target_library_hash"] != syn["liberty_sha256"]:
        raise SystemExit(f"liberty hash mismatch: {man['target_library_hash']} != {syn['liberty_sha256']}")
    ys, sdc, sta = render_scripts(man, [f"rtl/{f}" for f in design["rtl_files"]])
    man["synth_script_hash"] = sha256_text(ys)
    man["sta_script_hash"] = sha256_text(sta)
    man["sdc_hash"] = sha256_text(sdc)
    man["sdc_status"] = "PILOT_AUTHORED_UNTRUSTED: " + syn["sdc"]["provenance"]
    man["synth_contract_hash"] = synth_contract_hash(man, [f"rtl/{f}" for f in design["rtl_files"]])
    man["contract_hash"] = sha256_json({k: v for k, v in man.items() if k != "frozen_at"})
    return man


def validate(design_id, source_root=None):
    """Dry-run configuration check. Returns a list of problems (empty == OK)."""
    problems, notes = [], []
    pilot, design = load_pilot(), load_design(design_id)
    tools = _tools(pilot)
    for k, v in tools.items():
        if k.endswith("_bin") and not shutil.which(v):
            problems.append(f"tool not found: {k}={v}")
        if not k.endswith("_bin") and v.startswith("UNAVAILABLE"):
            problems.append(f"tool version unavailable: {k}")
    lib = expand(pilot["synthesis"]["liberty"])
    if not Path(lib).exists():
        problems.append(f"liberty not found: {lib}")
    elif sha256_file(lib) != pilot["synthesis"]["liberty_sha256"]:
        problems.append("liberty hash differs from pilot_config.liberty_sha256")
    rnames = {r["id"] for r in design["regions"]}
    for n in design.get("negative_controls", []):
        if n["region"] not in rnames:
            problems.append(f"negative control {n['id']} references unknown region {n['region']}")
    ws = ws_dir(design_id)
    mp = manifest_path(design_id)
    if source_root:
        src = Path(source_root) / design["source"]["dir"]
        for kind in ("rtl", "tb"):
            for item in design["import"][kind]:
                if not (src / item["from"]).exists():
                    problems.append(f"source file missing: {src / item['from']}")
    if mp.exists():
        man = read_json(mp)
        if hash_tree(ws / "baseline" / "rtl", man["rtl_files"]) != man["baseline_rtl_hash"]:
            problems.append("baseline RTL snapshot hash differs from manifest")
        if hash_tree(ws / "baseline" / "tb", man["tb"]["files"]) != man["tb_hash"]:
            problems.append("baseline TB snapshot hash differs from manifest")
        for k in ("yosys", "sta", "iverilog", "verilator"):
            if tools[k] != man["tools"][k]:
                problems.append(f"tool version changed: {k}: {man['tools'][k]} -> {tools[k]}")
        if sha256_file(man["synthesis"]["liberty"]) != man["target_library_hash"]:
            problems.append("liberty content changed since freeze")
        if synth_contract_hash(man, [f"rtl/{f}" for f in man["rtl_files"]]) != man["synth_contract_hash"]:
            problems.append("synthesis scripts/SDC/dont-use changed since freeze")
        if sha256_text(monitor_source(man["tb"]["monitor"])) != man["monitor_hash"]:
            problems.append("TB adapter changed since freeze")
        fresh = freeze(design, pilot)
        for k in ("pass_contract", "regions", "allowed_transform", "budget", "acceptance_rule", "llm", "synthesis"):
            if fresh[k] != man[k]:
                problems.append(f"config field '{k}' differs from frozen manifest")
        notes.append(f"manifest frozen at {man['frozen_at']}, contract {man['contract_hash'][:12]}")
    else:
        notes.append("no manifest yet (baseline not established)")
    return problems, notes
