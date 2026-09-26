"""`vicoa.rpc.repo_ops` — the new-session Git picker's daemon side.

A local repo stands in for GitHub (`_clone_url` is patched to its path), so
the clone / fetch / worktree logic runs against real git with no network.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from vicoa.machine_daemon import MachineDaemon
from vicoa.rpc import repo_ops


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    monkeypatch.setenv("HOME", str(home_dir))
    monkeypatch.setenv("USERPROFILE", str(home_dir))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home_dir))
    return home_dir


@pytest.fixture
def origin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An 'upstream' repo with `main` and `develop`, served as the clone URL."""
    repo = tmp_path / "origin"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "readme.md").write_text("main\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "checkout", "-q", "-b", "develop")
    (repo / "dev.txt").write_text("dev\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "dev")
    _git(repo, "checkout", "-q", "main")
    monkeypatch.setattr(repo_ops, "_clone_url", lambda _full_name: str(repo))
    return repo


def _wait(job_id: str) -> dict[str, Any]:
    deadline = time.time() + 60
    while time.time() < deadline:
        status = repo_ops.repo_prepare_status(job_id)
        if status.get("state") != "running":
            return status
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_prepare_existing_branch_creates_worktree(home: Path, origin: Path):
    started = repo_ops.repo_prepare_start(
        [{"full_name": "acme/app", "branch": "develop"}]
    )
    status = _wait(started["job_id"])

    assert status["state"] == "done", status
    entry = status["repos"][0]
    path = Path(entry["path"])
    assert entry["branch"] == "develop"
    assert (path / "dev.txt").read_text() == "dev\n"
    assert _git(path, "rev-parse", "--abbrev-ref", "HEAD") == "develop"
    # Base clone lives under the managed root and pins no branch.
    base = home / "vicoa" / "repos" / "acme" / "app"
    assert Path(entry["repo_root"]) == base
    assert _git(base, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
    assert str(path).startswith(str(home / "vicoa" / "workspaces"))


def test_prepare_new_branch_from_base(home: Path, origin: Path):
    started = repo_ops.repo_prepare_start(
        [{"full_name": "acme/app", "branch": "main", "new_branch": "feature/x"}]
    )
    status = _wait(started["job_id"])

    assert status["state"] == "done", status
    path = Path(status["repos"][0]["path"])
    assert _git(path, "rev-parse", "--abbrev-ref", "HEAD") == "feature/x"
    assert not (path / "dev.txt").exists()
    assert path.parent.name == "feature-x"


def test_same_branch_twice_is_refused_but_default_branch_is_free(
    home: Path, origin: Path
):
    # `main` is the clone's default branch: the detached base must not pin it.
    first = _wait(
        repo_ops.repo_prepare_start([{"full_name": "acme/app", "branch": "main"}])[
            "job_id"
        ]
    )
    assert first["state"] == "done", first

    second = _wait(
        repo_ops.repo_prepare_start([{"full_name": "acme/app", "branch": "main"}])[
            "job_id"
        ]
    )
    assert second["state"] == "error"
    assert "already open" in second["repos"][0]["error"]


def test_failure_rolls_back_earlier_repos(home: Path, origin: Path):
    status = _wait(
        repo_ops.repo_prepare_start(
            [
                {"full_name": "acme/app", "branch": "develop"},
                {"full_name": "acme/other", "branch": "nope"},
            ]
        )["job_id"]
    )
    assert status["state"] == "error"
    assert status["repos"][0]["state"] == "rolled_back"
    worktrees = _git(home / "vicoa" / "repos" / "acme" / "app", "worktree", "list")
    assert "develop" not in worktrees


@pytest.mark.parametrize(
    "repos",
    [
        [],
        [{"full_name": "not-a-repo", "branch": "main"}],
        [{"full_name": "acme/app", "branch": "--upload-pack=evil"}],
        [{"full_name": "acme/app", "branch": "main", "new_branch": "bad..name"}],
        [
            {"full_name": "acme/app", "branch": "main"},
            {"full_name": "ACME/app", "branch": "main"},
        ],
    ],
)
def test_invalid_picks_are_rejected_before_any_work(home: Path, repos: list[Any]):
    result = repo_ops.repo_prepare_start(repos)
    assert "error" in result
    assert not (home / "vicoa").exists()


def test_branch_list_rejects_bad_repo_name():
    assert repo_ops.github_branch_list("../etc") == {"error": "invalid_repo"}


def test_unknown_job():
    assert repo_ops.repo_prepare_status("nope") == {"error": "unknown_job"}


def test_repo_methods_are_advertised_and_dispatched(home: Path):
    daemon = MachineDaemon(api_key="test-key", base_url="http://localhost:0")
    methods = daemon._supported_rpc_methods()
    for method in (
        "github-repo-list",
        "github-branch-list",
        "repo-prepare",
        "repo-prepare-status",
    ):
        assert method in methods
    assert "session-repos" in daemon._capabilities()
    result = daemon._handle_rpc_request(
        {"method": "repo-prepare-status", "params": {"job_id": "nope"}}
    )
    assert result == {"error": "unknown_job"}


def test_additional_directories_become_add_dir_for_claude_only(tmp_path: Path):
    daemon = MachineDaemon(api_key="test-key", base_url="http://localhost:0")
    extra = tmp_path / "frontend"
    extra.mkdir()
    dirs = daemon._parse_additional_directories([str(extra)])
    assert dirs == [str(extra.resolve())]

    claude_cmd = daemon._build_headless_command(
        directory=str(tmp_path), agent="claude", session_id="s", add_dirs=dirs
    )
    idx = claude_cmd.index("--add-dir")
    assert claude_cmd[idx + 1] == dirs[0]

    codex_cmd = daemon._build_headless_command(
        directory=str(tmp_path), agent="codex", session_id="s", add_dirs=dirs
    )
    assert "--add-dir" not in codex_cmd


def test_additional_directories_validation(tmp_path: Path):
    daemon = MachineDaemon(api_key="test-key", base_url="http://localhost:0")
    assert daemon._parse_additional_directories(None) == []
    with pytest.raises(ValueError):
        daemon._parse_additional_directories("not-a-list")
    with pytest.raises(ValueError):
        daemon._parse_additional_directories([str(tmp_path / "missing")])
