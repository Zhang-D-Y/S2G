"""Baseline establishment and the per-candidate pipeline:
scope/transform check -> lint + compile + original TB -> synthesis -> decision.

State flow: CREATED -> COMPILED -> TB_PASSED -> SYNTH_DONE -> KEEP | REJECT | COLLAPSED;
any stage may end in FAILED, TIMEOUT or INCONCLUSIVE. Stages only run if reached.
"""
import os
from collections import Counter
from pathlib import Path

from . import region as R
from .feedback import build_feedback
from .manifest import ws_dir
from .synth import cached_or_run
from .tb import compile_and_run, copy_tree, lint
from .util import hash_tree, now_iso, read_json, sha256_text, write_json

FORMAL_NOT_RUN = "NOT_RUN"


def rel(ws, p):
    return os.path.relpath(p, ws)


# ---------------- versions (RTL contents addressed by hash) ----------------

def version_path(ws, vid):
    return Path(ws) / "versions" / f"{vid}.json"


def load_version(ws, vid):
    return read_json(version_path(ws, vid))


def register_version(ws, vid, rtl_dir, origin, lint_res, synth_res, synth_run_dir):
    rec = {"version_id": vid, "origin": origin, "rtl_dir": rel(ws, rtl_dir),
           "lint_warnings": lint_res["warnings"], "synth_status": synth_res["status"],
           "synth_run": rel(ws, synth_run_dir), "metrics": synth_res["metrics"],
           "synth_warnings": synth_res["warnings"],
           "feedback": f"feedback/{vid}.json", "registered_at": now_iso()}
    write_json(version_path(ws, vid), rec)
    return rec


# ---------------- baseline ----------------

def establish_baseline(man):
    ws = ws_dir(man["design_id"])
    rtl_dir, tb_dir = ws / "baseline" / "rtl", ws / "baseline" / "tb"
    vid = man["baseline_rtl_hash"]
    runs = ws / "runs"
    rec = {"design_id": man["design_id"], "version_id": vid, "contract_hash": man["contract_hash"],
           "started_at": now_iso(), "status": None, "blocking_reason": None, "jobs": []}

    lr = lint(man, rtl_dir, man["rtl_files"], _mk(runs / "baseline-lint"))
    rec["lint"] = {"errors": lr["errors"], "warnings": lr["warnings"]}
    rec["jobs"].append(lr["job"])
    if lr["errors"]:
        return _block(ws, rec, "baseline lint errors")

    tr = compile_and_run(man, rtl_dir, man["rtl_files"], tb_dir, _mk(runs / "baseline-tb"))
    rec["tb"] = {k: tr[k] for k in ("compile_status", "tb_status", "evidence")}
    rec["jobs"] += [j for j in (tr["compile_job"], tr["sim_job"]) if j]
    if tr["tb_status"] != "TB_PASSED":
        return _block(ws, rec, f"original design does not pass its original TB: {tr['compile_status']}/{tr['tb_status']}")

    reps = []
    for i in range(man["synthesis"]["repeat_runs"]):
        rd = runs / f"baseline-synth-rep{i + 1}"
        sr = cached_or_run(man, rtl_dir, man["rtl_files"], vid, rd, ws / "cache", force=True)
        reps.append((rd, sr))
        rec["jobs"] += sr["jobs"]
        if sr["status"] != "SYNTH_DONE":
            return _block(ws, rec, f"baseline synthesis rep{i + 1}: {sr['status']} {sr['reason']}")
    areas = [sr["metrics"]["area"] for _, sr in reps]
    fps = {sr["metrics"]["fingerprint"] for _, sr in reps}
    var = (max(areas) - min(areas)) / min(areas)
    rec["repeatability"] = {"runs": len(reps), "areas": areas, "rel_area_variation": var,
                            "distinct_fingerprints": len(fps),
                            "seed_control": "Yosys/ABC expose no seed in this flow; single-threaded; measured by repeat runs"}
    tol = man["acceptance_rule"]["min_rel_area_improvement"]
    if var >= tol or len(fps) != 1:
        return _block(ws, rec, f"repeat-run variation {var:.4g} (fingerprints {len(fps)}) not below tolerance {tol}")

    rd, sr = reps[0]
    register_version(ws, vid, rtl_dir, "baseline", lr, sr, rd)
    fb = build_feedback(ws, man, vid, sr, rd)
    rec["synth"] = {"metrics": sr["metrics"], "timing": sr["timing"], "warnings": sr["warnings"]}
    rec["feedback_hash"] = fb["feedback_hash"]
    rec["status"] = "BASELINE_OK"
    rec["finished_at"] = now_iso()
    write_json(ws / "baseline_record.json", rec)
    return rec


def _block(ws, rec, reason):
    rec.update(status="BLOCKED", blocking_reason=reason, finished_at=now_iso())
    write_json(Path(ws) / "baseline_record.json", rec)
    return rec


def _mk(p):
    Path(p).mkdir(parents=True, exist_ok=True)
    return p


# ---------------- candidates ----------------

def cand_dir(ws, cid):
    return Path(ws) / "candidates" / cid


def load_cand(ws, cid):
    return read_json(cand_dir(ws, cid) / "candidate_record.json")


def save_cand(ws, rec):
    write_json(cand_dir(ws, rec["candidate_id"]) / "candidate_record.json", rec)


def create_candidate(man, arm, cid, parent_vid, region_id, new_body, attempt_index, proposal,
                     prior_bodies=(), retry_of=None):
    """Materialize a candidate from a region replacement. Scope, transform-class and
    duplicate checks happen here; a failed check still consumes the attempt."""
    ws = ws_dir(man["design_id"])
    reg = next(r for r in man["regions"] if r["id"] == region_id)
    parent = load_version(ws, parent_vid)
    prtl = ws / parent["rtl_dir"]
    ptext = (prtl / reg["file"]).read_text()
    pbody = R.body(ptext, reg)
    d = cand_dir(ws, cid)
    if d.exists():
        raise SystemExit(f"candidate {cid} already exists; records are never overwritten")
    rtl = d / "rtl"
    copy_tree(man["rtl_files"], prtl, rtl)
    rec = {"candidate_id": cid, "arm": arm, "attempt_index": attempt_index, "retry_of": retry_of,
           "created_at": now_iso(), "parent_hash": parent_vid, "golden_hash": man["baseline_rtl_hash"],
           "changed_region": {"id": region_id, "file": reg["file"], "parent_body": pbody.strip(),
                              "candidate_body": None if new_body is None else new_body.strip()},
           "transform_id": man["allowed_transform"]["id"], "preconditions": reg.get("preconditions"),
           "expected_effect": proposal.get("intended_effect"),
           "prompt_hash": proposal.get("prompt_hash"), "model_version": proposal.get("model_version"),
           "input_feedback_hash": proposal.get("input_feedback_hash"), "token_usage": proposal.get("token_usage"),
           "proposal_elapsed_s": proposal.get("elapsed_s"), "proposal_log": proposal.get("log"),
           "patch_hash": None, "version_id": None, "state": "CREATED", "compile_status": None,
           "original_tb_status": None,
           "extended_tb_status": {"value": None, "reason": "NOT_RUN: no frozen extension tests in v1"},
           "formal_status": FORMAL_NOT_RUN, "synth_status": None, "metrics": None, "delta": None,
           "warnings": None, "decision": None, "reason": None, "jobs": [], "log_paths": {}}

    def fail(state, decision, reason):
        rec.update(state=state, decision=decision, reason=reason, finished_at=now_iso())
        save_cand(ws, rec)
        return rec

    if new_body is None:
        return fail("FAILED", "REJECT", proposal.get("error") or "no proposal produced")
    try:
        ctext = R.replace(ptext, reg, new_body)
    except ValueError as e:
        return fail("FAILED", "REJECT", f"cannot apply region replacement: {e}")
    (rtl / reg["file"]).chmod(0o644)
    (rtl / reg["file"]).write_text(ctext)
    patch = R.unified_patch(ptext, ctext, reg["file"])
    (d / "patch.diff").write_text(patch)
    rec["patch_hash"] = sha256_text(patch)
    rec["version_id"] = hash_tree(rtl, man["rtl_files"])

    ok, why = R.scope_check(ptext, ctext, reg)
    if not ok:
        return fail("FAILED", "REJECT", f"scope: {why}")
    if arm != "NEG":
        ok, why = R.check_transform(man["allowed_transform"], pbody, new_body)
        if not ok:
            return fail("FAILED", "REJECT", f"transform: {why}")
        canon = R.canonical(new_body)
        if canon == R.canonical(pbody) or canon in {R.canonical(b) for b in prior_bodies}:
            return fail("FAILED", "DUPLICATE", "duplicate of parent or an earlier candidate in this arm")
    save_cand(ws, rec)
    return rec


def test_candidate(man, cid):
    ws = ws_dir(man["design_id"])
    rec = load_cand(ws, cid)
    if rec["state"] != "CREATED":
        return rec
    d = cand_dir(ws, cid)
    runs = ws / "runs"
    parent = load_version(ws, rec["parent_hash"])
    lr = lint(man, d / "rtl", man["rtl_files"], _mk(runs / f"{cid}-lint"))
    rec["jobs"].append(lr["job"])
    rec["log_paths"]["lint"] = rel(ws, lr["job"]["log"])
    new_w = Counter(lr["warnings"]) - Counter(parent["lint_warnings"])
    rec["lint"] = {"errors": lr["errors"], "new_warnings": dict(new_w)}
    if lr["errors"]:
        rec.update(state="FAILED", compile_status="LINT_ERROR", decision="REJECT", reason="lint errors")
        save_cand(ws, rec)
        return rec
    tr = compile_and_run(man, d / "rtl", man["rtl_files"], ws / "baseline" / "tb", _mk(runs / f"{cid}-tb"))
    rec["jobs"] += [j for j in (tr["compile_job"], tr["sim_job"]) if j]
    rec["log_paths"]["tb"] = rel(ws, runs / f"{cid}-tb")
    rec["compile_status"] = tr["compile_status"]
    rec["original_tb_status"] = tr["tb_status"]
    rec["tb_evidence"] = tr["evidence"]
    if tr["compile_status"] != "COMPILED":
        rec.update(state="FAILED", decision="REJECT", reason="compile failed")
    elif tr["tb_status"] == "TB_PASSED":
        rec["state"] = "TB_PASSED"
    else:
        state = {"TIMEOUT": "TIMEOUT", "INCONCLUSIVE": "INCONCLUSIVE"}.get(tr["tb_status"], "FAILED")
        rec.update(state=state, decision="REJECT" if state == "FAILED" else "INCONCLUSIVE",
                   reason=f"original TB: {tr['tb_status']} {tr['evidence'].get('reason', '')}".strip())
    save_cand(ws, rec)
    return rec


def synth_candidate(man, cid):
    ws = ws_dir(man["design_id"])
    rec = load_cand(ws, cid)
    if rec["state"] != "TB_PASSED":
        return rec
    rd = ws / "runs" / f"{cid}-synth"
    sr = cached_or_run(man, cand_dir(ws, cid) / "rtl", man["rtl_files"], rec["version_id"], rd, ws / "cache")
    rec["jobs"] += sr["jobs"] if not sr.get("reused") else []
    rec["synth_reused"] = sr.get("reused", False)
    rec["log_paths"]["synth"] = rel(ws, rd)
    rec["synth_status"] = sr["status"]
    if sr["status"] != "SYNTH_DONE":
        rec.update(state="INCONCLUSIVE" if sr["status"] == "TIMEOUT" else "FAILED",
                   decision="INCONCLUSIVE", reason=f"synthesis {sr['status']}: {sr['reason']}")
        save_cand(ws, rec)
        return rec
    rec["metrics"] = sr["metrics"]
    rec["timing"] = sr["timing"]
    rec["warnings"] = sr["warnings"]
    rec["state"] = "SYNTH_DONE"
    save_cand(ws, rec)
    return rec


def decide(man, cid):
    """Preregistered acceptance rule; applied identically to every arm."""
    ws = ws_dir(man["design_id"])
    rec = load_cand(ws, cid)
    if rec["state"] != "SYNTH_DONE":
        return rec
    rule = man["acceptance_rule"]
    parent = load_version(ws, rec["parent_hash"])
    golden = load_version(ws, rec["golden_hash"])
    pm, cm = parent["metrics"], rec["metrics"]
    rec["delta"] = {
        "vs_parent": _delta(pm, cm), "vs_golden": _delta(golden["metrics"], cm),
        "timing_note": "mapped STA under pilot-authored SDC; reported only, not used for decisions"}
    risks = []
    if rule["require_equal_coarse_ff_bits"] and cm["coarse_ff_bits"] != pm["coarse_ff_bits"]:
        risks.append(f"register bits {pm['coarse_ff_bits']} -> {cm['coarse_ff_bits']}")
    if rule["require_no_new_latches"] and cm["coarse_latch_bits"] > pm["coarse_latch_bits"]:
        risks.append("new latch bits")
    if rule["require_no_new_lint_warnings"] and rec["lint"]["new_warnings"]:
        risks.append(f"new lint warnings: {list(rec['lint']['new_warnings'])[:3]}")
    new_sw = sorted(set(rec["warnings"]) - set(parent["synth_warnings"]))
    if rule["require_no_new_synth_warnings"] and new_sw:
        risks.append(f"new synthesis warnings: {new_sw[:3]}")
    rel_gain = (pm["area"] - cm["area"]) / pm["area"]
    if cm["fingerprint"] == pm["fingerprint"]:
        decision, reason = "COLLAPSED", "normalized netlist fingerprint equals parent's; synthesis erased the source change"
    elif risks:
        decision, reason = "REJECT", "structural risk: " + "; ".join(risks)
    elif rel_gain > rule["suspicious_rel_area_drop"]:
        decision, reason = "INCONCLUSIVE", f"area drop {rel_gain:.1%} exceeds suspicion threshold; paused for audit"
    elif rel_gain >= rule["min_rel_area_improvement"]:
        decision, reason = "KEEP", f"area improved {rel_gain:.2%} vs parent (>= {rule['min_rel_area_improvement']:.2%}); TB-passed, awaiting stronger validation"
    else:
        decision, reason = "REJECT", f"area change {rel_gain:+.2%} vs parent below tolerance {rule['min_rel_area_improvement']:.2%}"
    rec.update(decision=decision, reason=reason, state=decision if decision in ("KEEP", "REJECT", "COLLAPSED") else "INCONCLUSIVE",
               finished_at=now_iso())
    if decision == "KEEP":
        d = cand_dir(ws, cid)
        lr = {"warnings": dict(Counter(parent["lint_warnings"]) + Counter(rec["lint"]["new_warnings"]))}
        sr = {"status": "SYNTH_DONE", "metrics": cm, "warnings": rec["warnings"]}
        register_version(ws, rec["version_id"], d / "rtl", cid, lr, sr, ws / rec["log_paths"]["synth"])
        build_feedback(ws, man, rec["version_id"], read_json(ws / rec["log_paths"]["synth"] / "synth_result.json"),
                       ws / rec["log_paths"]["synth"])
    save_cand(ws, rec)
    return rec


def _delta(a, b):
    return {"area": round(b["area"] - a["area"], 4), "area_rel": round((b["area"] - a["area"]) / a["area"], 6),
            "cells": b["cells"] - a["cells"], "logic_depth": b["logic_depth"] - a["logic_depth"],
            "coarse_ff_bits": b["coarse_ff_bits"] - a["coarse_ff_bits"],
            "const_pin_bits": b["const_pin_bits"] - a["const_pin_bits"],
            "same_fingerprint": a["fingerprint"] == b["fingerprint"]}


def run_candidate(man, cid):
    """Advance a candidate through every stage it can reach (idempotent, resumable)."""
    test_candidate(man, cid)
    synth_candidate(man, cid)
    return decide(man, cid)
