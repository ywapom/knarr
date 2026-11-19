# Releasing

The package goes to PyPI as `knarr`; the Claude Code plugin is the `plugin/` folder of this repository and is picked up from GitHub.

## First release (claims the name)

A PyPI name belongs to whoever uploads the first release; a "pending" trusted publisher does not reserve it. So the first release goes up by hand:

1. On PyPI, create an API token (Account settings → API tokens, scope "Entire account" — a project-scoped token needs the project to exist).
2. From a clean checkout: `uv build`, then `uv publish` (paste the token when asked, or set `UV_PUBLISH_TOKEN`).
3. Check it: `uvx --refresh --from knarr knarr-doctor`.
4. On PyPI, open the new project → Settings → Publishing, and add a trusted publisher: this GitHub repository, workflow `publish.yml`, environment `pypi`. Then delete the API token.
5. On GitHub, create an environment named `pypi` (Settings → Environments). Add yourself as a required reviewer if you want to approve each upload.

## Each later release

1. Raise `version` in `pyproject.toml`. If anything under `plugin/` changed, raise `version` in `plugin/.claude-plugin/plugin.json` too.
2. Run the tests, including the browser tests: `KNARR_BROWSER_TESTS=1 python3 -m pytest tests`.
3. Commit, tag `vX.Y.Z`, push, and publish a GitHub release from the tag. The `Publish to PyPI` workflow builds the sdist and wheel and uploads them.
4. Check it from a clean machine: `uvx --refresh --from knarr knarr-doctor`.
