# Releasing

The package goes to PyPI as `theseus-mcp`; the Claude Code plugin is the `plugin/` folder of this repository and is picked up from GitHub.

## Once

1. On PyPI, add a trusted publisher for this repository (Account settings → Publishing): owner and repository of the GitHub repo, workflow `publish.yml`, environment `pypi`. Do it as a "pending publisher" before the first release, while the project name doesn't exist yet.
2. On GitHub, create an environment named `pypi` (Settings → Environments). Add yourself as a required reviewer if you want to approve each upload.

## Each release

1. Raise `version` in `pyproject.toml`. If anything under `plugin/` changed, raise `version` in `plugin/.claude-plugin/plugin.json` too.
2. Run the tests, including the browser tests: `THESEUS_BROWSER_TESTS=1 python3 -m pytest tests`.
3. Commit, tag `vX.Y.Z`, push, and publish a GitHub release from the tag. The `Publish to PyPI` workflow builds the sdist and wheel and uploads them.
4. Check it from a clean machine: `uvx --refresh --from theseus-mcp theseus-doctor`.
