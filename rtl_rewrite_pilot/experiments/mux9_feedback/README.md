# mux9 synthesis-feedback B1/B2 pilot

Prepared on 2026-10-11 (Asia/Shanghai), using public VerilogEval mux9to1v at
commit c498220d0a52248f8e3fdffe279075215bde2da6. This previously screened DEV
design is not an independent held-out test.

Run directory: ../../work/mux9_b1_b2_v1/

Frozen protocol SHA256:
e47a640ee1f0d898facb60f40aa36f814e133831f45d5cb4331dc369470fc3e2

## Experiment

| Setting | B1 | B2 |
|---|---|---|
| Proposer | deepseek-v4-pro | same |
| Thinking | enabled, high; top_p=0.95 | same |
| Temperature | omitted; ineffective in thinking mode | same |
| Budget | 3 calls, rounds 2+1 | same |
| Limits | 24000 input UTF-8 bytes; 8192 output tokens including reasoning | same |
| Start | immutable original mux9 | same |
| Shared inputs | RTL, original TB, contract, previous functional outcomes | same |
| Synthesis diagnostics | excluded | completed current/previous-round synthesis reports |
| Evaluator and controller | identical rules | identical rules |

Only one local combinational mux block may change. guard.py checks a restricted
grammar: mux grouping and conditional selection are permitted; new signals,
data arithmetic, loops, attributes, directives and don't-cares are forbidden.
Equivalence is checked against the original reference regardless of the parent.

Acceptance requires at least 0.5% area improvement, original and extension TB
passes, binary combinational RTL equivalence, no new structural risks/warnings,
and complete sky130hd mapping. A drop greater than 10% triggers an extra mapped
netlist equivalence audit using the frozen Liberty functions. Only a successful
audit clears that guard. There is no timing, P&R or power claim.

Both first-round proposals share the original parent. The best accepted
candidate becomes the second-round parent. This selection conveys limited
indirect feedback to both arms. B2 alone receives explicit synthesis reports.
Previous deterministic DEV candidates and results are excluded from prompts.

All malformed, duplicate, failed and collapsed proposals consume a slot.
Slots are reserved before network requests; no automatic retries occur.
Interrupted requests retain both their slot and conservative cost reservation.
Call order: B1, B2, B2, B1 for round one, then B1, B2 for round two.

## Cost and transmitted data

Destination: https://api.deepseek.com/chat/completions

Only the public mux9 source and original TB, frozen experiment instructions,
generated mux blocks and functional statuses, and (B2 only) synthesis summaries
for this public design are sent. No private project RTL, unrelated experiments,
credentials or full library files are included in prompts. Credentials are
read from the environment and never stored in request artifacts.

A free account-balance lookup must confirm funding before model calls.
The ceiling is CNY 5; conservative six-call reservation is CNY 2.6784.
The [official peak prices](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)
checked on 2026-10-11 are CNY 9/M uncached input tokens, CNY 0.30/M cached
input tokens, and CNY 27/M output tokens. Reservation assumes uncached input,
one token per UTF-8 input byte plus 1024 framing tokens, and maximum output
for every call. Actual usage and peak-price estimates are reported separately.
Account balance differences may include concurrent account activity.

## Preflight and current status

- Fresh baseline: original TB PASS (220 samples), extension PASS (2048 samples),
  three identical synthesis repetitions.
- Negative RTL control: rejected by both original TB and binary equivalence.
- Extra mapped-equivalence check: real positive and corrupted negative controls
  respectively PASS and FAIL.
- Initial shared prompts are byte-identical. B2 adds only its feedback block.
- Sentinel synthesis scores/reasons do not leak through B1's history.
- Initial B1/B2 inputs are 6677/8827 UTF-8 bytes, within their common limit.

Explicit authorization was subsequently received. Two Pro requests were
submitted: B1-r1-a1 completed (functionally valid but +2.58% area; REJECT),
and B2-r1-a1 was interrupted after submission when the user changed the model
to DeepSeek-V4.1-Flash. Its response and billing are unknown. Both slots remain
charged against the six-call authorization. This Pro comparison is incomplete
and must not be resumed or treated as a B1/B2 result.

The remaining four calls use a fresh, separate
[Flash experiment](../mux9_flash_feedback/README.md), with two calls per arm.
Original frozen code, protocol and artifacts are retained here.
