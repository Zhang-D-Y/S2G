# rtl_rewrite_pilot (rtltrial v1)

Implementation of `docs/RTL Rewriting Verification Scheme Based on Test Platform and Comprehensive Feedback_v1.md`:
original baseline → synthesis feedback → constrained rewrite → original-TB regression → synthesis comparison → keep or roll back.
This project is independent of the TEHM/R2G, Table2 and SOURCE experiments. The v1 CLI has no P&R feedback, no training and no formal checks
(`formal_status` is always `NOT_RUN`). The separate [VerilogEval mux DEV screen](dev/verilogeval_mux9_v1/README.md)
adds combinational equivalence for its own fixed candidates; it does not change the CLI's frozen experiments.

## Commands

Run from this directory: `python3 -m rtltrial <cmd>`. Only the standard library is used; works with Python ≥ 3.10.

| command | what it does |
|---|---|
| `validate --design D [--source-root R]` | Dry-run check: tools, Liberty hash, source files and, once frozen, every manifest hash (RTL/TB snapshot, tool versions, scripts, SDC, dont-use, adapter, rules). |
| `baseline --design D --source-root R` | Imports a read-only snapshot (checking the source commit), freezes `config/run_manifest.json`, then runs lint, the original TB and N repeat syntheses. Ends `BASELINE_OK`, or `BLOCKED` with a reason. |
| `selftest` | PASS-contract parser on pass / fail / missing-evidence / timeout logs, plus one real simulation that times out. |
| `negctl --design D` | Negative control(s) from the design spec. They must end `TB_FAILED`; if not, the regression-effectiveness claim is suspended. |
| `propose --design D --arm A` | Previews the next proposal (template body, or LLM prompt written to `dryrun/`). Spends nothing. |
| `run-arm --design D --arm T0\|B1\|B2 [--live]` | Budgeted loop. B1/B2 make **no model call without `--live`**: they write the prompt to `dryrun/` and stop. |
| `test\|synth\|compare --design D --cand ID` | Advance one candidate by one stage (idempotent). |
| `summarize` | `results/summary.csv` + `results/<design>.json` (all attempts, failures, costs). |

For `--live`, first `source ~/.llm_env`. The client reads `LLM_API_KEY / LLM_MODEL / LLM_BASE_URL` (OpenAI-compatible) and records
the HTTP status and error text of every call. A provider failure consumes the attempt.

## Arms (same candidate space, budget, evaluator)

- `T0`: deterministic enumeration of the allowed transform. Use it to check the flow; no model.
- `B1`: model proposer. Sees RTL, contract, transform definition, the TB, and earlier attempts' scope/compile/TB outcomes. It never
  sees synthesis numbers or decision labels. Disclosed leak: the controller's choice of incumbent conveys selection information.
- `B2`: everything B1 sees, plus `feedback/<version>.json` for the current version. From the next round on it also sees the
  synthesis outcomes of earlier candidates. The feedback is trimmed to fit the same per-call input limit.
- `NEG`: negative control, outside the budget.

Prompts carry only the task, the contract/protocol and data from this run. They contain no rewriting advice.

## Layout (per design under `work/<design_id>/`)

```
baseline/{rtl,tb}/, provenance.json   read-only snapshot + source commit/hashes
config/run_manifest.json              frozen contract (contract_hash)
versions/<hash>.json                  RTL versions with metrics (incumbents are hash refs)
candidates/<id>/                      rtl/, patch.diff, proposal/, candidate_record.json
runs/<id>-{lint,tb,synth}/            every log, script, netlist, STA report
feedback/<hash>.json                  B2 feedback package of a completed synthesis
state/<arm>.json                      attempts, rounds, incumbent
negative_controls.json, baseline_record.json
```

## Frozen flow (`flow/`)

The flow is Yosys `synth -flatten` (the coarse netlist with `src` attributes is dumped before `fine`), then `dfflibmap` +
`abc -D 10000` on sky130hd `tt_025C_1v80`, using the ORFS dont-use list. Metrics: Liberty area, cell types, coarse register and latch
bits, logic depth (comb std cells between timing start- and endpoints, computed in `netlist.py`), a name-insensitive WL fingerprint of
the mapped netlist, and OpenSTA under a **pilot-authored SDC**. Timing is labelled `REPORTED_UNTRUSTED` and does not enter decisions.
Critical-path endpoints are mapped to RTL lines in three steps: register Q name → coarse register → D-cone `src` attributes, with a
confidence level.

## Status 2026-10-10 (design `multi_pipe_8bit`, RTLLM @51ed553)

- Baseline: `TB_PASSED` (100 valid outputs observed by the passive adapter, error=0, $finish at 72220); area 4367.94, 400 cells,
  depth 12, 116 register bits; 3/3 repeat syntheses identical.
- `selftest` 10/10. `negctl`: `NEG-drop-operand` → `TB_FAILED` (93/100). Its unsynthesized area would be 3494.6 (−20 %).
- `T0`: 3/3 candidates (balanced and two other bracketings) `COLLAPSED`. The mapped netlist is **byte-identical** to the baseline,
  because Yosys `alumacc` folds every bracketing of the 4-term same-width sum into one `$macc_v2`. Under this flow the
  `add_regroup_same_width` class on this design hits the stop condition "synthesis repeatedly erases candidate changes".
  B1/B2 have **not** been run live.

## Additional DEV screen: 2026-10-11

[VerilogEval mux9](dev/verilogeval_mux9_v1/README.md) was downloaded and tested with
its unchanged upstream self-checking TB using a locally built Icarus v12.
Two exact-selector mux groupings both pass 220 original TB samples, 2048 extension
samples and combinational binary equivalence, and both survive synthesis as
distinct netlists. Area changes are +42.10% and -0.1613%; neither meets the frozen
0.5% area-improvement rule. The original is retained. The negative control is
detected by both TB and equivalence. Full results and the earlier blocked
verification-harness run are preserved in the linked DEV directory. No model
arms were run during that deterministic screen; mux support is not yet integrated into the main CLI.


## Flash model comparison: 2026-10-11

The separate [DeepSeek-V4.1-Flash B1/B2 experiment](experiments/mux9_flash_feedback/README.md)
uses two calls per arm over two rounds, with identical verification and synthesis
rules. It follows the user's explicit model correction; two earlier Pro submissions
are archived separately and count against the six-call total authorization.
See that experiment for the final status, all attempts, costs and claim limits.


## Non-thinking Flash comparison: 2026-10-11

The [new non-thinking B1/B2 run](experiments/mux9_flash_nothinking/README.md)
returned complete RTL in all four calls. B1 retained baseline area 775.744;
B2 retained area 754.4736 (-2.7419%), with 96 versus 110 cells. Original and
extended TB, binary RTL equivalence, final regressions and supplemental mapped
netlist equivalence all pass; the winner's mapped netlist is byte-identical
across three syntheses. This is one DEV observation, not causal evidence that
feedback improves designs generally. No timing or physical-design result.


## Multi-design repeats: 2026-10-11

The [fixed non-thinking configuration was tested on three designs with three repeats](experiments/mux_stability/README.md),
36 calls total. Both new designs retained their baseline in every B1/B2 pair.
On DEV mux9 the results split one B1 win, one B2 win and one tie. Thus the
pre-registered stability gate was not met; the earlier single B2 gain does not
establish stable feedback benefit. All final audits and supplemental checks
passed. This cohort remains limited to small public muxes and synthesis area.


## Complex arithmetic study: 2026-10-11

[Two RTLLM arithmetic designs, three repeats each](experiments/arithmetic_stability/README.md)
were tested with the same non-thinking Flash settings (24 calls). CLA32 and mul8
both retained their originals under B1 and B2 in every repeat. Twelve proposals
were duplicates and four responses lacked a valid candidate. The stability gate
was not met. A separate, undisclosed deterministic multiplier restructuring
proved available area improvement of about 19.86%, with binary RTL/mapped proofs
and exhaustive binary simulation. That witness is not a model-feedback gain.

## Structural candidate pools

The new [structural search entry point](rtltrial/search/README.md) combines bounded
success/repair pools with explicit sum-tree and carry-factoring actions. It reuses
the arithmetic TB, four-state extension, binary equivalence, synthesis and mapped
proof gates. Run `python3 -m rtltrial.search --help`. The deterministic DEV loop
and request/response interface make no provider calls; historical experiments
remain frozen. A `freeform` mode provides the same-controller comparison for a
future matched study.

## Deterministic OpenROAD backend screen

The separate [backend screen](experiments/backend_screen/README.md) routes the
existing mul8 and CLA32 deterministic candidates without any model calls.
[Measured results](dev/backend_screen_v1/REPORT.md) cover eight full route/RCX
runs with binary routed equivalence, frozen physical contracts, pre/post repair
risk and net repair edits. Both candidates improve the measured setup/repair
outcomes; this establishes backend sensitivity on these two examples, not LLM
feedback effectiveness or general stability.
