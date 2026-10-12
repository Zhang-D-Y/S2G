"""Proposers. All arms share one candidate space (the frozen transform on the declared
region) and one budget; they differ only in what the proposer is shown.

  T0  deterministic template enumeration (flow validation; no model)
  B1  model; RTL + contract + TB information + earlier attempts' TB outcomes
  B2  B1 inputs + synthesis feedback of the current version and earlier candidates
  NEG negative control from the manifest (outside the budget; must be rejected by the TB)

Prompts carry only the task, the protocol/contract and data from this run.
"""
from pathlib import Path

from . import region as R
from .evaluate import cand_dir, load_cand, load_version
from .feedback import load_feedback, render
from .llm import chat, extract_json, provider_identity
from .manifest import ws_dir
from .util import sha256_text, write_json

LLM_ARMS = ("B1", "B2")
SYSTEM = ("You propose one candidate edit in a controlled RTL rewriting experiment. "
          "Reply with a single JSON object and nothing else.")


def _region(man):
    if len(man["regions"]) != 1:
        raise SystemExit("v1 arms expect exactly one declared region per design")
    return man["regions"][0]


def template_body(man, parent_vid, prior_bodies):
    """Next unused full bracketing of the parent's operands, shallowest tree first."""
    ws = ws_dir(man["design_id"])
    reg = _region(man)
    pbody = R.body((ws / load_version(ws, parent_vid)["rtl_dir"] / reg["file"]).read_text(), reg)
    seen = {R.canonical(pbody)} | {R.canonical(b) for b in prior_bodies}

    def depth(t):
        return 0 if isinstance(t, str) else 1 + max(depth(t[0]), depth(t[1]))

    options = sorted((x[1:-1] for x in R.bracketings(R.operands(pbody))),
                     key=lambda e: (depth(R.parse_sum(e)), R.canonical(e)))
    for e in options:
        if R.canonical(e) not in seen:
            return {"region_id": reg["id"], "body": e, "intended_effect": "template enumeration (flow check)"}
    return {"region_id": reg["id"], "body": None, "error": "template space exhausted"}


def _attempt_lines(ws, cids, arm, synth_visible):
    lines = []
    for cid in cids:
        c = load_cand(ws, cid)
        s = (f"- {cid}: region `{c['changed_region']['candidate_body']}` -> compile {c['compile_status']}, "
             f"original TB {c['original_tb_status']}")
        if c.get("tb_evidence") and c["tb_evidence"].get("fail_lines"):
            s += f"; TB failure lines: {c['tb_evidence']['fail_lines'][:3]}"
        if c["state"] == "FAILED" and c["compile_status"] is None:
            s += f"; rejected before tools: {c['reason']}"
        if arm == "B2" and cid in synth_visible and c.get("metrics"):
            d = c["delta"]["vs_parent"] if c.get("delta") else None
            s += (f"; synthesis: area {c['metrics']['area']:.4f}" +
                  (f" ({d['area_rel']:+.3%} vs parent), netlist identical to parent: {d['same_fingerprint']}, "
                   f"logic depth {c['metrics']['logic_depth']}; decision {c['decision']}" if d else ""))
        lines.append(s)
    return lines


def build_prompt(man, arm, parent_vid, prior_cids, synth_visible, attempts_left):
    ws = ws_dir(man["design_id"])
    reg = _region(man)
    ver = load_version(ws, parent_vid)
    rtl_text = (ws / ver["rtl_dir"] / reg["file"]).read_text()
    tb_text = "\n\n".join((ws / "baseline" / "tb" / f).read_text() for f in man["tb"]["files"])
    rule = man["acceptance_rule"]
    pc = man["pass_contract"]
    parts = [
        "# Task",
        f"Propose a replacement for region {reg['id']} of `{reg['file']}` in the current version below. "
        f"The region is the {reg['description']}. "
        "Everything outside the region must stay byte-identical; the evaluator enforces this.",
        f"Objective: lower post-synthesis cell area (frozen Yosys flow, {man['synthesis']['target']}) by at least "
        f"{rule['min_rel_area_improvement']:.2%} relative to the current version, while satisfying the contract.",
        f"\n# Allowed transformation: {man['allowed_transform']['id']}",
        man["allowed_transform"]["definition"],
        f"Region preconditions (run manifest): {reg.get('preconditions')}",
        "\n# Fixed contract (enforced by the evaluator)",
        "- Ports, clock/reset behavior, protocol, throughput, pipeline depth, cycle latency, parameter contracts "
        "and the legal input domain are unchanged.",
        "- No directives or attributes (ifdef, translate_off/on, keep, dont_touch, blackbox, full_case, "
        "parallel_case), no x/z or don't-care values, no comments.",
        "- The frozen original testbench must pass. Testbench, synthesis script and constraints cannot be changed.",
        "\n# Evaluation protocol",
        f"Each proposal consumes one attempt; {attempts_left} attempt(s) remain in this arm. Malformed, "
        "out-of-scope, duplicate, failing and no-gain proposals all consume an attempt. "
        f"Order: scope/transform check -> lint -> compile + original TB -> synthesis -> decision against the "
        "current version.",
        f"\n# Current version {parent_vid[:12]} ({ver['origin']})",
        f"```verilog\n{rtl_text}\n```",
        f"Current region text: `{R.body(rtl_text, reg).strip()}`",
        "\n# Testbench (frozen)",
        f"Top `{man['tb']['top']}`; PASS requires the pass marker /{pc['pass_regex']}/, normal $finish, no failure "
        f"marker, and adapter evidence {pc['adapter_requirements']}.",
        f"```verilog\n{tb_text}\n```",
    ]
    hist = _attempt_lines(ws, prior_cids, arm, set(synth_visible))
    parts += ["\n# Earlier attempts in this arm", *(hist or ["(none)"])]
    tail = ["\n# Output format",
            '{"region_text": "<replacement for the region, without the trailing terminator>", '
            '"intended_effect": "<one sentence>"}']
    base = "\n".join(parts + tail)
    fb_hash = None
    dropped = 0
    if arm == "B2":
        fb = load_feedback(ws, parent_vid)
        fb_hash = fb["feedback_hash"]
        header = "\n# Synthesis feedback for the current version (from its completed run)\n"
        room = man["llm"]["max_input_chars"] - len(base) - len(SYSTEM) - len(header)
        text, dropped = render(fb, max(room, 0))
        user = "\n".join(parts) + header + text + "\n" + "\n".join(tail)
    else:
        user = base
    if len(user) + len(SYSTEM) > man["llm"]["max_input_chars"]:
        raise SystemExit(f"prompt exceeds frozen per-call input limit ({len(user)} chars)")
    return user, fb_hash, dropped


def llm_proposal(man, arm, cid, parent_vid, prior_cids, synth_visible, attempts_left):
    user, fb_hash, dropped = build_prompt(man, arm, parent_vid, prior_cids, synth_visible, attempts_left)
    d = cand_dir(ws_dir(man["design_id"]), cid) / "proposal"
    d.mkdir(parents=True, exist_ok=True)
    (d / "prompt.txt").write_text(f"[system]\n{SYSTEM}\n\n[user]\n{user}")
    resp = chat(man["llm"], SYSTEM, user)
    write_json(d / "response.json", resp)
    out = {"prompt_hash": sha256_text(SYSTEM + "\n" + user), "input_feedback_hash": fb_hash,
           "feedback_sections_dropped": dropped, "model_version": resp["model_version"],
           "token_usage": resp["token_usage"], "elapsed_s": resp["elapsed_s"],
           "log": str(d.relative_to(ws_dir(man["design_id"]))), "region_id": _region(man)["id"], "body": None}
    if resp["error"]:
        out["error"] = f"provider: {resp['error']}"
        return out
    obj = extract_json(resp["content"])
    if not isinstance(obj, dict) or not isinstance(obj.get("region_text"), str):
        out["error"] = f"unparseable reply (finish_reason={resp.get('finish_reason')})"
        return out
    out["body"] = obj["region_text"].strip().rstrip(";").strip()
    out["intended_effect"] = obj.get("intended_effect")
    return out


def dry_run_prompt(man, arm, parent_vid, prior_cids, synth_visible, attempts_left, out_path):
    user, fb_hash, dropped = build_prompt(man, arm, parent_vid, prior_cids, synth_visible, attempts_left)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(f"[system]\n{SYSTEM}\n\n[user]\n{user}")
    return {"chars": len(user) + len(SYSTEM), "feedback_hash": fb_hash, "feedback_sections_dropped": dropped,
            "provider": provider_identity(man["llm"])}
