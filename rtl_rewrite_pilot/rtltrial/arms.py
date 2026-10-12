"""Budgeted round loop per (design, arm) and the negative control.

The incumbent is a hash reference in state/<arm>.json, updated atomically at the end
of a round to the best KEEP candidate (lowest area). Rolling back means the reference
does not move; no candidate files or logs are deleted.
"""
from pathlib import Path

from .evaluate import create_candidate, load_cand, run_candidate, test_candidate
from .manifest import ws_dir
from .propose import LLM_ARMS, dry_run_prompt, llm_proposal, template_body
from .util import now_iso, read_json, write_json

ARMS = ("T0", "B1", "B2")


def state_path(ws, arm):
    return Path(ws) / "state" / f"{arm}.json"


def load_state(man, arm):
    ws = ws_dir(man["design_id"])
    p = state_path(ws, arm)
    if p.exists():
        return read_json(p)
    return {"arm": arm, "contract_hash": man["contract_hash"], "incumbent": man["baseline_rtl_hash"],
            "attempts": [], "rounds": [], "created_at": now_iso()}


def _require_baseline(ws):
    p = Path(ws) / "baseline_record.json"
    if not p.exists() or read_json(p)["status"] != "BASELINE_OK":
        raise SystemExit("baseline is not established (or BLOCKED); arms cannot run")


def run_arm(man, arm, live=False, log=print):
    if arm not in ARMS:
        raise SystemExit(f"unknown arm {arm}; expected one of {ARMS}")
    ws = ws_dir(man["design_id"])
    _require_baseline(ws)
    st = load_state(man, arm)
    if st["contract_hash"] != man["contract_hash"]:
        raise SystemExit("arm state was produced under a different contract")
    b = man["budget"]
    for r in range(1, b["max_rounds"] + 1):
        if any(x["round"] == r for x in st["rounds"]):
            continue
        parent = st["incumbent"]
        cids = [c for c in st["attempts"] if c.startswith(f"{arm}-r{r}-")]
        want = b["candidates_per_round"][r - 1]
        for cid in cids:  # resume attempts interrupted mid-pipeline
            run_candidate(man, cid)
        while len(cids) < want and len(st["attempts"]) < b["max_attempts_per_arm"]:
            k = len(st["attempts"]) + 1
            cid = f"{arm}-r{r}-a{k}"
            left = b["max_attempts_per_arm"] - len(st["attempts"])
            # synthesis outcomes become visible (B2 only) from the round after they complete
            synth_visible = [c for x in st["rounds"] for c in x["candidates"]]
            if arm in LLM_ARMS and not live:
                out = ws / "dryrun" / f"{cid}.prompt.txt"
                info = dry_run_prompt(man, arm, parent, st["attempts"], synth_visible, left, out)
                log(f"[dry-run] wrote {out} ({info['chars']} chars; provider {info['provider']}); "
                    "no call made, no attempt consumed. Re-run with --live to spend budget.")
                return st
            prior = [load_cand(ws, c)["changed_region"]["candidate_body"] for c in st["attempts"]]
            prior = [p for p in prior if p]
            if arm == "T0":
                prop = template_body(man, parent, prior)
            else:
                prop = llm_proposal(man, arm, cid, parent, st["attempts"], synth_visible, left)
            create_candidate(man, arm, cid, parent, prop["region_id"], prop["body"], k, prop, prior)
            st["attempts"].append(cid)
            write_json(state_path(ws, arm), st)  # the attempt is spent from here on
            cids.append(cid)
            c = run_candidate(man, cid)
            log(f"{cid}: {c['decision']} -- {c['reason']}")
        kept = [load_cand(ws, c) for c in cids if load_cand(ws, c)["decision"] == "KEEP"]
        best = min(kept, key=lambda c: c["metrics"]["area"]) if kept else None
        st["rounds"].append({"round": r, "parent": parent, "candidates": cids,
                             "incumbent_after": best["version_id"] if best else parent,
                             "promoted": best["candidate_id"] if best else None, "closed_at": now_iso()})
        if best:
            st["incumbent"] = best["version_id"]
        write_json(state_path(ws, arm), st)
        if len(st["attempts"]) >= b["max_attempts_per_arm"]:
            break
    st["finished_at"] = now_iso()
    write_json(state_path(ws, arm), st)
    return st


def run_negative_controls(man, log=print):
    """Each negative control must be rejected by the TB contract; otherwise the
    regression's effectiveness claim is suspended and the coverage gap recorded."""
    ws = ws_dir(man["design_id"])
    _require_baseline(ws)
    out = []
    for i, n in enumerate(man["negative_controls"], 1):
        cid = n["id"]
        if not (ws / "candidates" / cid).exists():
            create_candidate(man, "NEG", cid, man["baseline_rtl_hash"], n["region"], n["body"], i,
                             {"intended_effect": n["intent"]})
        c = test_candidate(man, cid)
        detected = c["original_tb_status"] == "TB_FAILED"
        out.append({"id": cid, "intent": n["intent"], "compile_status": c["compile_status"],
                    "original_tb_status": c["original_tb_status"], "detected": detected,
                    "evidence": c.get("tb_evidence"),
                    "note": None if detected else "NOT DETECTED: regression-effectiveness claim suspended"})
        log(f"{cid}: TB {c['original_tb_status']} -> {'detected' if detected else 'NOT DETECTED'}")
    write_json(ws / "negative_controls.json", {"checked_at": now_iso(), "controls": out})
    return out
