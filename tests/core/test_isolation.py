"""The core must stay free of Home Assistant so the backtest can load it."""

from __future__ import annotations

import ast
from pathlib import Path

CORE = Path(__file__).parents[2] / "custom_components" / "home_eta" / "core"


def test_core_imports_only_stdlib_and_itself() -> None:
    files = sorted(CORE.glob("*.py"))
    assert files
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level > 0:
                    continue  # relative import within core
                names = [node.module or ""]
            else:
                continue
            for name in names:
                assert not name.startswith(("homeassistant", "custom_components")), (
                    f"{path.name} imports {name}"
                )
