"""Offline control tests: no provider or network access is permitted."""
import contextlib
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import experiment as e
from guard import check


class Controls(unittest.TestCase):
    def test_original_and_dev_mux_blocks(self):
        for cid in ("B0", "NEG", "T0-01", "T0-02"):
            text = (e.DEV / "run_002/rtl" / cid / "dut.v").read_text()
            self.assertTrue(check(e.block_of(text))[0], cid)

    def test_scope_escapes_rejected(self):
        for text in (
            "always @(*) out = a+b;",
            "always @(*) begin sel=0; out=a; end",
            "always @(*) out=16'hxxxx;",
            "always @(*) out=$random;",
            "always @(*) out=a; module Bad; endmodule",
            "always @(*) out=out;",
            "always @(*) if(a) out=b; else out=c;",
            "always @(*) casez(sel) default:out=a; endcase",
        ):
            self.assertFalse(check(text)[0], text)

    def test_failed_calls_spend_slots_before_network_and_never_retry(self):
        source = e.PILOT / "work/mux9_b1_b2_v1"
        with tempfile.TemporaryDirectory(prefix="s2g-offline-controls-") as tmp:
            root = Path(tmp)
            for part in ("frozen", "rtl", "feedback"):
                shutil.copytree(source / part, root / part)
            for name in ("protocol.json", "baseline.json", "state.json"):
                shutil.copyfile(source / name, root / name)
            self.assertEqual(e.read_json(root / "state.json")["attempts"], [])
            rd = root / "runs/B0-synth-1"; rd.mkdir(parents=True)
            shutil.copyfile(source / "runs/B0-synth-1/synth_result.json", rd / "synth_result.json")
            count = []

            def failed_call(request):
                state = e.read_json(root / "state.json")
                count.append(request)
                self.assertEqual(len(state["attempts"]), len(count))
                self.assertAlmostEqual(state["reserved_cny"], len(count) * e.CALL_RESERVE)
                return {"error": "offline simulated HTTP 500", "http_status": 500,
                        "response": None, "elapsed_s": 0, "completed_at": e.now()}

            with patch.object(e, "verify", return_value=e.read_json(root / "protocol.json")), \
                 patch.object(e, "balance", return_value={"total_balance": 10}), \
                 patch.object(e, "load_liberty", return_value={}), \
                 patch.object(e, "call_provider", side_effect=failed_call), \
                 patch.object(e.screen, "simulate", return_value={"status": "TB_PASSED"}), \
                 patch.object(e.screen, "formal", return_value={"status": "PASS"}), \
                 patch.object(e, "audit"), \
                 patch("urllib.request.urlopen", side_effect=AssertionError("network prohibited")), \
                 contextlib.redirect_stdout(io.StringIO()):
                e.live(root)
                e.live(root)
            self.assertEqual(len(count), 6)
            state = e.read_json(root / "state.json")
            self.assertEqual(len(state["attempts"]), 6)
            for row in state["attempts"]:
                self.assertEqual(e.read_attempt(root, row["id"])["decision"], "REJECT")
            self.assertEqual(state["incumbents"], {"B1": "B0", "B2": "B0"})


if __name__ == "__main__":
    unittest.main()
