# Deterministic backend screen (no LLM)

This independently scoped extension runs the existing `mul8` sum-tree candidate
and `cla32` carry-factor candidate through OpenROAD/ORFS on Sky130 HD. It tests
whether physical risk is observable and responds to a legal structural rewrite.
It does not measure the benefit of giving an LLM feedback.

## Reproduce

From `rtl_rewrite_pilot/`:

```bash
python3 experiments/backend_screen/screen.py prepare --root dev/backend_screen_new
# The prepare step verifies the frozen source searches, original/extended TBs,
# RTL equivalence, exact re-synthesis, and mapped equivalence.
python3 experiments/backend_screen/screen.py freeze --root dev/backend_screen_new
python3 experiments/backend_screen/screen.py run --root dev/backend_screen_new \
  --name mul8-original-s17 --design mul8 --variant original --seed 17
# Repeat run for {mul8,cla32} x {original,candidate} x {17,29}.
python3 experiments/backend_screen/analyze.py --root dev/backend_screen_new --prove
python3 experiments/backend_screen/validate_evidence.py --root dev/backend_screen_new
python3 -m unittest experiments.backend_screen.test_analyze -v
```

Requires the installed `/opt/OpenROAD-flow-scripts/flow` platform/scripts,
`/usr/bin/openroad`, the existing oss-cad-suite tools and system Python with
PyYAML. The experiment Python driver uses the existing stdlib-only pilot.
Bootstrap compatibility runs use names starting with `bootstrap`; they are
excluded from the eight measurements. A fresh directory is required for every
physical run. No model client is invoked or credentials sourced.

## Frozen comparison

- Existing candidate IDs `B1-001` are **deterministic**, not new LLM calls.
  `mul8`: `dev/structure_search_v1/mul8`; `cla32`:
  `dev/structure_search_v2/cla32`. Inputs are copied and validated before routing.
- Same synthesis recipe as the candidate search. Fresh mapped netlists must
  match the source experiment byte-for-byte, then enter ORFS through its cached
  netlist interface. Both RTL and mapped binary equivalence must pass.
- Fixed core/die and explicit pin locations per design, identical SDC, library,
  thread count (1), density target (0.60), repair settings and routing layers.
  Final SDC, actual core/die area and pin inventories are audited across runs.
- Synthetic virtual clock 10 ns, input/output max delay 2 ns, min 0.2 ns,
  input `buf_1`, output load 0.005 pF. There are **no physical clock trees** in
  these combinational designs. This is an experimental interface contract,
  not an owner timing target.
- `OR_SEED=17,29` changes detailed routing; earlier stages are deterministic.
  These are not two independent placement seeds, RTL samples, or statistical
  evidence of generality. Constraints were not retuned after candidate results.
- ORFS installed tree has no usable Git metadata. Hashes of flow scripts,
  platform files, RTL/netlists, constraints and OpenROAD binary are frozen in
  `plan.json`, with the observed version recorded. Local script copies restore
  executable permissions; the runner isolates incompatible Python environment
  variables. The system installation is unchanged.

## What is measured

Full floorplan/placement/global route/detailed route, then OpenRCX extraction
and STA using the extracted SPEF. The snapshot is before insertion of inert
fill cells; `5_route.odb` is the post-fill endpoint. No GDS/signoff run is made.

`backend_risk.json` records placement pre-repair, global route pre/post repair,
and post-route worst slack, WNS, TNS, violating endpoints, bounded slew/cap
violations and margins, area, routing overflow, wirelength, vias and detailed
router DRC counts by iteration. Stage names refer to **that stage's repair**:
placement pre-repair already includes earlier floorplan and timing-driven
placement optimization. No-path/sentinel values and unbounded fanout checks
are unknown, not successful zeroes.

Each wrapped `repair_design`, `repair_timing`, `buffer_ports`, tie-fanout and
antenna repair command retains the native policy and arguments. Before/after
cell inventories provide inserted/removed/master-changed instance counts and
area deltas. Physical fillers/taps/decaps are excluded. These are **net edits
per call**, not all optimizer attempts, pin swaps or rolled-back edits. Internal
GPL and router transformations are outside this counter. Mandatory port
buffering is reported separately from risk-driven design/timing repair.
Native logs remain available. Repair elapsed times exclude snapshot overhead;
flow times include it and concurrent-run scheduling, so are descriptive only.

The stock ORFS Sky130 equivalence helper is disabled because it is unavailable
for this installation. Instead, the evaluator expands frozen Liberty functions
and proves all binary inputs for original RTL versus mapped inputs, and mapped
inputs versus routed netlists. Original and extended X/Z testbench results are
also retained; binary formal proof does not claim analog, electrical or X/Z
semantics. Router DRC=0 is not foundry signoff DRC/LVS.

A run is `MEASURED` only with completed flow artifacts, extracted timing,
positive routed equivalence evidence, complete metrics/repair records and
unchanged pins. `MEASURED` does not mean timing closure or improvement.
Missing evidence, failed proof, incomplete routing or a timeout cannot pass.
The runner kills the full process group on the 900 s per-flow timeout.

## Artifacts

Measured run: [dev/backend_screen_v1](../../dev/backend_screen_v1/).
Each run keeps native logs/JSON/reports/ODB/SPEF, cell and pin inventories,
`routed_equivalence/`, and `backend_risk.json`. Aggregate outputs are
`summary.csv`, `results.json`, `audit.json` and `REPORT.md`.
