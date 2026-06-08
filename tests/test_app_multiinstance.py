import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from document_search.app import create_app
from document_search.index.sqlite_store import SqliteStore
from document_search.services.session_store import SqliteSessionStore


def _login(client, username="admin", password="admin"):
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def test_login_creates_persistent_session_row(tmp_path):
    db = tmp_path / "t.db"
    app = create_app(str(db))
    with TestClient(app) as client:
        token = _login(client)
        # A brand-new store on the same DB file (simulating another replica)
        # must see the session row.
        other = SqliteStore(db)
        ss = SqliteSessionStore(other)
        sess = ss.get(token)
        assert sess is not None
        assert sess["role"] == "admin"


def test_session_token_authorises_protected_route(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _login(client)
        r = client.get("/api/me", headers={"X-Auth-Token": token})
        assert r.status_code == 200
        assert r.json()["username"] == "admin"


def test_session_survives_app_restart(tmp_path):
    """A token minted by app instance #1 still works on instance #2 (same DB)."""
    db = tmp_path / "t.db"
    app1 = create_app(str(db))
    with TestClient(app1) as c1:
        token = _login(c1)
    app2 = create_app(str(db))
    with TestClient(app2) as c2:
        r = c2.get("/api/me", headers={"X-Auth-Token": token})
        assert r.status_code == 200
        assert r.json()["role"] == "admin"


def test_expired_session_is_rejected(tmp_path):
    db = tmp_path / "t.db"
    app = create_app(str(db))
    with TestClient(app) as client:
        token = _login(client)
        # Force the session to be expired directly in the shared table.
        s = SqliteStore(db)
        import time
        s.conn.execute(
            "UPDATE sessions SET expires_at=? WHERE token=?",
            (time.time() - 1, token),
        )
        s.conn.commit()
        r = client.get("/api/me", headers={"X-Auth-Token": token})
        assert r.status_code == 401


def test_rate_limit_blocks_after_ten_failures(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        # 10 wrong-password attempts → the 11th call is rate-limited (429).
        for _ in range(10):
            r = client.post("/api/login", json={"username": "admin", "password": "wrong"})
            assert r.status_code == 401, r.text
        r = client.post("/api/login", json={"username": "admin", "password": "wrong"})
        assert r.status_code == 429


def test_rate_limit_resets_after_window(tmp_path):
    db = tmp_path / "t.db"
    app = create_app(str(db))
    with TestClient(app) as client:
        for _ in range(10):
            client.post("/api/login", json={"username": "admin", "password": "wrong"})
        # Age the recorded attempts past the 5-minute window.
        s = SqliteStore(db)
        import time
        s.conn.execute(
            "UPDATE login_attempts SET attempted_at=?",
            (time.time() - 301,),
        )
        s.conn.commit()
        # A correct login must now succeed (no longer blocked).
        r = client.post("/api/login", json={"username": "admin", "password": "admin"})
        assert r.status_code == 200, r.text


def test_successful_login_clears_failures(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        for _ in range(9):
            client.post("/api/login", json={"username": "admin", "password": "wrong"})
        # 9 failures then a success clears the counter…
        assert client.post(
            "/api/login", json={"username": "admin", "password": "admin"}
        ).status_code == 200
        # …so a fresh batch of 9 failures still isn't blocked.
        for _ in range(9):
            r = client.post("/api/login", json={"username": "admin", "password": "wrong"})
            assert r.status_code == 401


def test_cors_headers_reflect_allowlist(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCUMENT_SEARCH_CORS_ORIGINS", "https://app.example.com")
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        # Allowed origin → echoed back.
        r = client.get(
            "/api/me",
            headers={"Origin": "https://app.example.com", "X-Auth-Token": "x"},
        )
        assert r.headers.get("access-control-allow-origin") == "https://app.example.com"


def test_cors_disallows_unlisted_origin(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCUMENT_SEARCH_CORS_ORIGINS", "https://app.example.com")
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        r = client.get(
            "/api/me",
            headers={"Origin": "https://evil.example.com", "X-Auth-Token": "x"},
        )
        # Unlisted origin gets no allow-origin header.
        assert r.headers.get("access-control-allow-origin") is None


def test_cors_default_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("DOCUMENT_SEARCH_CORS_ORIGINS", raising=False)
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        r = client.get(
            "/api/me",
            headers={"Origin": "https://anything.example.com", "X-Auth-Token": "x"},
        )
        assert r.headers.get("access-control-allow-origin") is None
