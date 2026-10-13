# rtl_rewrite_pilot (historical pilot + shared helpers)

The optimization loop now lives in `../spec2gds` (`python3 -m s2g loop-init|loop-run`, see
`../docs/S2G_Upgrade_Plan_v1_EN.md`). This directory keeps only what is still imported or documents frozen evidence:

- `rtltrial/{util,liberty,netlist}.py`: helpers used by `rtltrial/search`, the remaining experiment runners and the DEV screens.
- `rtltrial/search/`: structural candidate search; `spec2gds` freezes `search/rules.py` as its typed-rule library.
- `experiments/{arithmetic_stability,backend_screen,mux9_flash_nothinking,mux_stability}/`: runners of closed experiments.
  `arithmetic_stability/guard.py` is also frozen by `spec2gds` as `typed_guard`.
- `dev/`: DEV screen entry scripts.

Removed on 2026-10-12 (recoverable from git commit 9e3a442): the sky130hd `rtltrial` v1 CLI (`cli`, `arms`, `evaluate`,
`propose`, `summarize`, `selftest`, `feedback`, `manifest`, `region`, `synth`, `tb`, `llm`), its `flow/` templates,
`pilot_config.json` and `designs/`; and the superseded `experiments/mux9_feedback` and `experiments/mux9_flash_feedback` runners.
Their exact runner code is frozen in `work/mux9_b1_b2_v1/code_snapshot/` and `work/mux9_flash_b1_b2_v1/code_snapshot/`;
their READMEs moved to `../data/experiments/`. The model transport now lives in `spec2gds/s2g/provider.py`.

Tests: `python3 -m unittest tests.test_structure_search experiments.backend_screen.test_analyze` from this directory; the
other experiments run `python3 -m unittest discover -p 'test_*.py'` inside their own directory.
Controller unit tests use the versioned example and synthetic evidence. Tests that validate historical arithmetic/mux
archives require the excluded local `work/` or DEV artifacts and report an explicit skip when those archives are absent.

## Historical v1 result: 2026-10-10 (design `multi_pipe_8bit`, RTLLM @51ed553, sky130hd)

- Baseline: `TB_PASSED` (100 valid outputs observed by the passive adapter, error=0, $finish at 72220); area 4367.94, 400 cells,
  depth 12, 116 register bits; 3/3 repeat syntheses identical.
- `selftest` 10/10. `negctl`: `NEG-drop-operand` → `TB_FAILED` (93/100). Its unsynthesized area would be 3494.6 (−20 %).
- `T0`: 3/3 candidates (balanced and two other bracketings) `COLLAPSED`. The mapped netlist is **byte-identical** to the baseline,
  because Yosys `alumacc` folds every bracketing of the 4-term same-width sum into one `$macc_v2`. Under this flow the
  `add_regroup_same_width` class on this design hits the stop condition "synthesis repeatedly erases candidate changes".
  B1/B2 were never run live with that CLI. Evidence: `work/multi_pipe_8bit/`.

## Additional DEV screen: 2026-10-11

[VerilogEval mux9](dev/verilogeval_mux9_v1/README.md) was downloaded and tested with
its unchanged upstream self-checking TB using a locally built Icarus v12.
Two exact-selector mux groupings both pass 220 original TB samples, 2048 extension
samples and combinational binary equivalence, and both survive synthesis as
distinct netlists. Area changes are +42.10% and -0.1613%; neither meets the frozen
0.5% area-improvement rule. The original is retained. The negative control is
detected by both TB and equivalence. Full results and the earlier blocked
verification-harness run are preserved in the linked DEV directory. No model
arms were run during that deterministic screen; mux support was never integrated into the removed v1 CLI.


## Flash model comparison: 2026-10-11

The separate [DeepSeek-V4.1-Flash B1/B2 experiment](../data/experiments/mux9_flash_feedback/README.md)
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
