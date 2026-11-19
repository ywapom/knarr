"""Knarr: an AI agent explores a website toward a goal with guarded browser tools.

``knarr.explorer`` is the core: the tool palette, the guards (URL boundary,
forbidden actions, write block), limits, trace, login borrowing and site profiles.
``knarr.mcp`` serves those tools to an MCP client such as Claude Code.
``knarr.doctor`` checks that everything can run on this machine.
"""
