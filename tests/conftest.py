"""All test shells and logs stay in disposable test directories."""
import pytest
from arbitrium import session


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(session, "LOGS_DIR", tmp_path / "session-logs")
