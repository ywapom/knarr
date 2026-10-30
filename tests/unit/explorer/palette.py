"""The explorer's tool palette as plain functions, for tests.

Gives each function the ``.name`` / ``.run(args)`` surface the tool tests
use, so they run without any model client's tool wrapper.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List

from theseus.explorer.explorer_tools import ExplorerContext, build_explorer_functions


class PlainTool:
    def __init__(self, fn: Callable[..., str]) -> None:
        self.name = fn.__name__
        self._fn = fn

    def run(self, args: Dict[str, Any]) -> str:
        return self._fn(**args)


def build_palette(context: ExplorerContext) -> List[PlainTool]:
    return [PlainTool(fn) for fn in build_explorer_functions(context)]
