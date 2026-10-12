# mux9 Flash synthesis-feedback comparison

The user selected DeepSeek-V4.1-Flash; the official API model name is
`deepseek-flash`. This separate run starts both arms from the original RTL.
The earlier Pro run was stopped after two submissions (one returned, one
interrupted with unknown billing); neither its candidates nor its feedback
enters this comparison.

## Frozen experiment

- Run: [mux9_flash_b1_b2_v1](../../work/mux9_flash_b1_b2_v1/).
- Contract SHA256: cc18a3145692018e02fd63e4ce7ca3999b177a0f31e818596ebaa2d4a4b2b0d8.
- B1 and B2 each have two attempts in two rounds (1+1), preserving the original
  authorization of six total submissions across Pro and Flash.
- B1 gets RTL, original TB, contract, previous functional-validation outcomes.
  B2 also gets completed synthesis reports for its parent/previous round.
- Same model, enabled thinking/high, top_p 0.95, 8192 output tokens, 24000 input
  UTF-8 bytes, 300-second timeout, no retries. Temperature is omitted.
- Only one combinational exact-selector mux block may change. Identical scope,
  lint, original TB, four-state extension, binary RTL equivalence and synthesis
  gates apply to both arms. All failures consume attempts.
- Accept >=0.5% area improvement. Drops >10% additionally require mapped-netlist
  equivalence. Timing, P&R and power are outside this experiment.
- Public VerilogEval Prob097_mux9to1v, commit
  c498220d0a52248f8e3fdffe279075215bde2da6; a previously screened DEV sample.

## Budget and authorization

The user approved the destination, public payload, six total calls and CNY 5,
then explicitly changed the model. The two earlier Pro submissions count.
Four remaining Flash calls reserve CNY 0.462336; together with the earlier
CNY 0.8928 reservation the total is CNY 1.355136. Interrupted requests may
still be billed. No retry or replacement call is allowed.

[Official model and peak prices](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/):
Flash input miss/hit/output are CNY 2/0.04/8 per million tokens. Off-peak is
half. Reported usage-derived peak estimates are separate from actual bills.
Credentials come from the environment; no credentials are saved in payloads.
Only public design data and its derived feedback are transmitted.

## Verification and artifacts

Fresh baseline TB and extension passed, three synth repetitions agreed, and
negative controls failed both TB and formal as intended. Offline tests 3/3
passed, including reserving every failed request and never retrying. Initial
shared prompts are identical and B2 adds only completed baseline feedback.
See preflight.json, protocol.json, attempts/, feedback/, summary.csv, audit.json,
and the final per-arm regressions in the run directory.

## Completed result

All four Flash responses report model `deepseek-flash`, `finish_reason=length`,
8192 reasoning tokens and empty final content. Consequently, no candidate RTL
was available for simulation or synthesis. This is an inconclusive generation
failure under the frozen high-thinking/8192-token setup, not evidence for or
against synthesis feedback. B2 round two therefore had baseline feedback only.

| Arm | Calls | Complete candidates | Retained area | Improvement |
|---|---:|---:|---:|---:|
| B1 | 2 | 0 | 775.744 | 0% |
| B2 | 2 | 0 | 775.744 | 0% |

The final original and extension TB regressions and binary formal checks pass
for both retained B0 copies. Audit PASS means frozen integrity, prompt isolation,
accounting and final validation passed; it does not mean optimization succeeded.

Total submissions are exactly six: two Pro plus four Flash. Flash usage implies
CNY 0.27492344 at published peak rates; known Pro usage adds CNY 0.212553.
The interrupted Pro submission has unknown usage/billing and keeps its CNY 0.4464
reservation. Known peak usage plus unknown reservation totals CNY 0.93387644,
within the CNY 5 ceiling. These are conservative estimates, not an invoice.
Full accounting and response checks: [global_accounting.json](../../work/mux9_flash_b1_b2_v1/global_accounting.json).

A future run should first address generation reliability (for example, disabled
thinking for this small mux task), then freeze an identical new setup for both
arms. No additional model calls were made; the six-call authorization is exhausted.

    source ~/.llm_env
    export LLM_MODEL=deepseek-flash
    python3 experiments/mux9_flash_feedback/experiment.py audit --root work/mux9_flash_b1_b2_v1

This single small DEV sample cannot establish causality or generalization.
