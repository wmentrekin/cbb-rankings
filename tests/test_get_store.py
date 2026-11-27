import pytest

from database import get_store
from database.d1 import D1Client
from database.local import LocalStore


def test_local(monkeypatch, tmp_path):
    monkeypatch.setenv("CBB_STORE", "local")
    monkeypatch.setenv("CBB_LOCAL_DB", str(tmp_path / "t.db"))
    assert isinstance(get_store(), LocalStore)


def test_d1_default(monkeypatch):
    monkeypatch.delenv("CBB_STORE", raising=False)
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a")
    monkeypatch.setenv("CLOUDFLARE_D1_DATABASE_ID", "d")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")
    assert isinstance(get_store(), D1Client)


def test_unknown(monkeypatch):
    monkeypatch.setenv("CBB_STORE", "pg")
    with pytest.raises(ValueError):
        get_store()
