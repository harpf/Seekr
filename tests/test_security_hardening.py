import inspect
import io
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

import document_search.app as appmod
from document_search.app import create_app


def _login(client, username="admin", password="admin"):
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def test_create_user_duplicate_does_not_leak_exception_text(tmp_path):
    """Creating a duplicate user must return a generic message, NOT the raw
    sqlite3 IntegrityError / UNIQUE-constraint string."""
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _login(client)
        r1 = client.post(
            "/api/users",
            headers={"X-Auth-Token": token},
            json={"username": "dupe", "password": "password123", "role": "user"},
        )
        assert r1.status_code == 200, r1.text
        r2 = client.post(
            "/api/users",
            headers={"X-Auth-Token": token},
            json={"username": "dupe", "password": "password123", "role": "user"},
        )
        assert r2.status_code == 400
        detail = r2.json()["detail"].lower()
        for leak in ("unique", "constraint", "sqlite", "integrityerror", "traceback"):
            assert leak not in detail, f"leaked internal detail: {detail!r}"


def test_dockerfile_runs_as_non_root():
    """The Dockerfile must create a dedicated user and drop to it before CMD."""
    text = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "useradd" in text or "adduser" in text, "no user creation in Dockerfile"
    assert "USER seekr" in text, "Dockerfile does not switch to the seekr user"
    user_idx = text.index("USER seekr")
    cmd_idx = text.rindex("CMD")
    assert user_idx < cmd_idx, "USER must precede CMD"


def _upload(client, token, filename, content: bytes, subpath=""):
    return client.post(
        "/api/upload",
        headers={"X-Auth-Token": token},
        files={"file": (filename, io.BytesIO(content), "application/octet-stream")},
        data={"target_subpath": subpath},
    )


def test_upload_rejects_exe_renamed_to_pdf(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCUMENT_SEARCH_UPLOAD_ROOT", str(tmp_path / "uploads"))
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _login(client)
        exe_bytes = b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 64
        r = _upload(client, token, "report.pdf", exe_bytes)
        assert r.status_code == 400, r.text
        assert "match" in r.json()["detail"].lower() or "content" in r.json()["detail"].lower()


def test_upload_rejects_traversal_subpath(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCUMENT_SEARCH_UPLOAD_ROOT", str(tmp_path / "uploads"))
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _login(client)
        r = _upload(client, token, "note.txt", b"hello text", subpath="../../etc")
        assert r.status_code == 400, r.text
        assert "subpath" in r.json()["detail"].lower() or "invalid" in r.json()["detail"].lower()


def test_upload_accepts_real_text_file(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCUMENT_SEARCH_UPLOAD_ROOT", str(tmp_path / "uploads"))
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _login(client)
        r = _upload(client, token, "note.txt", b"a genuine plain text note\n")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "uploaded"


def test_security_sensitive_deps_are_bounded():
    """cryptography, PyYAML, pillow, jinja2, python-multipart must have an upper
    bound (a `<` constraint) so a surprise major bump can't land unreviewed."""
    text = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
    sensitive = ["cryptography", "PyYAML", "pillow", "jinja2", "python-multipart"]
    lines = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name = re.split(r"[<>=!~ ]", line, maxsplit=1)[0]
        lines[name.lower()] = line
    for dep in sensitive:
        assert dep.lower() in lines, f"{dep} missing from requirements.txt"
        assert "<" in lines[dep.lower()], f"{dep} has no upper bound: {lines[dep.lower()]!r}"


def test_lockfile_exists_and_pins_sensitive_deps():
    lock = REPO_ROOT / "requirements.lock"
    assert lock.exists(), "requirements.lock missing"
    text = lock.read_text(encoding="utf-8").lower()
    for dep in ("cryptography", "pyyaml", "pillow", "jinja2", "python-multipart"):
        assert dep in text, f"{dep} not pinned in requirements.lock"
    assert "==" in text


def test_mount_does_not_leak_credentials_in_argv(tmp_path, monkeypatch):
    """The mount subprocess argv must never contain the literal password;
    credentials must be passed via an -o credentials=<file> option instead."""
    captured = {}

    def fake_run(cmd, *a, **kw):
        captured["cmd"] = list(cmd)
        creds_file = None
        for i, tok in enumerate(cmd):
            if tok == "-o":
                opts = cmd[i + 1]
                for opt in opts.split(","):
                    if opt.startswith("credentials="):
                        creds_file = opt.split("=", 1)[1]
        captured["creds_file"] = creds_file
        if creds_file:
            captured["creds_contents"] = Path(creds_file).read_text(encoding="utf-8")

        class _P:
            returncode = 0
            stdout = ""
            stderr = ""
        return _P()

    monkeypatch.setattr(appmod.subprocess, "run", fake_run)
    monkeypatch.setattr(appmod.os, "name", "posix", raising=False)

    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _login(client)
        r = client.post(
            "/api/paths/mount",
            headers={"X-Auth-Token": token},
            json={
                "remote_path": "//server/share",
                "mount_point": str(tmp_path / "mnt"),
                "share_type": "smb",
                "username": "alice",
                "password": "SuperSecret123",
                "domain": "WORKGROUP",
            },
        )
        assert r.status_code == 200, r.text

    argv_joined = " ".join(captured["cmd"])
    assert "SuperSecret123" not in argv_joined, f"password leaked in argv: {captured['cmd']}"
    assert "password=SuperSecret123" not in argv_joined
    assert captured.get("creds_file"), "no credentials= file used"
    assert "SuperSecret123" in captured.get("creds_contents", "")


def test_mount_rejects_bad_remote_path(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod.os, "name", "posix", raising=False)
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _login(client)
        r = client.post(
            "/api/paths/mount",
            headers={"X-Auth-Token": token},
            json={
                "remote_path": "-o,attacker=1",
                "mount_point": str(tmp_path / "mnt"),
                "share_type": "smb",
            },
        )
        assert r.status_code == 400, r.text


def test_all_subprocess_run_calls_have_timeout():
    """Every subprocess.run(...) in app.py must pass a timeout= kwarg so a hung
    child cannot block a worker thread forever."""
    src = inspect.getsource(appmod)
    idx = 0
    offenders = []
    needle = "subprocess.run("
    while True:
        start = src.find(needle, idx)
        if start == -1:
            break
        depth = 0
        i = start + len(needle) - 1  # position of the '('
        end = None
        for j in range(i, len(src)):
            if src[j] == "(":
                depth += 1
            elif src[j] == ")":
                depth -= 1
                if depth == 0:
                    end = j
                    break
        call_text = src[start: (end + 1) if end else len(src)]
        if "timeout=" not in call_text:
            offenders.append(call_text[:80])
        idx = (end + 1) if end else start + len(needle)
    assert not offenders, f"subprocess.run without timeout: {offenders}"
