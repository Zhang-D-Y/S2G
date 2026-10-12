"""rtltrial command line.

  validate   dry-run configuration / manifest validation (no simulation, no synthesis)
  baseline   import read-only snapshot, freeze manifest, original TB, repeat synthesis
  selftest   TB adapter/parser on success, failure, missing-evidence and timeout cases
  negctl     run the negative control(s); must be rejected by the TB contract
  propose    preview the next proposal of an arm (template body or LLM prompt); spends nothing
  run-arm    budgeted propose -> test -> synth -> compare loop for one arm
  test | synth | compare   advance one candidate through a single stage (idempotent)
  summarize  results/summary.csv and per-design JSON with complete costs
"""
import argparse
import json
import sys

from . import arms, evaluate, selftest
from .manifest import freeze, import_baseline, load_design, load_manifest, load_pilot, manifest_path, validate, ws_dir
from .propose import LLM_ARMS, dry_run_prompt, template_body
from .summarize import summarize_all
from .util import write_json


def cmd_validate(a):
    problems, notes = validate(a.design, a.source_root)
    for n in notes:
        print("note:", n)
    for p in problems:
        print("PROBLEM:", p)
    print("VALID" if not problems else f"INVALID ({len(problems)} problems)")
    return 1 if problems else 0


def _checked_manifest(design_id):
    problems, _ = validate(design_id)
    if problems:
        sys.exit("manifest validation failed:\n  " + "\n  ".join(problems))
    return load_manifest(design_id)


def cmd_baseline(a):
    design, pilot = load_design(a.design), load_pilot()
    ws = ws_dir(a.design)
    if not (ws / "baseline").exists():
        if not a.source_root:
            sys.exit("first baseline needs --source-root (checkout of the design's source repo)")
        import_baseline(design, a.source_root)
    mp = manifest_path(a.design)
    if not mp.exists():
        write_json(mp, freeze(design, pilot))
    rec = evaluate.establish_baseline(_checked_manifest(a.design))
    print(json.dumps({k: rec.get(k) for k in ("status", "blocking_reason", "version_id")}, indent=2))
    if rec["status"] == "BASELINE_OK":
        m = rec["synth"]["metrics"]
        print(f"TB: {rec['tb']['tb_status']} adapter={rec['tb']['evidence']['adapter']} "
              f"sim_time={rec['tb']['evidence']['sim_time']}")
        print(f"synth: area={m['area']} cells={m['cells']} depth={m['logic_depth']} ff_bits={m['coarse_ff_bits']} "
              f"repeat variation={rec['repeatability']['rel_area_variation']}")
    return 0 if rec["status"] == "BASELINE_OK" else 2


def cmd_selftest(a):
    pilot = load_pilot()
    bad = 0
    for name, want, got in selftest.run(pilot["tools"]["iverilog_bin"], pilot["tools"]["vvp_bin"]):
        ok = want == got
        bad += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {name}: expected {want}, got {got}")
    return 1 if bad else 0


def cmd_negctl(a):
    res = arms.run_negative_controls(_checked_manifest(a.design))
    return 0 if all(r["detected"] for r in res) else 3


def cmd_propose(a):
    man = _checked_manifest(a.design)
    st = arms.load_state(man, a.arm)
    left = man["budget"]["max_attempts_per_arm"] - len(st["attempts"])
    if a.arm in LLM_ARMS:
        out = ws_dir(a.design) / "dryrun" / f"{a.arm}-preview.prompt.txt"
        visible = [c for r in st["rounds"] for c in r["candidates"]]
        info = dry_run_prompt(man, a.arm, st["incumbent"], st["attempts"], visible, left, out)
        print(f"prompt preview: {out}\n{json.dumps(info, indent=2)}")
    else:
        prior = [evaluate.load_cand(ws_dir(a.design), c)["changed_region"]["candidate_body"] for c in st["attempts"]]
        print(json.dumps(template_body(man, st["incumbent"], [p for p in prior if p]), indent=2))
    return 0


def cmd_run_arm(a):
    man = _checked_manifest(a.design)
    st = arms.run_arm(man, a.arm, live=a.live)
    print(f"arm {a.arm}: attempts={len(st['attempts'])} incumbent={st['incumbent'][:12]} rounds={len(st['rounds'])}")
    return 0


def cmd_stage(a):
    man = _checked_manifest(a.design)
    fn = {"test": evaluate.test_candidate, "synth": evaluate.synth_candidate, "compare": evaluate.decide}[a.cmd]
    rec = fn(man, a.cand)
    print(json.dumps({k: rec.get(k) for k in ("candidate_id", "state", "compile_status", "original_tb_status",
                                               "synth_status", "decision", "reason")}, indent=2))
    return 0


def cmd_summarize(a):
    rows, out = summarize_all()
    for r in rows:
        print(f"{r['design_id']:<18} {r['arm']:<3} attempts={r['attempts']} keep={r['keep']} collapsed={r['collapsed']} "
              f"reject={r['reject']} dup={r['duplicate']} final_vs_baseline={r['final_vs_baseline_rel']} "
              f"tools={r['tool_invocations']}/{r['tool_wall_s']}s tokens={r['tokens_in']}+{r['tokens_out']}")
    print(f"written: {out}/summary.csv")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="rtltrial", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, design=True, **kw):
        p = sub.add_parser(name, **kw)
        if design:
            p.add_argument("--design", required=True)
        p.set_defaults(fn=fn)
        return p

    add("validate", cmd_validate).add_argument("--source-root")
    add("baseline", cmd_baseline).add_argument("--source-root")
    add("selftest", cmd_selftest, design=False)
    add("negctl", cmd_negctl)
    add("propose", cmd_propose).add_argument("--arm", required=True, choices=arms.ARMS)
    p = add("run-arm", cmd_run_arm)
    p.add_argument("--arm", required=True, choices=arms.ARMS)
    p.add_argument("--live", action="store_true", help="allow paid model calls (B1/B2); default is dry-run")
    for st in ("test", "synth", "compare"):
        add(st, cmd_stage).add_argument("--cand", required=True)
    add("summarize", cmd_summarize, design=False)
    a = ap.parse_args(argv)
    sys.exit(a.fn(a))
