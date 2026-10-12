from pathlib import Path
import re
from .common import require, identity, read, write, digest, stamp

IDENT = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
REQUIRED = ('design_family', 'top', 'ports', 'behavior', 'arithmetic', 'clocks', 'resets',
            'latency_cycles', 'throughput', 'protocol', 'legal_inputs', 'parameters',
            'verification', 'requirements', 'open_questions')

def validate_spec(spec):
    require(spec.get('schema') == 's2g.spec.v1', 'spec schema must be s2g.spec.v1')
    require(all(k in spec for k in REQUIRED), 'spec missing required contract fields')
    require(IDENT.fullmatch(spec['top']) is not None, 'unsafe top name')
    require(spec['design_family'] and spec['requirements'] and spec['behavior'], 'empty family/behavior/requirements')
    require(isinstance(spec['ports'], dict) and spec['ports'], 'ports required')
    for name, p in spec['ports'].items():
        require(IDENT.fullmatch(name) and p['direction'] in ('input', 'output') and
                isinstance(p['width'], int) and p['width'] > 0 and isinstance(p['signed'], bool), 'invalid port')
    require(spec['verification']['pass_marker'] and spec['verification']['tb_top'], 'TB evidence contract required')
    require(spec['verification']['reference_origin'] and spec['verification']['test_origin'], 'verification provenance required')
    require(spec['verification'].get('minimum_checks', 0) > 0, 'minimum_checks required')
    return identity(spec)

def review(spec_path, reviewer, expected_hash, demo=False):
    spec = read(spec_path)
    sha = validate_spec(spec)
    require(sha == expected_hash, 'spec hash differs from reviewed content')
    require(not spec['open_questions'], 'unresolved specification questions')
    require(reviewer.strip(), 'reviewer required')
    rec = dict(schema='s2g.review.v1', spec_hash=sha, reviewer=reviewer,
               scope='builtin_demo' if demo else 'user_attestation', reviewed_at=stamp())
    write(str(spec_path) + '.review.json', rec)
    return rec

def draft(requirements, out):
    text = Path(requirements).read_text()
    write(out, dict(schema='s2g.spec-draft.v1', status='NEEDS_SPECIFICATION', requirements_text=text,
                    source_sha256=digest(requirements), required_fields=list(REQUIRED),
                    instructions='Complete s2g.spec.v1, attach independently sourced reference/TB; resolve questions; review the exact hash.'))
    return read(out)
