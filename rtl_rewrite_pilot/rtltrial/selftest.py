"""Adapter/parser self-test on success, failure, missing-evidence and timeout cases.

Synthetic logs exercise the PASS-contract parser; one real simulation that never
finishes exercises the timeout path end to end."""
import tempfile
from pathlib import Path

from .tb import ADAPTER_TAG, judge
from .util import run_job

PC = {"pass_regex": "=+Your Design Passed=+", "fail_regexes": ["Test completed with\\s+\\d+\\s*/\\s*\\d+ failures", "^ERROR"],
      "finish_regex": "\\$finish called at (\\d+)",
      "adapter_requirements": {"valid_out_events": [">=", 100], "error": ["==", 0]},
      "adapter_failure_fields": ["error"]}
OK_JOB = {"exit_code": 0, "timed_out": False, "elapsed_s": 0.1}
FIN = "tb.v:79: $finish called at 72220 (1ns)"

CASES = [
    ("pass", f"===Your Design Passed===\n{FIN}\n{ADAPTER_TAG} valid_out_events=100 error=0", OK_JOB, "TB_PASSED"),
    ("tb failure marker", f"===Test completed with 7 /100 failures===\n{FIN}\n{ADAPTER_TAG} valid_out_events=100 error=7",
     OK_JOB, "TB_FAILED"),
    ("checker nonzero, no marker", f"===Your Design Passed===\n{FIN}\n{ADAPTER_TAG} valid_out_events=100 error=3",
     OK_JOB, "TB_FAILED"),
    ("too few executed checks", f"===Your Design Passed===\n{FIN}\n{ADAPTER_TAG} valid_out_events=4 error=0",
     OK_JOB, "INCONCLUSIVE"),
    ("no finish", f"===Your Design Passed===\n{ADAPTER_TAG} valid_out_events=100 error=0", OK_JOB, "INCONCLUSIVE"),
    ("no pass marker", f"{FIN}\n{ADAPTER_TAG} valid_out_events=100 error=0", OK_JOB, "INCONCLUSIVE"),
    ("no adapter line", f"===Your Design Passed===\n{FIN}", OK_JOB, "INCONCLUSIVE"),
    ("nonzero exit", f"===Your Design Passed===\n{FIN}\n{ADAPTER_TAG} valid_out_events=100 error=0",
     {**OK_JOB, "exit_code": 1}, "INCONCLUSIVE"),
    ("timeout", "", {"exit_code": None, "timed_out": True, "elapsed_s": 1.0}, "TIMEOUT"),
]

HANG_TB = """module hang; reg clk = 0; always #5 clk = ~clk; endmodule
"""


def run(iverilog="iverilog", vvp="vvp"):
    results = []
    for name, log, job, want in CASES:
        got, _ = judge(log, job, PC)
        results.append((name, want, got))
    with tempfile.TemporaryDirectory() as d:
        Path(d, "hang.v").write_text(HANG_TB)
        c = run_job([iverilog, "-g2012", "-o", "h.vvp", "hang.v"], d, Path(d, "c.log"), 60)
        if c["exit_code"] == 0:
            j = run_job([vvp, "-n", "h.vvp"], d, Path(d, "s.log"), 1.0)
            got, _ = judge(Path(j["log"]).read_text(), j, PC)
        else:
            got = f"compile failed ({c['exit_code']})"
        results.append(("real simulation timeout", "TIMEOUT", got))
    return results
