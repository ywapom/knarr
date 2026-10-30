"""Theseus: an AI agent explores a website toward a goal with guarded browser tools.

``theseus.explorer`` is the core: the tool palette, the guards (URL boundary,
forbidden actions, write block), limits, trace, login borrowing and site profiles.
``theseus.mcp`` serves those tools to an MCP client such as Claude Code.
``theseus.doctor`` checks that everything can run on this machine.
"""
