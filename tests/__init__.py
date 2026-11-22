import sys
from pathlib import Path

# src must come first: the tests exercise this checkout, never another installed copy of agyswap
_SRC = str(Path(__file__).resolve().parents[1] / "src")
sys.path[:] = [_SRC] + [p for p in sys.path if p != _SRC]
_loaded = sys.modules.get("agyswap")
if _loaded is not None and not str(getattr(_loaded, "__file__", "")).startswith(_SRC):
    for _name in [n for n in sys.modules if n == "agyswap" or n.startswith("agyswap.")]:
        del sys.modules[_name]

from tests import guard  # noqa: E402

guard.install()
