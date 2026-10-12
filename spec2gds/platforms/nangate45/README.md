# S2G Nangate45 LVS adapter

This local copy changes two parts of the installed research-platform deck. The original platform tree remains untouched; `PROVENANCE.json` records source and patched hashes, and `LICENSE` retains the platform license.

1. NMOS extraction connects its body to `pwell`, globally named `PWELL`. The original standard-cell explicit ground alias lists `BULK` and `VSS` but omits `PWELL`. The Nangate45 CDL connects NMOS bodies to VSS. Add PWELL to the existing standard-cell alias list. No device extraction, signal connectivity, comparison, or error check is disabled.
2. `report_lvs` accepts `$report_file`, binding the result to the current checker directory instead of writing beside the source GDS.

The library already uses implicit body/power conventions for standard cells without internal taps. This adapter fixes consistency of that research convention; it does not validate manufactured well taps or qualify a foundry flow. Nangate45 is a non-manufacturable research library.

Qualification is executable and stores hashes:

```bash
cd /data1/zhangdy/S2G/spec2gds
/usr/bin/python3 scripts/qualify_lvs.py \
  --run runs/mul8-smoke-fixed-001/candidates/c0000/run-000 \
  --context configs/local-nangate45.json --out runs/my-new-lvs-qualification
```

The original design must compare MATCH. An intentionally grounded logic-cell signal input and a BUF_X1 NMOS body connected to VDD must both compare MISMATCH. KLayout exits zero even on comparison mismatches, so checks require the appropriate comparison message and a newly generated report database. Original GDS and CDL hashes are checked before and after.
