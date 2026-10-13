# S2G: Physical-Feedback-Guided RTL Generation and Optimization

S2G provides a research workflow from a reviewed specification or golden RTL to verified RTL candidates and GDS, with synthesis and physical feedback for candidate exploration.

The current implementation supports combinational designs on the Nangate45 research platform. It binds specifications, verification assets, implementation constraints, tools, and process files to frozen hashes. Candidate generation uses a file-based request/submit interface or deterministic typed rewrites; provider calls are never implicit.

## Source layout

- `spec2gds/`: spec contracts, candidate accounting, functional verification, synthesis, stage-by-stage ORFS execution, RCX/STA, GDS, DRC/LVS, stage graphs, feedback, Pareto selection, and dataset manifests.
- `rtl_rewrite_pilot/rtltrial/`: shared netlist/Liberty helpers and the structural candidate search (the sky130hd v1 pilot CLI was removed; see its README).
- `rtl_rewrite_pilot/experiments/`: experiment runners and verification helpers.
- `spec2gds/examples/`: specification, reference RTL, TB, and constraint fixtures.
- `spec2gds/vendor/r2g/`: pinned, licensed DEF/LEF/Liberty parsers. Runtime script dependencies stay inside S2G; EDA tools and process assets may be external.
- `spec2gds/platforms/nangate45/`: the local LVS adapter, its license, and provenance.

Only source scripts, configuration, tests, examples, and dependency notices are versioned. All `docs` directories, experimental results, run/work directories, graph outputs, downloaded third-party checkouts, tool binaries, and caches remain local. Links to historical run evidence in component READMEs refer to local artifacts excluded from this repository.

Project plans, specifications, and architecture documents are maintained in English under `docs/`. Experimental data documentation, integration records, smoke reports, and case studies are maintained in English under `data/`. Both directories remain local and excluded from Git. Do not create another `spec2gds/docs/` or `docs/datas/` tree.

## Run the current framework

Use Python 3.10 or newer. The core uses only the standard library. Configure the EDA executables, ORFS flow directory, process assets, and S2G-local paths in `spec2gds/configs/local-nangate45.json` before running on another machine.

```bash
cd spec2gds
python3 -m s2g doctor --context configs/local-nangate45.json
python3 -m unittest discover -s tests -v
python3 scripts/smoke_mul8.py --root runs/my-new-mul8-smoke
```

Run the bounded optimization loop over an initialized workspace. Use `--scripted` for offline responses; `--live` makes paid provider calls and needs the provider key in the environment:

```bash
python3 -m s2g loop-init --root runs/project-a --loop a2-001 --arm A2 --scripted responses.json
python3 -m s2g loop-run  --root runs/project-a --loop a2-001 --scripted responses.json   # resumable
python3 -m s2g loop-status --root runs/project-a --loop a2-001
```

Arm comparisons with repeated independent runs and a currency ceiling use `exp-init`, `exp-preflight`, `exp-run` and `exp-report`. A live run refuses to start unless preflight confirms that the frozen model is served and that the balance covers the remaining ceiling.

The smoke runner creates original and balanced-tree multiplier candidates, validates them through GDS acceptance, checks stage graphs, and runs positive and signal/body negative LVS controls. Its output root must not already exist. Generated results remain outside Git.

The example review is explicitly a built-in demonstration, not a substitute for user review of a production specification. Sequential equivalence, multi-clock designs, trained risk predictors, multiple corners, and manufacturing signoff require additional qualified adapters. Nangate45 itself is a non-manufacturable research library.

## Current implementation and fixes

The reviewed local smoke has completed TB, RTL/mapped/postroute equivalence, timing, electrical checks, DRC, LVS, and stage-graph validation. Regression coverage includes:

- PWELL body-connection consistency in the Nangate45 LVS deck and explicit per-check report destinations.
- Propagation of batch failures and distinct nonzero exit codes for completed but rejected runs.
- Candidate-specific graph identities, validated stage boundaries, and failure-closed graph acceptance.
- Preservation of completed measurements when a later checker fails.
- Explicit separation of raw worst slack and WNS, with legacy-schema compatibility.
- Frozen manifests covering selected local process overrides.

These checks establish workflow behavior on the current examples. LLM feedback gains and stability across unseen design families remain experimental questions.

## Historical pilot dependencies

Legacy experiment runners expect benchmark checkouts and, for some replay tests, local snapshots that are intentionally excluded. The benchmark revisions used during development were:

- RTLLM: `51ed553d0ffd32797a1a0a13e051656bf302c81f`.
- VerilogEval: `c498220d0a52248f8e3fdffe279075215bde2da6`.
- Icarus Verilog source: `4fd5291632232fbe1ba49b2c26bb6b2bf1c6c9cf`.

The current `spec2gds` fixtures and regression suite do not require archived experiment outputs. Historical replay suites may require their original snapshots; they are not the source-only installation smoke.
