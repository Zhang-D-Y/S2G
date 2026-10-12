# More complex arithmetic RTL feedback experiment

Frozen 2026-10-11. [Run](../../work/arithmetic_stability_v1/).
Plan SHA256 ff5cc0b3819c14a9eed69d74f4675c238aebc06d63bc703b38496524c0fb64eb.

RTLLM source commit 51ed553d0ffd32797a1a0a13e051656bf302c81f. Two arithmetic
families with non-vacuous original TBs; no scaled synthetic copies:

| Design | Frozen rewrite region | Baseline area | Cells | Depth |
|---|---|---:|---:|---:|
| cla32 | four carry equations in the CLA module instantiated eight times | 1020.9792 | 149 | 22 |
| mul8 | one combinational conditional partial-product accumulation block | 2507.4048 | 332 | 25 |

All ports, declarations, hierarchy and other expressions remain fixed. This is
an explicitly new arithmetic factoring/restructuring class, not the previous
restricted mux-only class. CLA permits existing carry inputs/outputs and only
four continuous assignments. mul8 permits only its existing signals, a single
always block and at most the original fixed eight-iteration loop. No declarations,
attributes/directives, module injection, new state, unbounded loops or X/Z
literals are allowed. Whole-design equivalence separately checks semantics.

## Screening and validation

RTLLM fixed-point adder/subtractor TBs print PASS but use only unknown inputs:
passive monitors observed 0/100 known samples. They are excluded. The ALU was
excluded by inspection for undriven outputs and Z/default semantics. Screening
logs and no-call artifacts are under ../../dev/complex_screen/.

Both selected original TBs have 100/100 known-input samples and zero errors;
TB bytes are preserved. A passive observer records counts and the existing
checker error variable without changing stimulus or verdicts. A module wrapper
connects the immutable upstream TB to TopModule. The reference top/support
modules are uniquely renamed to avoid gold/gate definition collisions.

CLA extension: 4657 binary/directed/random and X/Z cases. mul8 extension: all
65536 binary pairs plus 224 X/Z cases (65760 total). SAT proves combinational
binary equivalence independently; the extension does not prove all four-state
inputs. Original negative controls fail TB and SAT. Three baseline syntheses
agree for every repeat. Final retained versions rerun functional checks;
non-baseline retained versions also get mapped-cell equivalence and repeated
synthesis. Large drops >10% require mapped proof before acceptance.

Deterministic pre-model DEV probes show that restructuring survives synthesis.
CLA nested carry factoring is valid but increases area. A balanced conditional
partial-product probe reduces mul8 area by about 19.9%, to 2009.4272, and passes
original/extended TB, binary RTL and mapped equivalence. An earlier version with
new unused-signal warnings was rejected and retained in the log. No template
text, synthesis score or feedback from this screen is included in paid prompts.
Both designs therefore have been screened; they are not unseen held-out designs.

## Frozen matched trial

Same provider settings as the previous non-thinking pilot: deepseek-flash,
thinking disabled, temperature=1, max output 8192 tokens, max input 24000 bytes,
300-second HTTP timeout, no retries. Each design has three repeats. Every repeat
starts both arms at B0 and gives each arm two proposals across two rounds.
Repeat 2 reverses arm order. No cross-repeat/design/arm history is supplied.
B1 gets source, original TB and functional outcomes only; B2 adds completed
synthesis reports. Incumbent selection conveys limited indirect feedback to both.

Same Yosys, Liberty, synthesis script, ABC target and acceptance threshold:
>=0.5% area reduction with all functional/structural gates passed and no new
warnings. STA/P&R/power are not evaluated. The frozen descriptive stability rule
requires B2 to win at least 2/3 repeats on EACH design and have positive mean
paired gain on EACH. Two families with three provider draws each remain a small
pilot, without guaranteed seeded independence or broad causal conclusions.

24 new calls reserve CNY 2.774016; this batch ceiling is CNY 5. Earlier batches
are separate. Public source/TB and derived summaries go to api.deepseek.com;
credentials are never written to request artifacts. 3/3 offline tests passed,
covering scope, identical settings, prompt isolation and failed-call accounting.

## Completed result

24 calls completed; 20 delivered parseable candidate blocks. Both arms retained
the original on every repeat of both designs: all six pairs tie at zero area
improvement. The frozen stability gate was not met.

- 12 exact/normalized duplicates, 1 synthesis collapse.
- 5 candidates failed the area threshold, 1 had new warnings, 1 failed the
  four-state extension. Four responses were invalid candidates (three returned
  only {"type":"json_object"}; one returned extra JSON data).
- Seven non-duplicate proposals reached and passed original/extension TB and
  binary formal checks. Duplicates were not revalidated as new proposals.
- All six final pair audits and retained-original regressions passed.
- Peak-rate usage estimate: CNY 0.07688984. No retries or replacement calls.

The deterministic DEV multiplier witness is separate from the model arms:
2507.4048 -> 2009.4272 area (-19.8603%), full 65536-pair binary simulation,
X/Z extension, binary RTL equivalence and mapped equivalence pass. Its mapped
netlist also repeats byte-identically three times. It was never fed to models.
This demonstrates available improvement in the allowed space, while the current
proposal process did not find it. It does not establish a feedback benefit.

[Chinese report](../../work/arithmetic_stability_v1/REPORT.md)
[Paired outcomes](../../work/arithmetic_stability_v1/summary.csv)
[Every attempted proposal](../../work/arithmetic_stability_v1/attempts.csv)
[Deterministic witness RTL](../../dev/complex_screen/mul8_evaluator/rtl/T0-clean/dut.v)

The inherited per-repeat audit claim_scope display string mentions muxes; the
correct scope is this arithmetic cohort, as fixed in plan.json and protocols.
[Metadata erratum](../../work/arithmetic_stability_v1/scope_label_erratum.json)
records this without changing frozen code or historical evidence.


    source ~/.llm_env
    export LLM_MODEL=deepseek-flash
    python3 experiments/arithmetic_stability/cohort.py live --root work/arithmetic_stability_v1

Per-attempt requests/responses, patches, verification and synthesis logs, feedback
hashes, final regressions, supplemental proof and all failed attempts are retained.
