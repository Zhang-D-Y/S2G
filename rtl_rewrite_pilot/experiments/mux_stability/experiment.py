#!/usr/bin/env python3
"""Repeated multi-design non-thinking Flash B1/B2 mux pilot. prepare freezes/evaluates; live spends; audit verifies."""
import argparse
import csv
import difflib
import importlib.util
import json
import os
import shutil
import re
import subprocess
import guard
from designs import DESIGNS, extension_tb
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
PILOT = HERE.parents[1]
DEV = PILOT / "dev/verilogeval_mux9_v1"
spec = importlib.util.spec_from_file_location("muxscreen", HERE / "screen.py")
screen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(screen)
from guard import check
from rtltrial.netlist import Coarse
from rtltrial.util import read_json, write_json, sha256_file, sha256_json, sha256_text, run_job, tool_version
from rtltrial.liberty import load_liberty

SYSTEM = "You propose one local RTL mux rewrite. Return one JSON object only; do not call tools."
COMMON = """Optimize the supplied combinational RTL for lower technology-mapped cell area.
The evaluator uses fixed Yosys 0.51+101, synth -flatten, sky130hd tt_025C_1v80,
the same dont-use list, and ABC delay target 10000 ps for every candidate.
Area is the only optimization objective. Logic depth is diagnostic, not timing.

Replace exactly the single combinational always block; ports and all other text
remain unchanged. Return JSON: {"block": "always @(*) begin ... end", "intended_effect": "..."}.
Allowed class: exact-selector mux regrouping. Preserve sel=0:a,1:b,2:c,3:d,4:e,
5:f,6:g,7:h,8:i; sel=9..15 and any X/Z selector must yield 16'hffff. Preserve
the selected 16-bit data unchanged, including X/Z data behavior.
Only one always @(*) / always @* / always_comb block is allowed. Inside, use
begin/end, case/endcase with constant labels, if/else and blocking out= assignments.
Conditions and case selectors may read only sel, fixed sel slices, unsigned
constants and Boolean/comparison operators. RHS leaves may only be a..i or
16'hffff (also '1); conditional ?: selection among such leaves is allowed.
No declarations, extra signals, loops, functions, arithmetic/data manipulation,
casez/casex, attributes, directives, comments, X/Z constants or don't-cares.
Widths, interfaces and combinational behavior must stay fixed. No TB edits.

Acceptance requires original TB, a frozen extension and combinational binary
equivalence to the immutable original reference, complete library mapping, no
new lint/synthesis warnings or structural risks, and area improvement >=0.5%
relative to the current incumbent. Drops >10% require a separate mapped-netlist
equivalence audit before promotion. Synthesis may erase a rewrite; this is not a gain.
Each proposal consumes an attempt, including malformed, invalid, failing,
duplicate or collapsed proposals. Two attempts in two rounds (1 then 1).
Round-one candidates share the baseline parent; round-two starts from the best
accepted round-one candidate, or the original. This controller selection leaks
limited selection information to both arms. No retry or replacement calls.
Shared validation feedback is restricted to syntax/compile/test/equivalence
outcomes. No prior DEV candidate or result is supplied. Block <=6000 UTF-8 bytes.
"""
SETTINGS = {"model": "deepseek-flash", "base_url": "https://api.deepseek.com",
            "thinking": {"type": "disabled"}, "reasoning_effort": None, "top_p": None,
            "temperature": 1.0, "temperature_note": "explicit non-thinking sampling; same for both arms",
            "max_tokens": 8192, "max_input_bytes": 24000, "message_overhead_token_reserve": 1024,
            "timeout_s": 300, "max_calls": 4, "currency": "CNY", "spending_ceiling": 5.0,
            "peak_price_per_million": {"input_miss": 2.0, "input_hit": 0.04, "output": 8.0},
            "pricing_source": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
            "pricing_checked_date": "2026-10-11", "retries": 0}
CALL_RESERVE = ((SETTINGS["max_input_bytes"] + 1024) * 2 + 8192 * 8) / 1e6
ORDER = [(1, "B1", 1), (1, "B2", 1), (2, "B1", 2), (2, "B2", 2)]

BASE_COMMON = COMMON
SPEC = None
REPEAT = None

def configure(design, repeat):
    global SPEC,REPEAT,COMMON,ORDER
    SPEC=DESIGNS[design]; REPEAT=repeat; guard.configure(SPEC)
    screen.STEM=SPEC['stem']; screen.EXT_SAMPLES=8*4**SPEC['selector_width']
    text=BASE_COMMON
    a=text.index('Allowed class:'); b=text.index('Only one always',a)
    mapping=', '.join(str(i)+':'+p for i,p in enumerate(SPEC['mapping']))
    text=text[:a]+f"Allowed class: exact-selector mux regrouping. Preserve {SPEC['selector']}={mapping}; all other selector values, including X/Z, yield {SPEC['width']}'h{SPEC['default']:x}. Preserve selected data unchanged including X/Z.\n"+text[b:]
    text=text.replace('sel, fixed sel slices',SPEC['selector']+', fixed '+SPEC['selector']+' slices')
    text=text.replace('blocking out= assignments','blocking '+SPEC['output']+'= assignments')
    text=text.replace("RHS leaves may only be a..i or\n16'hffff (also '1)","RHS leaves may only be "+', '.join(SPEC['ports'])+f" or {SPEC['width']}'h{SPEC['default']:x}")
    COMMON=text
    first=['B1','B2'] if repeat%2 else ['B2','B1']
    ORDER=[(rnd,arm,rnd) for rnd in (1,2) for arm in first]


def now():
    return datetime.now(timezone.utc).isoformat()


def sources():
    return [Path(__file__), HERE / "guard.py", HERE / "screen.py", HERE / "designs.py", HERE / "cohort.py", PILOT / "rtltrial/netlist.py",
            PILOT / "rtltrial/liberty.py", PILOT / "rtltrial/util.py"]


def verify(root):
    man = read_json(root / "protocol.json")
    assert sha256_json({k: v for k, v in man.items() if k != "contract_hash"}) == man["contract_hash"]
    assert man["design_spec"] == SPEC and man["repeat"] == REPEAT
    for p, h in man["code_hashes"].items():
        assert sha256_file(PILOT / p) == h, f"runner changed: {p}"
    for p, h in man["frozen_hashes"].items():
        assert sha256_file(root / p) == h, f"frozen input changed: {p}"
    assert sha256_file(man["liberty"]) == man["liberty_sha256"]
    for k, binary in man["tools"].items():
        assert tool_version([binary, "-V"]) == man["tool_versions"][k], "tool changed"
    return man


def region(text):
    start=re.search(r"\balways(?:_comb)?\b",text).start()
    return start,text.rindex("\nendmodule")

def block_of(text):
    start,end=region(text)
    return text[start:end].strip()


def materialize(root, cid, block):
    original = (root / "rtl/B0/dut.v").read_text()
    start, end = region(original)
    text = original[:start] + block.strip() + "\n" + original[end:]
    out = root / "rtl" / cid
    out.mkdir(parents=True)
    (out / "dut.v").write_text(text)
    (out / "patch.diff").write_text("".join(difflib.unified_diff(original.splitlines(True), text.splitlines(True), fromfile="B0/dut.v", tofile=f"{cid}/dut.v")))
    return sha256_file(out / "dut.v")


def feedback(root, cid, sr):
    assert sr["status"] == "SYNTH_DONE"
    run = root / "runs" / ("B0-synth-1" if cid == "B0" else f"{cid}-synth")
    data = {"version_sha256": sha256_file(root / "rtl" / cid / "dut.v"),
            "source_run": str(run.relative_to(root)), "source_result_sha256": sha256_file(run / "synth_result.json"),
            "stage": "completed technology-mapped synthesis only", "metrics": sr["metrics"],
            "source_localization": Coarse(read_json(run / "coarse.json"), "TopModule").structure()[:16],
            "localization_note": "native Yosys src; may be many-to-many; confidence per entry",
            "timing": sr["timing"], "warnings": sr["warnings"], "completed_at": now()}
    data["feedback_hash"] = sha256_json(data)
    write_json(root / "feedback" / f"{cid}.json", data)


def prepare(root):
    if root.exists():
        raise SystemExit("choose a new run directory")
    oldroot = DEV / "run_002"
    old = read_json(oldroot / "manifest.json")
    for p, h in old["files"].items():
        assert sha256_file(oldroot / p) == h
    for p, h in old["runner_hashes"].items():
        assert sha256_file(PILOT / p) == h
    root.mkdir(parents=True)
    repo=PILOT.parent / "third_party/verilog-eval"
    assert subprocess.check_output(["git","-C",str(repo),"rev-parse","HEAD"],text=True).strip()==screen.COMMIT
    frozen=root / "frozen"; frozen.mkdir()
    tracked=["dataset_spec-to-rtl/"+SPEC["stem"]+suffix for suffix in ("_ref.sv","_test.sv","_prompt.txt")]
    assert not subprocess.check_output(["git","-C",str(repo),"status","--porcelain","--",*tracked],text=True)
    for name in tracked: shutil.copyfile(repo/name,frozen/Path(name).name)
    shutil.copyfile(repo/"LICENSE",frozen/"DATASET_LICENSE")
    shutil.copyfile(oldroot/"frozen/synth.ys",frozen/"synth.ys")
    (frozen/"extended_tb.sv").write_text(extension_tb(SPEC))
    baseline=(frozen/(SPEC["stem"]+"_ref.sv")).read_text().replace("module RefModule","module TopModule",1)
    start,end=region(baseline)
    pattern=r"("+SPEC["output"]+r"\s*=\s*)"+SPEC["mapping"][0]+r"\s*;"
    negbody,n=re.subn(pattern,lambda m:m[1]+SPEC["mapping"][1]+";",baseline[start:end],count=1)
    assert n==1
    for cid,text in (("B0",baseline),("NEG",baseline[:start]+negbody+baseline[end:])):
        folder=root/"rtl"/cid; folder.mkdir(parents=True);(folder/"dut.v").write_text(text)
    (root / "frozen/shared_prompt.txt").write_text(SYSTEM + "\n" + COMMON)
    for p in sources():
        dest = root / "code_snapshot" / p.relative_to(PILOT)
        dest.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(p, dest)
    man = {"schema": "rtltrial.mux9.feedback.v1", "frozen_at": now(),
           "stage": SPEC["stratum"], "design": screen.STEM, "design_spec":SPEC, "repeat":REPEAT,
           "source_commit": old["source_commit"], "source_dev_contract": old["contract_hash"],
           "llm": SETTINGS, "maximum_reserved_cost_cny": CALL_RESERVE * 4,
           "rounds": [1, 1], "call_order": ORDER, "arms": {"B1": "no synthesis diagnostics", "B2": "B1 plus completed-run synthesis feedback"},
           "transform": "one exact-selector mux block; fail-closed grammar in guard.py",
           "rules": {"minimum_area_improvement": 0.005, "suspicious_area_drop": 0.10,
                     "large_drop_audit": "require extra mapped-netlist SAT equivalence and all normal functional/structural checks; otherwise INCONCLUSIVE and not promoted"},
           "tools": old["tools"], "tool_versions": old["tool_versions"],
           "liberty": old["liberty"], "liberty_sha256": old["liberty_sha256"],
           "timing": old["timing"], "indirect_selection_leak": True,
           "failure_cost": "reserve slot before network; unknown billing charged at full reservation; no automatic retries",
           "cold_start": "fresh original TB, extension, 3 synth repetitions and negative controls; common cost and one original synthesis charge per standalone arm",
           "code_hashes": {str(p.relative_to(PILOT)): sha256_file(p) for p in sources()},
           "frozen_hashes": {str(p.relative_to(root)): sha256_file(p) for p in sorted(root.rglob('*')) if p.is_file()}}
    man["contract_hash"] = sha256_json(man)
    write_json(root / "protocol.json", man)
    for p in root.rglob("*"):
        if p.is_file(): p.chmod(0o444)
    baseline = {"status": "RUNNING", "started_at": now()}
    write_json(root / "baseline.json", baseline)
    baseline["lint"] = screen.lint(root, "B0", man["tools"])
    baseline["original_tb"] = screen.simulate(root, "B0", man["tools"])
    baseline["extension_tb"] = screen.simulate(root, "B0", man["tools"], True)
    assert baseline["lint"]["status"] == "PASS"
    assert baseline["original_tb"]["status"] == baseline["extension_tb"]["status"] == "TB_PASSED"
    lib = load_liberty(man["liberty"], root / "cache")
    ys = (root / "frozen/synth.ys").read_text()
    baseline["synth_repeats"] = [screen.synth(root, "B0", man["tools"], ys, lib, n) for n in (1, 2, 3)]
    assert all(s["status"] == "SYNTH_DONE" and s["check_clean"] for s in baseline["synth_repeats"])
    assert len({(s["metrics"]["area"], s["metrics"]["fingerprint"]) for s in baseline["synth_repeats"]}) == 1
    baseline["negative_tb"] = screen.simulate(root, "NEG", man["tools"])
    baseline["negative_formal"] = screen.formal(root, "NEG", man["tools"])
    assert baseline["negative_tb"]["status"] == "TB_FAILED" and baseline["negative_formal"]["status"] == "FAIL"
    baseline["status"] = "BASELINE_OK"; baseline["completed_at"] = now()
    write_json(root / "baseline.json", baseline)
    feedback(root, "B0", baseline["synth_repeats"][0])
    write_json(root / "state.json", {"status": "PREPARED", "attempts": [], "incumbents": {"B1": "B0", "B2": "B0"}, "closed_rounds": [], "reserved_cny": 0})
    for arm in ("B1", "B2"):
        prompt, shared, refs = make_prompt(root, arm, 1, 1)
        write_json(root / "dryrun" / f"{arm}.json", {"system": SYSTEM, "user": prompt, "shared": shared, "feedback_refs": refs})
    print("PREPARED", man["contract_hash"], "max CNY", round(CALL_RESERVE * 4, 6), flush=True)


def read_attempt(root, cid):
    return read_json(root / "attempts" / cid / "record.json")


def make_prompt(root, arm, rnd, index):
    state = read_json(root / "state.json")
    parent = state["incumbents"][arm]
    prior = [read_attempt(root, a["id"]) for a in state["attempts"] if a["arm"] == arm]
    history = []
    for rec in prior:
        # Allowlist: no decision/reason/delta/area enters shared validation history.
        history.append({"attempt": rec["index"], "block": (rec.get("block") or "")[:1500],
                        "block_truncated": len(rec.get("block") or "") > 1500,
                        "scope": rec.get("scope_status"), "lint": rec.get("lint", {}).get("status"),
                        "original_tb": rec.get("original_tb", {}).get("status"),
                        "extension_tb": rec.get("extension_tb", {}).get("status"),
                        "formal": rec.get("formal", {}).get("status")})
    shared = COMMON + f"\nRound {rnd}, attempt {index}/2.\nCurrent RTL:\n" + (root / "rtl" / parent / "dut.v").read_text()
    shared += "\nFrozen original testbench:\n" + (root / "frozen" / (screen.STEM + "_test.sv")).read_text()
    shared += f"\nExtension contract: all {4**SPEC['selector_width']} four-state selectors x 8 frozen data patterns, including X/Z; compare immutable reference.\n"
    shared += "Earlier validation outcomes in this arm:\n" + json.dumps(history, ensure_ascii=True)
    refs, prompt = [], shared
    if arm == "B2":
        ids = list(dict.fromkeys([parent] + [r["id"] for r in prior if r["round"] < rnd and r.get("synth", {}).get("status") == "SYNTH_DONE"]))
        packages = []
        for cid in ids:
            fb = read_json(root / "feedback" / f"{cid}.json")
            assert sha256_file(root / fb["source_run"] / "synth_result.json") == fb["source_result_sha256"]
            packages.append({k: fb[k] for k in ("version_sha256", "stage", "metrics", "source_localization", "localization_note", "timing", "warnings")})
            refs.append({"id": cid, "feedback_hash": fb["feedback_hash"], "file_sha256": sha256_file(root / "feedback" / f"{cid}.json")})
        prompt += "\nSYNTHESIS_FEEDBACK_FROM_COMPLETED_RUNS\n" + json.dumps(packages, separators=(",", ":"))
    if len((SYSTEM + prompt).encode()) > SETTINGS["max_input_bytes"]:
        raise ValueError("frozen prompt input byte limit exceeded")
    return prompt, shared, refs


def balance():
    key = os.environ.get("LLM_API_KEY")
    assert key and os.environ.get("LLM_MODEL") == SETTINGS["model"]
    assert os.environ.get("LLM_BASE_URL", "").rstrip("/").removesuffix("/v1") == SETTINGS["base_url"]
    req = urllib.request.Request(SETTINGS["base_url"] + "/user/balance", headers={"Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=30) as response:
        body = json.loads(response.read())
    row = next((x for x in body.get("balance_infos", []) if x.get("currency") == "CNY"), None)
    assert body.get("is_available") is True and row is not None, "account balance unavailable"
    return {"at": now(), "currency": "CNY", "total_balance": float(row["total_balance"]), "available": True}


def call_provider(request):
    key = os.environ["LLM_API_KEY"]
    req = urllib.request.Request(SETTINGS["base_url"] + "/chat/completions", data=json.dumps(request).encode(),
                                 headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    rec = {"started_at": now(), "http_status": None, "error": None, "response": None}
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=SETTINGS["timeout_s"]) as r:
            rec["http_status"] = r.status; rec["response"] = json.loads(r.read())
    except urllib.error.HTTPError as e:
        rec["http_status"] = e.code
        rec["error"] = (f"HTTP {e.code}: " + e.read().decode(errors="replace")[:500]).replace(key, "[REDACTED]")
    except Exception as e:
        rec["error"] = (type(e).__name__ + ": " + str(e)[:300]).replace(key, "[REDACTED]")
    rec["elapsed_s"] = round(time.monotonic() - start, 3); rec["completed_at"] = now()
    return rec


def gate_audit(root, cid, man):
    rd = root / "runs" / f"{cid}-mapped-audit"; rd.mkdir(parents=True)
    (rd / "mapped.v").write_text((root / "runs" / f"{cid}-synth/netlist.v").read_text())
    script = (f"read_liberty -ignore_miss_func {man['liberty']}\n"
              f"read_verilog -sv {root / 'frozen' / (screen.STEM + '_ref.sv')}\n"
              "read_verilog mapped.v\nproc\nopt\n"
              "miter -equiv -flatten RefModule TopModule equiv\nprep -top equiv\nflatten\n"
              "sat -verify -prove trigger 0 -set-def-inputs -show-inputs -show-outputs\n")
    (rd / "prove.ys").write_text(script)
    job = run_job([man["tools"]["yosys"], "-s", "prove.ys"], rd, rd / "formal.log", 120)
    text = (rd / "formal.log").read_text()
    status = "PASS" if screen.successful(job) and "SAT proof finished - no model found: SUCCESS!" in text else "UNKNOWN"
    if "proof did fail" in text: status = "FAIL"
    return {"status": status, "jobs": [job], "scope": "mapped cells expanded using frozen Liberty functions; all binary inputs"}


def evaluate(root, rec, man, lib):
    cid = rec["id"]
    ok, why = check(rec["block"])
    rec["scope_status"] = "PASS" if ok else "FAIL"
    if not ok:
        rec.update(decision="REJECT", reason="scope: " + why); return
    state = read_json(root / "state.json")
    parent = rec["parent"]
    seen = [block_of((root / "rtl/B0/dut.v").read_text())]
    seen += [read_attempt(root, a["id"]).get("block", "") for a in state["attempts"] if a["arm"] == rec["arm"] and a["id"] != cid]
    if any(check(s)[0] and check(s)[1] == why for s in seen):
        rec.update(decision="DUPLICATE", reason="same normalized block as original or earlier arm attempt"); return
    rec["rtl_sha256"] = materialize(root, cid, rec["block"])
    rec["lint"] = screen.lint(root, cid, man["tools"])
    if rec["lint"]["status"] != "PASS":
        rec.update(decision="REJECT", reason="lint failure"); return
    rec["original_tb"] = screen.simulate(root, cid, man["tools"])
    if rec["original_tb"]["status"] != "TB_PASSED":
        rec.update(decision="REJECT", reason="original TB did not pass"); return
    rec["extension_tb"] = screen.simulate(root, cid, man["tools"], True)
    if rec["extension_tb"]["status"] != "TB_PASSED":
        rec.update(decision="REJECT", reason="extension TB did not pass"); return
    rec["formal"] = screen.formal(root, cid, man["tools"])
    if rec["formal"]["status"] != "PASS":
        rec.update(decision="REJECT", reason="binary RTL equivalence did not pass"); return
    rec["synth"] = screen.synth(root, cid, man["tools"], (root / "frozen/synth.ys").read_text(), lib)
    sr = rec["synth"]
    if sr["status"] != "SYNTH_DONE":
        rec.update(decision="INCONCLUSIVE", reason="synthesis incomplete"); return
    feedback(root, cid, sr)
    base = read_json(root / "baseline.json")
    pr = base if parent == "B0" else read_attempt(root, parent)
    ps = base["synth_repeats"][0] if parent == "B0" else pr["synth"]
    cm, pm = sr["metrics"], ps["metrics"]
    gain = (pm["area"] - cm["area"]) / pm["area"]
    rec["relative_area_improvement"] = gain
    new_lint = {tuple(w) for w in rec["lint"]["warnings"]} - {tuple(w) for w in pr["lint"]["warnings"]}
    if new_lint or not sr["check_clean"] or any(cm[k] for k in ("coarse_ff_bits", "coarse_latch_bits", "mapped_ff_cells", "mapped_latch_cells", "undef_pin_bits")) or set(sr["warnings"]) - set(ps["warnings"]):
        rec.update(decision="REJECT", reason="new lint/synthesis warning or structural risk")
    elif cm["fingerprint"] == pm["fingerprint"] and cm["cells_by_type"] == pm["cells_by_type"]:
        rec.update(decision="COLLAPSED", reason="same normalized fingerprint and cell distribution")
    elif gain > 0.10:
        rec["large_drop_audit"] = gate_audit(root, cid, man)
        if rec["large_drop_audit"]["status"] == "PASS":
            rec.update(decision="KEEP", reason="area improvement >10%; frozen extra mapped-equivalence audit passed")
        else:
            rec.update(decision="INCONCLUSIVE", reason="large area drop awaiting successful mapped-equivalence audit")
    elif gain >= 0.005:
        rec.update(decision="KEEP", reason="area improvement >=0.5%; all functional/structural gates passed")
    else:
        rec.update(decision="REJECT", reason="area improvement below frozen 0.5% threshold")


def close_round(root, rnd):
    state = read_json(root / "state.json")
    if rnd in state["closed_rounds"]: return
    for arm in ("B1", "B2"):
        rows = [read_attempt(root, a["id"]) for a in state["attempts"] if a["arm"] == arm and a["round"] == rnd]
        eligible = [r for r in rows if r.get("decision") == "KEEP"]
        if eligible: state["incumbents"][arm] = min(eligible, key=lambda r: r["synth"]["metrics"]["area"])["id"]
    state["closed_rounds"].append(rnd); write_json(root / "state.json", state)


def live(root):
    man = verify(root)
    assert read_json(root / "baseline.json")["status"] == "BASELINE_OK"
    funding = balance()
    assert funding["total_balance"] >= SETTINGS["spending_ceiling"], "insufficient funding for frozen envelope"
    if not (root / "balance_before.json").exists(): write_json(root / "balance_before.json", funding)
    lib = load_liberty(man["liberty"], root / "cache")
    for rnd, arm, index in ORDER:
        if rnd == 2: close_round(root, 1)
        cid = f"{arm}-r{rnd}-a{index}"
        state = read_json(root / "state.json")
        if any(a["id"] == cid for a in state["attempts"]):
            # Spent network slots are never retried, including unknown outcomes.
            prior = read_attempt(root, cid)
            if prior.get("decision") is None:
                prior.update(decision="INCONCLUSIVE", reason="interrupted attempt; slot and full cost reservation retained")
                write_json(root / "attempts" / cid / "record.json", prior)
            continue
        verify(root)
        prompt, shared, refs = make_prompt(root, arm, rnd, index)
        request = {"model": SETTINGS["model"], "thinking": SETTINGS["thinking"],
                   "temperature": SETTINGS["temperature"],
                   "max_tokens": SETTINGS["max_tokens"], "response_format": {"type": "json_object"},
                   "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}], "stream": False}
        d = root / "attempts" / cid; d.mkdir(parents=True)
        write_json(d / "request.json", request)
        write_json(d / "prompt_audit.json", {"shared_sha256": sha256_text(shared), "shared": shared,
                                              "feedback_refs": refs, "prompt_sha256": sha256_text(prompt), "input_bytes": len((SYSTEM + prompt).encode())})
        rec = {"id": cid, "arm": arm, "round": rnd, "index": index, "parent": state["incumbents"][arm],
               "parent_sha256": sha256_file(root / "rtl" / state["incumbents"][arm] / "dut.v"),
               "reserved_at": now(), "reserved_cny": CALL_RESERVE, "decision": None, "block": ""}
        assert len(state["attempts"]) < 4 and state["reserved_cny"] + CALL_RESERVE < SETTINGS["spending_ceiling"]
        write_json(d / "record.json", rec)
        state["attempts"].append({"id": cid, "arm": arm, "round": rnd, "index": index})
        state["reserved_cny"] += CALL_RESERVE; state["status"] = "RUNNING"; write_json(root / "state.json", state)
        print("CALL", cid, "parent", rec["parent"], "input bytes", len((SYSTEM + prompt).encode()), flush=True)
        transport = call_provider(request)
        write_json(d / "response.json", transport)
        response = transport.get("response") or {}
        rec.update(transport_error=transport["error"], proposal_wall_s=transport["elapsed_s"], usage=response.get("usage"),
                   response_model=response.get("model"), response_id=response.get("id"))
        try:
            if transport["error"]: raise ValueError("provider failure")
            choice = response["choices"][0]
            rec["finish_reason"] = choice.get("finish_reason")
            if choice.get("finish_reason") != "stop": raise ValueError("incomplete provider response")
            obj = json.loads(choice["message"]["content"])
            if not isinstance(obj, dict) or not isinstance(obj.get("block"), str): raise ValueError("missing block")
            rec["block"] = obj["block"].strip(); rec["intended_effect"] = obj.get("intended_effect")
            evaluate(root, rec, man, lib)
        except Exception as exc:
            rec.update(decision="INCONCLUSIVE" if not isinstance(exc, (ValueError, KeyError, TypeError)) else "REJECT",
                       reason=type(exc).__name__ + ": " + str(exc)[:300])
        rec["completed_at"] = now(); write_json(d / "record.json", rec)
        print("RESULT", cid, rec["decision"], rec.get("reason"), flush=True)
    close_round(root, 2)
    state = read_json(root / "state.json"); state["status"] = "MODEL_CALLS_COMPLETE"; write_json(root / "state.json", state)
    try: write_json(root / "balance_after.json", balance())
    except Exception as exc: write_json(root / "balance_after.json", {"error_type": type(exc).__name__})
    for arm, cid in state["incumbents"].items():
        final = f"{arm}-final"
        if not (root / "rtl" / final).exists():
            shutil.copytree(root / "rtl" / cid, root / "rtl" / final)
            fr = {"version": cid, "rtl_sha256": sha256_file(root / "rtl" / final / "dut.v"),
                  "original_tb": screen.simulate(root, final, man["tools"]),
                  "extension_tb": screen.simulate(root, final, man["tools"], True),
                  "formal": screen.formal(root, final, man["tools"])}
            write_json(root / f"{arm}-final.json", fr)
    audit(root)


def tool_jobs(obj):
    if isinstance(obj, dict):
        return obj.get("jobs", []) + [j for k, v in obj.items() if k != "jobs" for j in tool_jobs(v)]
    if isinstance(obj, list): return [j for v in obj for j in tool_jobs(v)]
    return []


def audit(root):
    man = verify(root); state = read_json(root / "state.json"); base = read_json(root / "baseline.json")
    bm = base["synth_repeats"][0]["metrics"]
    failures, rows, ledger = [], [], []
    for a in state["attempts"]:
        cid = a["id"]; d = root / "attempts" / cid; rec = read_attempt(root, cid)
        req, pa = read_json(d / "request.json"), read_json(d / "prompt_audit.json")
        prompt = req["messages"][1]["content"]
        if req.get("model") != "deepseek-flash" or req.get("thinking") != {"type": "disabled"} or req.get("temperature") != 1.0 or "reasoning_effort" in req or "top_p" in req:
            failures.append(cid + ": non-thinking configuration mismatch")
        if rec.get("response_model") not in (None, "deepseek-flash"):
            failures.append(cid + ": response model mismatch")
        if pa["prompt_sha256"] != sha256_text(prompt): failures.append(cid + ": prompt hash")
        if rec["arm"] == "B1" and (prompt != pa["shared"] or pa["feedback_refs"]): failures.append(cid + ": B1 leakage")
        if rec["arm"] == "B2" and not prompt.startswith(pa["shared"] + "\nSYNTHESIS_FEEDBACK_FROM_COMPLETED_RUNS\n"): failures.append(cid + ": B2 delta")
        for ref in pa["feedback_refs"]:
            f = root / "feedback" / (ref["id"] + ".json"); fb = read_json(f)
            if sha256_file(f) != ref["file_sha256"] or fb["completed_at"] >= rec["reserved_at"]:
                failures.append(cid + ": future/changed feedback")
            if ref["id"] != "B0" and not ref["id"].startswith(rec["arm"] + "-"): failures.append(cid + ": cross-arm feedback")
        if rec.get("rtl_sha256") and sha256_file(root / "rtl" / cid / "dut.v") != rec["rtl_sha256"]: failures.append(cid + ": RTL hash")
        usage = rec.get("usage") or {}
        pn, on = usage.get("prompt_tokens"), usage.get("completion_tokens")
        cost = None
        if pn is not None and on is not None:
            hit = usage.get("prompt_cache_hit_tokens", 0); miss = usage.get("prompt_cache_miss_tokens", pn - hit)
            cost = (hit * 0.04 + miss * 2 + on * 8) / 1e6
            if pn > SETTINGS["max_input_bytes"] + 1024 or on > 8192: failures.append(cid + ": token bound")
        ledger.append({"id": cid, "prompt_tokens": pn, "completion_tokens": on,
                       "peak_price_cost_cny": cost, "reserved_cny": rec["reserved_cny"], "proposal_wall_s": rec.get("proposal_wall_s")})
        m = rec.get("synth", {}).get("metrics", {})
        rows.append({"id": cid, "arm": rec["arm"], "round": rec["round"], "parent": rec["parent"],
                     "original_tb": rec.get("original_tb", {}).get("status", "NOT_RUN"),
                     "extension_tb": rec.get("extension_tb", {}).get("status", "NOT_RUN"), "formal": rec.get("formal", {}).get("status", "NOT_RUN"),
                     "area": m.get("area"), "area_change_vs_B0_pct": (m["area"] / bm["area"] - 1) * 100 if m else None,
                     "cells": m.get("cells"), "depth": m.get("logic_depth"), "decision": rec["decision"], "reason": rec.get("reason")})
    if len(state["attempts"]) != 4 or any(sum(a["arm"] == arm for a in state["attempts"]) != 2 for arm in ("B1", "B2")): failures.append("unequal/incomplete budget")
    arms = {}
    common_jobs = tool_jobs(base)
    for arm in ("B1", "B2"):
        cid = state["incumbents"][arm]
        final = read_json(root / f"{arm}-final.json") if (root / f"{arm}-final.json").exists() else {}
        if not final or final["original_tb"]["status"] != "TB_PASSED" or final["extension_tb"]["status"] != "TB_PASSED" or final["formal"]["status"] != "PASS": failures.append(arm + ": final validation")
        metrics = bm if cid == "B0" else read_attempt(root, cid)["synth"]["metrics"]
        jobs = [j for a in state["attempts"] if a["arm"] == arm for j in tool_jobs(read_attempt(root, a["id"]))] + tool_jobs(final)
        arms[arm] = {"final_version": cid, "area": metrics["area"], "improvement_vs_B0_pct": (1 - metrics["area"] / bm["area"]) * 100,
                     "tool_invocations": len(jobs), "tool_wall_s": round(sum(j["elapsed_s"] for j in jobs), 3),
                     "cold_start_baseline_synth_charge_s": sum(j["elapsed_s"] for j in base["synth_repeats"][0]["jobs"])}
    if rows:
        with (root / "summary.csv").open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    out = {"status": "PASS" if not failures else "FAIL", "failures": failures, "baseline_area": bm["area"],
           "arms": arms, "model_calls": len(state["attempts"]), "ledger": ledger,
           "peak_price_cost_cny": sum(x["peak_price_cost_cny"] if x["peak_price_cost_cny"] is not None else x["reserved_cny"] for x in ledger),
           "common_cold_start_cost": {"tool_invocations": len(common_jobs), "tool_wall_s": round(sum(j["elapsed_s"] for j in common_jobs), 3)},
           "claim_scope": "one repeat of a frozen small mux cohort; aggregate separately by new-design and DEV strata; no timing, physical or power conclusion",
           "audited_at": now()}
    write_json(root / "audit.json", out)
    state["status"] = "COMPLETE" if not failures else "AUDIT_FAILED"; write_json(root / "state.json", state)
    print(json.dumps(out, indent=2), flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("command", choices=["prepare", "live", "audit"])
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--design", choices=list(DESIGNS), required=True)
    ap.add_argument("--repeat", type=int, choices=[1,2,3], required=True)
    a = ap.parse_args()
    configure(a.design,a.repeat)
    {"prepare": prepare, "live": live, "audit": audit}[a.command](a.root.resolve())


if __name__ == "__main__": main()
