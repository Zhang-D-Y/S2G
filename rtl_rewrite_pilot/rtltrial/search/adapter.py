"""Reuse the frozen arithmetic evaluator without modifying historical runners.

The legacy module has bare imports and process-global design configuration.
Load it under private names and use one design per process; CLI runs are locked.
"""
import importlib.util
import sys
from pathlib import Path

PILOT = Path(__file__).resolve().parents[2]
LEGACY = PILOT / 'experiments/arithmetic_stability'


def load(design):
    saved = {k: sys.modules.get(k) for k in ('guard', 'designs')}
    path = list(sys.path)
    try:
        for name in ('guard', 'designs', 'experiment'):
            spec = importlib.util.spec_from_file_location('_structure_' + name, LEGACY / (name + '.py'))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            if name in saved:
                sys.modules[name] = module
        module.configure(design, 1)
        return module
    finally:
        sys.path[:] = path
        for name, old in saved.items():
            if old is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old
