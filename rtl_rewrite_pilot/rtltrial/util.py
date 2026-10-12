"""Hashing, JSON records and tool-job execution shared by all stages."""
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(path) -> str:
    return sha256_bytes(Path(path).read_bytes())


def sha256_text(s: str) -> str:
    return sha256_bytes(s.encode())


def sha256_json(obj) -> str:
    return sha256_text(json.dumps(obj, sort_keys=True, separators=(",", ":")))


def hash_tree(root, rel_paths) -> str:
    """Hash an ordered list of files (path + content), so file order is part of the hash."""
    h = hashlib.sha256()
    for rel in rel_paths:
        h.update(rel.encode() + b"\0" + Path(root, rel).read_bytes() + b"\0")
    return h.hexdigest()


def read_json(path):
    with open(path) as f:
        return json.load(f)


def write_json(path, obj):
    """Atomic write: the record is either the old or the new version, never partial."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2, sort_keys=False)
        f.write("\n")
    os.replace(tmp, path)


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def run_job(cmd, cwd, log_path, timeout_s, env=None):
    """Run one tool invocation; never raises on tool failure.

    Returns exit_code (None on timeout), timed_out, elapsed_s and the log path.
    exit_code is recorded but semantic status is decided by the caller.
    """
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    timed_out = False
    with open(log_path, "w") as log:
        log.write(f"# cmd: {' '.join(map(str, cmd))}\n# cwd: {cwd}\n")
        log.flush()
        try:
            p = subprocess.run(cmd, cwd=cwd, stdout=log, stderr=subprocess.STDOUT,
                               timeout=timeout_s, env=env)
            code = p.returncode
        except subprocess.TimeoutExpired:
            code, timed_out = None, True
        except FileNotFoundError as e:
            log.write(f"# tool not found: {e}\n")
            code = 127
    return {"cmd": [str(c) for c in cmd], "exit_code": code, "timed_out": timed_out,
            "elapsed_s": round(time.monotonic() - t0, 3), "log": str(log_path)}


def tool_version(cmd) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        text = (out.stdout or out.stderr).strip().splitlines()
        return text[0] if text else "UNKNOWN"
    except Exception as e:  # noqa: BLE001 - recorded, not hidden
        return f"UNAVAILABLE: {e}"


def missing(reason: str):
    """Missing values are null with a reason, never 0."""
    return {"value": None, "reason": reason}


def norm_ws(s: str) -> str:
    return re.sub(r"\s+", "", s)
