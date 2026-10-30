"""Keep the unit tests independent of the developer's THESEUS_* settings
(THESEUS_NO_SANDBOX, THESEUS_CDP_URL, THESEUS_WRITE_ALLOW and the rest) and of
the site profiles in their ~/.theseus/sites."""

import os

import pytest


@pytest.fixture(autouse=True)
def _clean_theseus_env(monkeypatch, tmp_path_factory):
    for name in list(os.environ):
        if name.startswith("THESEUS_"):
            monkeypatch.delenv(name, raising=False)
    # A directory that doesn't exist: no profiles, unless a test makes some.
    monkeypatch.setenv("THESEUS_PROFILES_DIR", str(tmp_path_factory.getbasetemp() / "no-site-profiles"))
