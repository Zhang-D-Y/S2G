# Repeated mux synthesis-feedback study

Frozen 2026-10-11 before paid calls. Run: ../../work/mux_stability_v2/.
Contract SHA256: b1dcfeb10b987983edadcdb372c540d589f2beffefe9d602350e75054c1bf694.

Three original VerilogEval designs at c498220d0a52248f8e3fdffe279075215bde2da6:

| Design | Stratum | Original TB samples | Extension samples | Baseline area |
|---|---|---:|---:|---:|
| Prob076_always_case (6 x 4-bit mux) | new design | 118 | 512 | 143.888 |
| Prob130_circuit5 (4 x 4-bit mux) | new design | 141 | 2048 | 112.608 |
| Prob097_mux9to1v (9 x 16-bit mux) | DEV replication | 220 | 2048 | 775.744 |

The two new designs were selected by compatibility with the frozen local
exact-selector mux rewrite class, before any paid proposal. They are public
benchmarks and are not claimed to be unseen by the model. No size-scaled or
renamed copies count as separate designs. Baselines only rename the DUT module;
upstream references and TBs are unchanged. Port names, widths and default
constants parameterize the existing scope guard and extension generator.
The extension covers every four-state selector with eight frozen data patterns.

Each design has three fresh repeats, each with B1 and B2 taking two attempts
in two rounds (1+1): 36 calls total. Repeats are new provider draws, not seeded
replicas; no provider seed or guaranteed statistical independence is claimed.
Designs interleave across repeats; repeat 2 reverses arm order. Every arm starts
from its original RTL with no cross-arm/design/repeat history. Prior pilot
results and proposed fixes are excluded from model inputs.

## Fixed treatment and evaluator

Identical to the prior non-thinking pilot: deepseek-flash, thinking disabled,
temperature 1.0, no reasoning_effort/top_p, 8192 output tokens, 24000 input bytes,
300-second HTTP timeout, no retries. B1 sees source, original TB, contract and
functional outcomes. B2 additionally sees only completed synthesis feedback
from its own parent and earlier round. Controller selection conveys limited
indirect information to both arms. A maximum 6000-byte single combinational
block may be changed; ports, timing behavior, reference and TB are immutable.

The same Yosys, sky130hd Liberty, mapping script and tools are hash-checked.
Acceptance requires original TB, extension, binary RTL equivalence, lint and
structural checks, and >=0.5% area improvement. Drops >10% require mapped-cell
equivalence. Failed/invalid/duplicate/collapsed proposals consume budget.
Each final retained non-baseline design gets extra mapped equivalence and two
additional synth repeats, which are not fed back into selection or prompts.
There are no STA, P&R, power or general functional-correctness claims beyond
these tests and binary proofs.

Original TB pass floors adapt to actual upstream sample counts: baseline must
have >=100 checked samples and positive normal completion; candidates must
match their own baseline count. The initial preflight v1 inherited the mux9
minimum of 200, rejected the 118-sample baseline before any paid call, and is
retained with blocked.json and frozen snapshots. v2 contains the corrected
pre-call sample contract. No model outcomes informed this change.

## Pre-registered analysis

Primary results include the two new designs only. DEV mux9 is separate.
Report all final retained areas, per-design mean improvement, B2/B1 wins and
ties, range across repeats, complete-candidate and functional pass rates, and
all failures. The frozen descriptive stability gate requires B2 to win at
least 2/3 repeats on EACH new design and have positive mean paired improvement
on EACH new design. This small study does not support broad causal claims or
reliable population-level confidence estimates.

## Budget and status

36 submissions reserve CNY 4.161024 at current published peak rates; this batch
has a CNY 5 ceiling. Previous experiments remain separate paid batches. The new
user instruction authorizes expanding designs and repeats with the same public
payload types and DeepSeek destination. Credentials are never saved in prompts.

[Official prices](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)
[Thinking settings](https://api-docs.deepseek.com/guides/thinking_mode/)

All nine baseline/negative-control bundles and prompt isolation passed. Offline
control tests 3/3 passed (scope parameterization, configuration identity,
failure accounting/no retries, and B1 synthesis-history exclusion).
## Completed result

36/36 calls delivered complete candidate blocks. Nine pair audits and final
regressions passed. The pre-registered stability rule was NOT met.

| Design | B1 mean area gain | B2 mean area gain | B2 wins / B1 wins / ties |
|---|---:|---:|---|
| mux6 (new) | 0% | 0% | 0 / 0 / 3 |
| mux4 (new) | 0% | 0% | 0 / 0 / 3 |
| mux9 (DEV) | 0.91398% | 0.96774% | 1 / 1 / 1 |

The six pairs on new designs all retained their baselines. On mux9, repeat 1
favored B1 (+2.74194% area gain), repeat 2 tied, and repeat 3 favored B2
(+2.90323%). The earlier pilot's successful B2 example is excluded from these
means. This batch gives no evidence of stable feedback benefit on the new
samples; the DEV difference is only 0.05376 percentage points on average and
changes sign across repeats.

Both improved retained netlists pass binary mapped equivalence and reproduce
byte-identically across three syntheses. There were 14 collapsed proposals,
3 duplicates, 2 kept proposals and 17 rejects (4 four-state extension failures,
12 insufficient area improvements, 1 new warning/structural-risk rejection).
29 proposals completed the TB/extension/binary-formal gates; duplicates were
not re-evaluated and must not be counted as verified new proposals.

Peak-rate usage estimate is CNY 0.12255704, not an invoice. No retry or extra
model calls occurred. Supplemental verification costs are included in the
analysis artifact separately from model tokens.

[Chinese result report](../../work/mux_stability_v2/REPORT.md)
[All paired outcomes](../../work/mux_stability_v2/summary.csv)
[All attempted proposals](../../work/mux_stability_v2/attempts.csv)
[Full analysis](../../work/mux_stability_v2/analysis.json)

Replaying the live command skips completed pairs and does not resubmit their
calls. It is not a command for starting additional paid experiments.


    source ~/.llm_env
    export LLM_MODEL=deepseek-flash
    python3 experiments/mux_stability/cohort.py live --root work/mux_stability_v2

After completion, summary.json / summary.csv give matched outcomes; attempts.csv
includes every attempted proposal. Per-repeat audit.json, raw requests/responses,
source patches, feedback hashes, final regressions and supplemental proof and
repeated-synthesis logs are retained.
