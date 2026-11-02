"""Run advisor regression tests with only its backend and the public SDK on sys.path."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "plugin-sdk" / "python"), str(Path(__file__).resolve().parents[1] / "backend")]
if __name__ == "__main__":
    suite = unittest.defaultTestLoader.discover(str(Path(__file__).parent), pattern=sys.argv[1] if len(sys.argv) > 1 else "test_*.py")
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    sys.exit(not result.wasSuccessful())
