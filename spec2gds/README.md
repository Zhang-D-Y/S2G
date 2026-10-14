# S2G spec→GDS Research System v0.1

A runnable combinational workflow with one top module is implemented: specification-review binding → golden/candidate import → TB and SAT equivalence → synthesis and mapped equivalence → stage-by-stage OpenROAD implementation → RCX/STA → GDS → DRC/LVS → stage graphs, feedback, candidate pools, and dataset export.

This is an executable foundation for the overall plan. It does not establish LLM optimization gains or imply that arbitrary natural language can automatically become a trusted specification or complete process signoff. **Workflow COMPLETE and design ACCEPTED are separate states.** The missing PWELL body connection in the local Nangate45 LVS deck has been identified and corrected, with signal/body negative controls added. See [mul8 smoke and debug](../data/MUL8_SMOKE_DEBUG.md) for full revalidation; the [initial integration results](../data/INTEGRATION.md) retain the actual pre-fix failures.

## Directory and Dependency Boundaries

All scripts and outputs for this project reside in `S2G/spec2gds/`; reused historical candidate rules reside in `S2G/rtl_rewrite_pilot/`. R2G DEF/LEF/Liberty parsers have been copied into `vendor/r2g/`, with the original MIT license, commit, and per-file hashes. **Runtime does not import or execute scripts from `Tools/agent-r2g`.**

External dependencies are limited to the EDA toolchain and process assets. The explicit compatibility profile uses ORFS scripts from `/opt/OpenROAD-flow-scripts/flow`, the Nangate45 platform in the user's tool directory, and installed Yosys/OpenROAD/KLayout/Icarus executables. ORFS scripts are copied into each workspace and frozen; instrumentation changes only those copies. This combination has passed project tests and cannot be combined with historical sky130hd experiments for comparison. Versions, scripts, tools, PDK files, and verification assets are hashed.

The `S2G` root can be relocated: first update the explicit paths in `configs/local-nangate45.json`. `graph_parser_dir`, `typed_rules`, and `typed_guard` must reside inside S2G; tool and PDK paths may be external.

## Running the Workflow

CLI exit codes: `0` means the requested stage was reached (finish also requires ACCEPTED), `1` indicates tool/verification failure, `2` indicates an input or frozen-contract error, and `3` means execution completed but acceptance failed. Batch commands propagate any failure; synthesis-only requests do not require backend acceptance.

The core uses only the Python standard library. Prefer `/usr/bin/python3` to avoid inheriting the oss-cad Python environment.

```bash
cd /data1/zhangdy/S2G/spec2gds
/usr/bin/python3 -m s2g doctor --context configs/local-nangate45.json
/usr/bin/python3 -m unittest discover -s tests -v
# The root must not exist; runs two GDS flows, an offline request/submit, and a faulty negative control.
/usr/bin/python3 scripts/demo.py --root runs/my-first-demo
# Previously tested mul8 original + balanced-tree candidate, including full physical acceptance and LVS positive/negative controls.
/usr/bin/python3 scripts/smoke_mul8.py --root runs/my-mul8-smoke
```

The example contains exact 8-bit addition and a ripple rewrite. Each legal RTL undergoes exhaustive testing of 65,536 input pairs and a combinational-equivalence proof over the full binary domain. The virtual IO clock is 10 ns, not a timing commitment for a real project. The `builtin_demo` review applies only to this example and cannot substitute for user specification review.

Basic usage with real inputs:

```bash
python3 -m s2g spec-check examples/adder8/spec.json
# The reviewer must read the specification first and supply the content hash from spec-check; this records a human attestation, not a digital signature.
python3 -m s2g review my-spec.json --reviewer REVIEWER --spec-hash HASH
python3 -m s2g init --root runs/project-a --spec my-spec.json \
  --reference golden.v --tb tb.v --sdc constraints.sdc --context configs/local-nangate45.json
python3 -m s2g add --root runs/project-a --rtl golden.v
python3 -m s2g run --root runs/project-a --candidate c0000
python3 -m s2g feedback --root runs/project-a --candidate c0000 --mode graph
python3 -m s2g request --root runs/project-a --candidate c0000 --mode physical
# request reserves one budget slot and writes request.json with the contract, RTL, and completed feedback.
# After the external generator returns complete RTL, submit the candidate id reported by request:
python3 -m s2g submit --root runs/project-a --candidate c0001 --rtl response.v
python3 -m s2g run --root runs/project-a --candidate c0001
python3 -m s2g pool --root runs/project-a
```

`spec-draft --requirements text.txt --out draft.json` records natural-language requirements and fields that need completion; it does not invent a complete contract. Complete `s2g.spec.v1`, obtain human review, validate an independent reference implementation and TB, then enter equivalent optimization. The file-based request/submit protocol is offline; paid provider access is available only through an explicitly live bounded loop with a frozen cost ledger. The model, endpoint, thinking configuration and budget require a concrete authorized experiment plan.

**Bounded optimization loop** (upgrade plan `docs/S2G_Upgrade_Plan_v1_EN.md`): `loop-init --root ROOT --loop ID --arm A1|A2|A3 (--scripted FILE | --live)` freezes the policy `configs/loop-policy-v1.json`, workspace, and toolchain hashes. `loop-run` uses the controller rather than the model to execute all mandatory checks (static → TB+SAT → synthesis evidence/STA → physical implementation as planned), supplies bounded `s2g.feedback.v2` evidence, promotes candidates under preregistered synthesis rules, and performs final confirmation. `loop-status` displays the summary. `--scripted` provides offline replay; only `--live` initiates paid calls. Rerunning `loop-run` after an interruption reconciles `events.jsonl` and resumes without resending completed model requests. Each synthesis also writes `synthesis_evidence.json` (area/timing/coverage/path→RTL, with units and statuses). Graph-export failure now closes only the dataset gate (`dataset_gates.graph`) and no longer marks the circuit as failed; data tasks that require graphs use `run/batch --require-graph`.

`propose --root ROOT --action action.json [--parent ID]` connects the existing `mul8`/`cla32` typed rules. The specification must declare `rewrite.design`, and structural replacement stays within the fixed rewrite region. Illegal actions, generation failures, and duplicate candidates all consume slots; every action must still pass full verification.

**Hybrid ALU generation:** a loop policy can freeze `generation.mode=typed|hybrid`, its ALU semantic map/vocabulary, a validated-parent pool and a 1–4 edit cap. Hybrid accepts typed `action=alu` proposals alongside complete RTL. `loop-init/loop-run --random random_single_action_walk|random_independent_sampling --seed N` runs offline controls through the same gates. A new optional `final_selection.kind=routed_pool` selects the smallest accepted routed implementation after search; it requires physical coverage of every eligible proposal and preserves old incumbent-only policies. Freeze concrete 4/8-proposal comparison policies with `scripts/freeze_alu_hybrid.py --root NEW_ROOT`; run a real-tool scripted smoke with `scripts/smoke_alu_hybrid.py --root NEW_ROOT`. See [the interface and comparison plan](../docs/ALU_Hybrid_Candidate_Generation_v1_EN.md) and [integration evidence](../data/experiments/alu_hybrid_v1/README.md). Both scripts make zero model calls.

**Full routed ALU landscape:** `scripts/route_alu_landscape.py prepare|run|status --root NEW_ROOT` repeats eleven known cases, gates full enumeration on exact decision-metric agreement, and routes the two complete libraries with bounded parallelism. `scripts/report_routed_alu.py build --root ROOT` audits the completed table and computes routed fixed-seed projections and a cache index. Existing registrations remain unchanged; these scripts make zero model calls. See [the protocol](../docs/ALU_Routed_Landscape_Protocol_v1_EN.md) and [run record](../data/experiments/alu_routed_landscape_v1/README.md).

`batch --root ROOT --candidates c0000 c0001 --repeats 2` executes repeated runs sequentially. `candidate_budget` and `tool_run_budget` are counted separately, with no refund for failure. Repeats share the fixed route seed and cannot be described as independent randomized trials. Changing the seed, constraints, or process requires a new workspace. An interrupted RUNNING state is not automatically treated as complete; retain the evidence and rerun in a new workspace.

## Verification and Artifacts

- `freeze.json`: binds the specification, tools, process, scripts, and verification assets. Source changes prohibit continuing the old workspace.
- `candidates/ID/record.json`: parent candidate, action, source hash, budget slot, and status.
- `run-NNN/ledger.json`: stage status, tool time, execution-code hashes, and retained failure reasons.
- `backend/work/results/.../6_final.gds`: actual layout; generating it does not automatically mean acceptance passed.
- `stages/*/design.odb|design.def`: independent stage snapshots that prevent later tools from overwriting early graph sources.
- `graphs/{floorplan,placement,cts,route}.json`: fixed synthesis topology, stage-specific coordinate masks, Liberty area and LEF dimensions, and source-tracing coverage.
- `labels.json`, `risk.json`, `signoff.json`: results separate from input graphs, missing-value masks, and DRC/LVS outcomes.
- `acceptance.json`: individual decisions for functional, mapped and postroute equivalence, setup/hold, electrical checks, finish, GDS, and DRC/LVS.
- `evidence.json`: artifact hashes rechecked before feedback and dataset export.

`export --root ROOT --splits family-splits.json --out dataset.json` exports a manifest including failed trajectories. The `family-splits.json` format is `{"demo_adder8":"dev"}`. All rewrites and repeats from one family stay in the same split. Families with shared lineage but different names must first be grouped manually; automatic code-lineage detection is not claimed.

Optional `pyg-export --graph PATH --out NEW_DIR --python /path/to/python` uses **an exporter inside S2G** to convert JSON to a PyG incidence graph. Missing torch/PyG dependencies are reported as blocking. These dependencies are not installed locally; JSON graphs are the validated artifacts, without claiming compatibility with the upstream b–f / R2G2 eight-relation dataset.

## Explicit Scope Boundaries

The current backend profile is Nangate45 at a single corner; the functional contract is combinational logic over all binary inputs, with one top module and helpers permitted by the frozen RTL policy. Sequential equivalence, multiple clocks, CDC, macros, DFT, formal proofs with input-domain assumptions, and power activity have not been qualified. Sequential contracts are blocked; finite simulation is not presented as an all-cycle proof. X/Z are outside the SAT equivalence domain. Contracts, tests, SDC, and floorplan cannot be changed to obtain improvements.

Graphs use synthesis-cell/port–net incidence. Coordinates are cell origins, not pin geometry or measured congestion. Source tracing uses surviving attributes/same-named signals and falls back to module scope when unavailable; precise critical-path localization is not claimed. No risk predictor has been trained; feedback is labeled as tool measurements or structural proxies. Repair counts are net instance edits before and after high-level repair calls, not complete counts of pin swaps, internal rollbacks, or router actions.

See the [architecture and interfaces](../docs/ARCHITECTURE.md) and [overall research plan](../docs/Physical%20Feedback%20Guided%20RTL%20Generation%20and%20Optimization_draft_v0.1.md).

`examples/mul8/` provides the existing multiplier's bridge contract and `balanced.json` action. Use `propose` in an independent workspace to generate a balanced-tree candidate; `scripts/smoke_mul8.py` executes the current full smoke test.
