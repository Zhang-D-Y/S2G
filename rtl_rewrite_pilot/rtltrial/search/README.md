# Structural candidate search

A separate, stdlib-only search entry point over the **existing arithmetic
verification flow**. Historical runners, contracts and results remain unchanged.
Supported designs: RTLLM `mul8` and `cla32`, with their existing local rewrite
regions and unchanged declarations. This is a DEV implementation, not a measured
LLM feedback-improvement result.

## What is implemented

- A bounded **success pool** and **repair queue**, inspired by REvolution. Verified
  candidates that do not improve area can remain `EXPLORE` parents. Only `KEEP`
  candidates meeting the incumbent's >=0.5% area-improvement threshold become the
  final best version. Parent selection alternates best-first and least-expanded
  alternatives. Pool size defaults to four; the repair queue keeps two failures.
  Repair proposals use a verified parent plus failed-proposal previews and
  functional statuses. This is not a reproduction of REvolution's adaptive UCB
  strategy selection or crossover.
- Explicit, width-aware rewrite operations inspired by ROVER: `sum_tree` chooses
  a balanced/left/right tree and a permutation of all eight partial products;
  `rotate` modifies an existing tree; `carry_factor` chooses factoring groups for
  the four original one-bit XOR carry equations. Each action has a strict schema
  and deterministic renderer. Operand loss/duplication and arbitrary source
  injection are rejected. This is a small tree IR and rule library, not e-graph
  equality saturation, and the rules do not certify four-state equivalence.
- Same-region `freeform` generation as a control. It uses the same pool controller,
  evaluator, model settings and attempt limits as `structured` generation. Prepare
  separate runs to form freeform/structured × B1/B2 comparisons. The old historical
  single-incumbent runs are not directly interchangeable with this control.
- Token-normalized source deduplication before tools; mapped fingerprint plus
  cell-distribution comparison after synthesis. The latter is a conservative
  search heuristic, not a proof of identical netlists. Each evaluated RTL still
  needs its own equivalence evidence.
- B1 receives source, original TB, bounded functional history and repair previews.
  B2 additionally receives completed-run metrics, cell-type changes relative to
  the parent, logic depth, source-localization clues and collapse information.
  Feedback binds the actual RTL and synthesis result hashes. Both controllers
  use area internally; parent selection conveys limited indirect feedback.
- Every `next` reserves a slot before returning a request. Invalid responses,
  external call failures, duplicates, collapse and interrupted attempts all consume
  slots. A pending request blocks another reservation in that arm; `abort` closes
  it without a retry. Per-run CLI locking prevents concurrent state mutations.

`adapter.py` loads the existing arithmetic evaluator under private module names;
`engine.py` calls its lint, simulation, formal, synthesis and mapped-proof stages.
No production RTL is overwritten. Fresh input snapshots and code hashes are
frozen for each run, and baseline jobs are rerun (including three syntheses and
negative controls). Existing source provenance is checked before copying inputs.

## Verification and acceptance

Original TB with positive execution evidence → frozen X/Z extension → binary
whole-design SAT equivalence → complete clean sky130hd mapping → warning/state/
undefined-pin checks. Area drops >10% relative to either baseline or parent
require mapped-cell equivalence before entering the success pool. No new
warnings are allowed relative to the frozen baseline. `finalize` reruns both
arms' selected RTL through functional checks; changed winners additionally need
mapped proof and two fresh syntheses matching the candidate's netlist bytes.

The four-state extension is finite and does not prove all X/Z inputs. The formal
proof covers all binary inputs of these combinational designs. Area is the only
objective; depth is diagnostic and timing/P&R/power are not assessed.

## Commands

Run from `rtl_rewrite_pilot/`:

```bash
# Full deterministic DEV loop, six attempts per arm; fresh output directories.
python3 -m rtltrial.search demo --design mul8 --root dev/search_mul8_new
python3 -m rtltrial.search demo --design cla32 --root dev/search_cla32_new

# Prepare a new matched B1/B2 session (does local validation; no model call).
python3 -m rtltrial.search prepare --design mul8 --generation structured \
  --attempts 12 --pool-size 4 --root work/structure_search_new

# Reserve exactly one proposal slot and write a provider-ready request.json.
python3 -m rtltrial.search next --root work/structure_search_new --arm B1

# Submit either the proposal JSON or a complete chat-completions response.
python3 -m rtltrial.search submit --root work/structure_search_new --arm B1 \
  --response /path/to/response.json

# Account for an external call failure; it consumes the reserved slot.
python3 -m rtltrial.search submit --root work/structure_search_new --arm B1 \
  --error 'provider request failed'

# Close an interrupted reservation without retrying it.
python3 -m rtltrial.search abort --root work/structure_search_new --arm B1

python3 -m rtltrial.search summarize --root work/structure_search_new
# Requires both arms to complete the frozen attempt budget.
python3 -m rtltrial.search finalize --root work/structure_search_new

python3 -m unittest discover -s tests -p test_structure_search.py -v
```

Example structured response for mul8:

```json
{"op":"sum_tree","params":{"shape":"balanced","order":[0,1,2,3,4,5,6,7]}}
```

Example for cla32:

```json
{"op":"carry_factor","params":{"groups":[1,2,2,2]}}
```

This entry point **does not contact a model provider**. It creates requests with
`deepseek-flash`, `thinking=disabled`, temperature 1.0 and max output 8192 tokens;
input is bounded at 24000 UTF-8 bytes. It can ingest provider responses with usage
metadata, but external billing is not verified. An automated paid transport and
new experiment-level monetary budget are outside this implementation. Do not run
an old `experiment.py live` command against these new run directories.

`next` is a reservation, not a free preview. Every request/response, parent hash,
patch hash, rendered RTL, validation result, feedback reference and rejection is
retained. `summary.json`/`attempts.csv` cover all reserved attempts; `final.json`
records final validation. Baseline, candidate and final tool costs are included.
Old arithmetic input snapshots under `work/arithmetic_stability_v1/` and their
recorded tool/library paths must be available; this adapter is intentionally
scoped to the current workspace rather than a general benchmark importer.

## Research provenance and interpretation

- [REvolution official implementation](https://github.com/kmcho2019/REvolution):
  candidate populations, success/failure separation and explicit mutation choices.
- [ROVER paper](https://arxiv.org/html/2406.12421v1): local equivalent structural
  rewrites, width/sign conditions, retention of alternative implementations and
  verification. This implementation reuses ideas, not their source code.

The mul8 balanced tree reproduces a previously known DEV witness. Both arms get
identical deterministic proposal sequences in `demo`; any improvement belongs to
that rule library, not an LLM or the presence of feedback. An actual feedback study
must freeze matched per-arm budgets, repeat runs, and evaluate additional designs
that were not used to develop these rules. Keep the old results separate.

## Validated local results

[Integration results and evidence](RESULTS.md): 11 control tests plus all three
historical arithmetic regression tests pass. Deterministic mul8 search reproduces
19.8603% mapped area improvement with final RTL/mapped equivalence and repeated
synthesis; CLA32 retains baseline while preserving exploration alternatives.
Current-code CLA32 structured and freeform regression runs also pass.

Historical `summarize` verifies frozen snapshots/records and marks
`runner_matches_current`; it may regenerate reports after an implementation
update. Advancing a run still requires its exact frozen implementation.
