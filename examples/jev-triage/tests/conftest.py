"""All authored tests default to explicit offline mode, even when keys are present."""

import pytest


@pytest.fixture(scope="session", autouse=True)
def offline_tests():
    """Prevent smoke tests from contacting a provider using ambient credentials."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("JEV_TRIAGE_OFFLINE", "true")
        yield
