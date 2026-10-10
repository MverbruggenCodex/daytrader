"""Run the test suite without pytest installed: python tests/run_tests.py

Uses pytest when available; otherwise a tiny stand-in that supports what these tests need.
"""
import inspect
import math
import os
import sys
import tempfile
import traceback
import types
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

try:
    import pytest  # noqa: F401
    sys.exit(pytest.main([HERE, "-q"]))
except ImportError:
    pass


class _Approx:
    def __init__(self, expected, rel=1e-6, abs_=1e-9):
        self.expected, self.rel, self.abs = expected, rel, abs_

    def __eq__(self, other):
        if isinstance(self.expected, (tuple, list)):
            return len(other) == len(self.expected) and all(
                math.isclose(a, b, rel_tol=self.rel, abs_tol=self.abs) for a, b in zip(other, self.expected))
        return math.isclose(other, self.expected, rel_tol=self.rel, abs_tol=self.abs)

    def __repr__(self):
        return f"approx({self.expected})"


shim = types.ModuleType("pytest")
shim.approx = lambda expected, rel=1e-6, abs=1e-9: _Approx(expected, rel, abs)
sys.modules["pytest"] = shim

import test_trader  # noqa: E402

failed = 0
tests = [(n, f) for n, f in vars(test_trader).items() if n.startswith("test_") and callable(f)]
for name, fn in tests:
    kwargs = {}
    if "tmp_path" in inspect.signature(fn).parameters:
        kwargs["tmp_path"] = Path(tempfile.mkdtemp())
    try:
        fn(**kwargs)
        print(f"PASS {name}")
    except Exception:
        failed += 1
        print(f"FAIL {name}")
        traceback.print_exc()
print(f"\n{len(tests) - failed}/{len(tests)} geslaagd")
sys.exit(1 if failed else 0)
