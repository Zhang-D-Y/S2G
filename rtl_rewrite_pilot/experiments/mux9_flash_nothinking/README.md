# mux9 non-thinking Flash comparison

Prepared 2026-10-11 after the user requested disabling thinking and repeating
the feedback comparison. Fresh B1/B2 arms start at original B0; previous Pro,
thinking Flash and DEV candidates/results are excluded from prompts.

Model: deepseek-flash (DeepSeek-V4.1-Flash). thinking=disabled, temperature=1.0;
reasoning_effort and top_p omitted. Non-thinking top_p is fixed at 1 by provider.
Both arms have two attempts over two rounds (1+1), max output 8192 tokens,
max input 24000 UTF-8 bytes, no retries. Calls ordered B1,B2 then B1,B2.

B1 sees source, original TB, contract and prior functional validation outcomes.
B2 adds only synthesis reports from completed runs. Both use the same evaluator:
scope, lint, original TB, four-state extension, binary RTL equivalence and mapped
sky130hd synthesis. Accept area reduction >=0.5%; >10% requires extra mapped
netlist equivalence. Incumbent selection conveys limited indirect feedback to
both arms. No timing, P&R, power or generalization claim is supported.

Four new calls reserve CNY 0.462336 at peak rates; with all earlier reservations,
total reservation is CNY 1.817472, below the original CNY 5 envelope. The earlier
six-call batch remains closed; these four follow the new user instruction.
Public RTL/TB and derived synthesis summaries go to api.deepseek.com only.

Fresh baseline and negative controls passed. Offline controls 3/3 passed,
including explicit non-thinking request settings, failed-call accounting and
no retries. Initial shared prompts are byte-identical; B2 adds baseline feedback.

## Completed result

All 4 responses returned complete JSON candidates with finish_reason=stop,
193-228 output tokens and no reasoning content. Mean proposal latency was
2.6445 seconds. This run demonstrates delivery for these four calls, not
statistical reliability across other tasks.

| Retained design | Area | Cells | Logic depth | Area improvement |
|---|---:|---:|---:|---:|
| B0 original | 775.7440 | 110 | 4 | 0% |
| B1 without synthesis reports | 775.7440 | 110 | 4 | 0% |
| B2 with synthesis reports | 754.4736 | 96 | 4 | 2.7419% |

- B1 first attempt passed functional checks but collapsed to B0 synthesis.
- B1 second attempt omitted the sel=8 -> i behavior; the original TB rejected it.
- B2 first attempt wrapped the exact case in an explicit sel < 9 guard, with
  all-ones defaults. It passed all gates and was retained.
- B2 second attempt removed that guard and returned to baseline area; rejected
  against the improved incumbent. It did not improve further with feedback.

Both final designs pass the original 220-sample TB, 2048-sample extension and
binary combinational equivalence. Additional post-run verification of the B2
winner passed mapped-netlist equivalence, and two repeated syntheses reproduced
the original candidate's exact mapped netlist bytes (3/3 identical). These extra
checks did not change selection and were not given to the models. Their tool
cost is recorded separately in supplemental_verification.json.

This is a positive observation for B2 on one screened DEV mux: a better mapped
netlist was produced while the tested behavior and binary equivalence were
preserved. One stochastic run cannot attribute the improvement causally to
feedback, establish multi-round learning, or support timing/P&R/power claims.
Both first-round and second-round prompts and feedback provenance are retained.

Four calls cost CNY 0.02344488 at published peak rates (usage estimate, not an
invoice). All model batches total ten submitted requests, including one earlier
interrupted Pro request with unknown billing. Known peak estimates plus that
request's reservation total CNY 0.95732132; all reservations total CNY 1.817472.

Results: [summary.csv](../../work/mux9_flash_nothinking_v1/summary.csv),
[audit.json](../../work/mux9_flash_nothinking_v1/audit.json),
[supplemental verification](../../work/mux9_flash_nothinking_v1/supplemental_verification.json),
[accounting](../../work/mux9_flash_nothinking_v1/accounting.json).
Winning source: [B2 final RTL](../../work/mux9_flash_nothinking_v1/rtl/B2-final/dut.v).


[Run artifacts](../../work/mux9_flash_nothinking_v1/)

[Official mode parameters](https://api-docs.deepseek.com/guides/thinking_mode/)
[Official peak prices](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)

Audit command (no paid calls):

    python3 experiments/mux9_flash_nothinking/experiment.py audit --root work/mux9_flash_nothinking_v1

Contract SHA256: a9a17264db8a030c60ce401fd91cdf17706f8627057a2a0d2ea3a37a9efa3eff
