"""Y1: pin removal of dead imports introduced by FIX-2P-3 refactor.

After FIX-2P-3 replaced the three-call get_forecasts_batch pattern in
rebalancer.py with a single get_forecasts_for_city_local_window, the
old import lingered as dead code.  Removing it keeps the public-symbol
graph honest and avoids the "this looks load-bearing, don't touch"
trap on later edits.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _imported_names(module_path: Path) -> set[str]:
    tree = ast.parse(module_path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
    return names


def test_rebalancer_does_not_import_get_forecasts_batch() -> None:
    """Y1: get_forecasts_batch was replaced by get_forecasts_for_city_local_window
    in FIX-2P-3.  Importing it without using it confuses future readers."""
    imported = _imported_names(ROOT / "src" / "strategy" / "rebalancer.py")
    assert "get_forecasts_batch" not in imported, (
        "Y1: dead import resurfaced — FIX-2P-3 deleted every call site"
    )
    # Sanity: the replacement is still imported
    assert "get_forecasts_for_city_local_window" in imported
