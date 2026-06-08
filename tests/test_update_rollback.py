import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
UPDATE_SH = REPO_ROOT / "scripts" / "update.sh"

pytestmark = pytest.mark.skipif(
    os.name == "nt" and not Path("/bin/sh").exists(),
    reason="needs a POSIX shell (run in the Linux container or WSL/git-bash)",
)


def _make_stub(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _init_repo(root: Path) -> str:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    (root / "marker.txt").write_text("v1", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "v1"], cwd=root, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()


def _run_update(root: Path, ready_exit: str):
    """Run update.sh with stubbed git/docker/curl. `ready_exit` is the shell
    exit code the curl stub returns for the /ready probe (0 = healthy)."""
    bindir = root / "stubbin"
    bindir.mkdir()
    # git: real git for rev-parse/checkout, but 'pull' is a no-op that advances HEAD.
    _make_stub(bindir / "git", f"""
real_git() {{ command "{_which_git()}" "$@"; }}
case "$1" in
  pull) real_git commit --allow-empty -qm "pulled"; ;;
  fetch) : ;;
  *) real_git "$@" ;;
esac
""")
    _make_stub(bindir / "docker", "exit 0\n")
    # curl returns ready_exit for the health probe.
    _make_stub(bindir / "curl", f"exit {ready_exit}\n")

    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}" + env["PATH"]
    env["DOCUMENT_SEARCH_SKIP_BACKUP"] = "1"  # don't invoke python backup in tests
    return subprocess.run(
        ["/bin/sh", str(UPDATE_SH)],
        cwd=root, env=env, capture_output=True, text=True,
    )


def _which_git() -> str:
    return subprocess.run(
        ["sh", "-c", "command -v git"], capture_output=True, text=True
    ).stdout.strip()


def _copy_update_script(root: Path) -> None:
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "scripts" / "update.sh").write_text(
        UPDATE_SH.read_text(encoding="utf-8"), encoding="utf-8"
    )


def test_update_succeeds_when_ready_ok(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    start = _init_repo(root)
    _copy_update_script(root)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "add script"], cwd=root, check=True)
    res = _run_update(root, ready_exit="0")
    assert res.returncode == 0, res.stderr + res.stdout
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True
    ).stdout.strip()
    # HEAD advanced past the starting commit (the pull added a commit).
    assert head != start


def test_update_rolls_back_when_ready_fails(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _init_repo(root)
    _copy_update_script(root)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "add script"], cwd=root, check=True)
    pre_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True
    ).stdout.strip()

    res = _run_update(root, ready_exit="1")  # /ready never becomes healthy
    # Non-zero exit signals the rollback path ran and reported failure.
    assert res.returncode != 0, res.stdout
    assert "rollback" in (res.stdout + res.stderr).lower()
    # HEAD must be back at the pre-update commit.
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True
    ).stdout.strip()
    assert head == pre_commit
