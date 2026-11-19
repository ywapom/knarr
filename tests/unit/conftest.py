"""Keep the unit tests independent of the developer's KNARR_* settings
(KNARR_NO_SANDBOX, KNARR_CDP_URL, KNARR_WRITE_ALLOW and the rest) and of
the site profiles in their ~/.knarr/sites."""

import os

import pytest


@pytest.fixture(autouse=True)
def _clean_knarr_env(monkeypatch, tmp_path_factory):
    for name in list(os.environ):
        if name.startswith("KNARR_"):
            monkeypatch.delenv(name, raising=False)
    # A directory that doesn't exist: no profiles, unless a test makes some.
    monkeypatch.setenv("KNARR_PROFILES_DIR", str(tmp_path_factory.getbasetemp() / "no-site-profiles"))
