"""Import boundary: the package imports only the standard library, itself and
a short list of third-party libraries.

These tests parse every module, lazy (in-function) imports included, so a
dependency can't slip in through a code path the other tests never run.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

ALLOWED_THIRD_PARTY = {
    "mcp",           # MCP server (theseus.mcp)
    "playwright",
    "pytest",
    "websockets",    # login borrowing over DevTools, and the write-block integration test's server
    "yaml",
}
ALLOWED_FIRST_PARTY = {"theseus", "tests"}


def _python_files():
    for root in ("theseus", "tests"):
        yield from sorted((REPO_ROOT / root).rglob("*.py"))


def _absolute_imports(path: Path):
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.lineno, node.module


def test_no_module_imports_outside_the_allowlist():
    allowed = set(sys.stdlib_module_names) | ALLOWED_THIRD_PARTY | ALLOWED_FIRST_PARTY
    offenders = []
    for path in _python_files():
        for lineno, module in _absolute_imports(path):
            if module.split(".")[0] not in allowed:
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {module}")
    assert not offenders, "imports outside the allowlist:\n" + "\n".join(offenders)


@pytest.mark.parametrize("source", [
    "import requests",
    "def f():\n    from somewhere.private import thing\n",
])
def test_scanner_sees_top_level_and_lazy_imports(tmp_path, source):
    path = tmp_path / "sample.py"
    path.write_text(source)
    tops = {module.split(".")[0] for _, module in _absolute_imports(path)}
    assert tops and not tops & ALLOWED_THIRD_PARTY
