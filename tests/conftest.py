import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


@pytest.fixture()
def tmp_sessions(tmp_path, monkeypatch):
    """Isolate turn logs per test so nothing leaks between them or into the repo."""
    d = tmp_path / "sessions"
    d.mkdir()
    monkeypatch.setenv("VM_DIR", str(d))
    return str(d)
