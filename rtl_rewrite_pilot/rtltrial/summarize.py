"""Results by design and arm, with complete cost accounting (failures included)."""
import csv
from collections import Counter
from pathlib import Path

from .arms import ARMS, state_path
from .evaluate import load_cand, load_version
from .manifest import ROOT, ws_dir
from .util import now_iso, read_json, write_json

FIELDS = ["design_id", "arm", "status", "baseline_area", "final_area", "final_vs_baseline_rel", "final_version",
          "promoted_candidates", "attempts", "tb_passed", "keep", "reject", "collapsed", "duplicate",
          "inconclusive", "failure_reasons", "llm_calls", "tokens_in", "tokens_out", "proposal_wall_s",
          "tool_invocations", "tool_wall_s", "evidence_scope"]


def _jobs_cost(jobs):
    return len(jobs), round(sum(j["elapsed_s"] for j in jobs), 3)


def summarize_design(design_id):
    ws = ws_dir(design_id)
    br = read_json(ws / "baseline_record.json")
    rows, detail = [], {"design_id": design_id, "baseline": {
        "status": br["status"], "blocking_reason": br["blocking_reason"], "version_id": br["version_id"],
        "tb_status": br.get("tb", {}).get("tb_status"),
        "area": br.get("synth", {}).get("metrics", {}).get("area"),
        "cold_start_cost": dict(zip(("tool_invocations", "tool_wall_s"), _jobs_cost(br["jobs"])))}}
    neg = ws / "negative_controls.json"
    detail["negative_controls"] = read_json(neg)["controls"] if neg.exists() else None
    base_area = detail["baseline"]["area"]
    detail["arms"] = {}
    for arm in ARMS:
        sp = state_path(ws, arm)
        if not sp.exists():
            continue
        st = read_json(sp)
        cands = [load_cand(ws, c) for c in st["attempts"]]
        dec = Counter(c["decision"] for c in cands)
        inv, wall = _jobs_cost([j for c in cands for j in c["jobs"]])
        usage = [c.get("token_usage") or {} for c in cands]
        final = load_version(ws, st["incumbent"])
        fa = final["metrics"]["area"] if final.get("metrics") else None
        fails = Counter(c["reason"].split(":")[0] for c in cands if c["decision"] not in ("KEEP",) and c["reason"])
        row = {
            "design_id": design_id, "arm": arm,
            "status": "finished" if st.get("finished_at") else "in_progress",
            "baseline_area": base_area, "final_area": fa,
            "final_vs_baseline_rel": round((fa - base_area) / base_area, 6) if fa and base_area else None,
            "final_version": st["incumbent"][:12] + (" (original retained)" if st["incumbent"] == br["version_id"] else ""),
            "promoted_candidates": ";".join(r["promoted"] for r in st["rounds"] if r["promoted"]),
            "attempts": len(cands), "tb_passed": sum(c["original_tb_status"] == "TB_PASSED" for c in cands),
            "keep": dec["KEEP"], "reject": dec["REJECT"], "collapsed": dec["COLLAPSED"],
            "duplicate": dec["DUPLICATE"], "inconclusive": dec["INCONCLUSIVE"],
            "failure_reasons": "; ".join(f"{k} x{v}" for k, v in fails.items()),
            "llm_calls": sum(1 for c in cands if c.get("proposal_log")),
            "tokens_in": sum(u.get("prompt_tokens", 0) for u in usage),
            "tokens_out": sum(u.get("completion_tokens", 0) for u in usage),
            "proposal_wall_s": round(sum(c.get("proposal_elapsed_s") or 0 for c in cands), 3),
            "tool_invocations": inv, "tool_wall_s": wall,
            "evidence_scope": "TB-passed only (formal NOT_RUN); synthesis-stage area; timing untrusted; no physical claims",
        }
        rows.append(row)
        detail["arms"][arm] = {"row": row, "rounds": st["rounds"], "candidates": [
            {k: c.get(k) for k in ("candidate_id", "attempt_index", "parent_hash", "changed_region", "compile_status",
                                   "original_tb_status", "formal_status", "synth_status", "decision", "reason",
                                   "delta", "token_usage", "model_version", "input_feedback_hash")}
            for c in cands]}
    return rows, detail


def summarize_all():
    out = ROOT / "results"
    out.mkdir(exist_ok=True)
    rows = []
    for d in sorted((ROOT / "work").iterdir()) if (ROOT / "work").exists() else []:
        if not (d / "baseline_record.json").exists():
            continue
        r, detail = summarize_design(d.name)
        rows += r
        detail["generated_at"] = now_iso()
        write_json(out / f"{d.name}.json", detail)
    with open(out / "summary.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    return rows, Path(out)
