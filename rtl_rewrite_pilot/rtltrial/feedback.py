"""B2 feedback package: a filtered summary of one *completed* synthesis run.

Only data derived from that run is included (metrics, localized structure, timing
labelled with its trust level, warnings). No advice text.
"""
from pathlib import Path

from .util import now_iso, read_json, sha256_json, write_json

TOP_CELL_TYPES = 12
MAX_STRUCT = 16
MAX_PATHS = 5


def build_feedback(ws, man, vid, sr, run_dir):
    m = sr["metrics"]
    by_type = sorted(m["cells_by_type"].items(), key=lambda kv: -kv[1])
    fb = {
        "schema_version": "rtltrial.feedback.v1",
        "version_id": vid,
        "source_run": str(Path(run_dir).relative_to(ws)),
        "synth_job_key": sr.get("job_key"),
        "created_at": now_iso(),
        "stage": "post-synthesis (technology-mapped netlist); says nothing about placement, wire length or congestion",
        "metrics": {
            "area": m["area"], "area_unit": m["area_unit"], "cells": m["cells"],
            "top_cell_types": dict(by_type[:TOP_CELL_TYPES]),
            "register_bits": m["coarse_ff_bits"], "latch_bits": m["coarse_latch_bits"],
            "logic_depth": m["logic_depth"], "logic_depth_definition": m["logic_depth_definition"],
            "netlist_fingerprint": m["fingerprint"],
        },
        "coarse_structure": sr["structure"][:MAX_STRUCT],
        "coarse_structure_basis": "Yosys coarse netlist after `synth -run begin:fine` (alumacc/opt done); src attributes may merge several origins",
        "critical_paths": sr["critical_paths"][:MAX_PATHS],
        "timing": sr["timing"],
        "warnings": sr["warnings"][:20],
    }
    fb["feedback_hash"] = sha256_json({k: v for k, v in fb.items() if k != "created_at"})
    write_json(Path(ws) / "feedback" / f"{vid}.json", fb)
    return fb


def load_feedback(ws, vid):
    return read_json(Path(ws) / "feedback" / f"{vid}.json")


def render(fb, max_chars):
    """Compact text for the proposer prompt. Sections are dropped from the end when
    over the budget; the truncation is stated in the text and returned."""
    t = fb["timing"]
    sections = []
    m = fb["metrics"]
    sections.append("\n".join([
        f"version {fb['version_id'][:12]} -- {fb['stage']}",
        f"area {m['area']:.4f} ({m['area_unit']}); cells {m['cells']}; register bits {m['register_bits']}; "
        f"latch bits {m['latch_bits']}; logic depth {m['logic_depth']} ({m['logic_depth_definition']})",
        "top cell types: " + ", ".join(f"{k.replace('sky130_fd_sc_hd__', '')}x{v}" for k, v in m["top_cell_types"].items()),
    ]))
    rows = [f"  {s['type']} width={s['y_width']} src={','.join(s['src']) or '-'} confidence={s['confidence']}"
            for s in fb["coarse_structure"]]
    sections.append("coarse arithmetic/selection cells (" + fb["coarse_structure_basis"] + "):\n" + "\n".join(rows))
    prow = []
    for p in fb["critical_paths"]:
        st, en, loc = p["startpoint"], p["endpoint"], p["endpoint_cone_src"]
        prow.append(f"  slack {p['slack_ns']} ns, {p['comb_cells_on_path']} comb cells: "
                    f"{st.get('signal')}[{st.get('bit', '')}] -> {en.get('signal')}[{en.get('bit', '')}]; "
                    f"endpoint cone src={','.join(loc['src']) or '-'} (confidence {loc['confidence']}: {loc['basis']})")
    sections.append(f"mapped STA ({t['status']}; trusted={t['trusted']}; {t['wire_model']}; "
                    f"worst slack {t.get('worst_slack_ns')} ns, TNS {t.get('tns_ns')} ns; "
                    f"constraint gaps: {'; '.join(t['constraint_gaps'])}):\n" + "\n".join(prow))
    if fb["warnings"]:
        sections.append("synthesis warnings:\n" + "\n".join(f"  {w}" for w in fb["warnings"]))
    out, dropped = [], 0
    for s in sections:
        if sum(len(x) + 2 for x in out) + len(s) > max_chars:
            dropped += 1
            continue
        out.append(s)
    if dropped:
        out.append(f"[{dropped} feedback section(s) omitted to fit the per-call input limit]")
    return "\n\n".join(out), dropped
