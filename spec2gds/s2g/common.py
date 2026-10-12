import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import time

class GateError(RuntimeError):
    pass

def require(ok, why):
    if not ok:
        raise GateError(why)

def read(path):
    return json.loads(Path(path).read_text())

def write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True, allow_nan=False) + '\n')
    tmp.replace(path)

def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()

def identity(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()

def worst_slack(report, kind):
    """v1 mislabeled raw worst slack as WNS. New reports distinguish the two."""
    if report.get('schema', 's2g.risk.v1') == 's2g.risk.v1':
        return report.get(kind + '_wns')
    return report.get(kind + '_ws')

def stamp():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())

def clean_env():
    e = {k: v for k, v in os.environ.items() if not any(x in k.upper() for x in ('KEY', 'TOKEN', 'SECRET', 'PASSWORD'))}
    for key in ('PYTHONHOME', 'PYTHONPATH', 'LD_LIBRARY_PATH'):
        e.pop(key, None)
    e.update(PATH='/usr/bin:/bin:' + e.get('PATH', ''), QT_QPA_PLATFORM='offscreen', LC_ALL='C', PYTHONDONTWRITEBYTECODE='1')
    return e

def job(argv, cwd, log, timeout=300, env=None):
    log = Path(log)
    log.parent.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    timed_out = False
    with log.open('w') as f:
        p = subprocess.Popen([str(x) for x in argv], cwd=cwd, stdout=f, stderr=subprocess.STDOUT,
                             env=env or clean_env(), start_new_session=True)
        try:
            rc = p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(p.pid, signal.SIGKILL)
            p.wait()
            rc = None
    return dict(argv=[str(x) for x in argv], exit_code=rc, timed_out=timed_out,
                elapsed_s=round(time.monotonic()-start, 3), log=str(log), log_sha256=digest(log))

def checked_job(argv, cwd, log, timeout=300, evidence=None):
    result = job(argv, cwd, log, timeout)
    write(str(log) + '.job.json', result)
    require(result['exit_code'] == 0 and not result['timed_out'], 'tool failed: ' + str(log))
    if evidence:
        require(evidence in Path(log).read_text(errors='replace'), 'positive evidence missing: ' + str(log))
    return result

def files_manifest(paths):
    return {str(Path(p).resolve()): digest(p) for p in sorted(set(paths)) if Path(p).is_file()}

def verify_manifest(man):
    for path, sha in man.items():
        require(Path(path).is_file() and digest(path) == sha, 'frozen file changed/missing: ' + path)

def tree_files(root):
    return [p for p in Path(root).rglob('*') if p.is_file() and '__pycache__' not in p.parts and '.git' not in p.parts]

def tcl(path):
    s = str(path)
    require(not any(c in s for c in '{}\n\r'), 'unsafe Tcl path')
    return '{' + s + '}'

def ys(path):
    s = str(path)
    require(not any(c in s for c in '\n\r"\\'), 'unsupported Yosys path')
    return '"' + s + '"'
