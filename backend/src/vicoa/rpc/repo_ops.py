"""GitHub repositories for the new-session Git picker — list, branch, prepare.

The picker lets a session start on several repositories at once, each on a
branch of the user's choosing. Three steps, all daemon-side:

1. `github_repo_list` / `github_branch_list` — what the machine's `gh` login
   can see. Like `github_ops`, Vicoa stores no GitHub credential of its own:
   whatever `gh auth login` granted (a fine-grained token scoped to a few repos,
   a full OAuth login) is exactly what the picker offers.
2. `repo_prepare_start` — for each pick, clone the repo once into a
   daemon-managed base (`~/vicoa/repos/<owner>/<name>`, created without a
   checkout, HEAD detached so no branch is ever "checked out" there), fetch,
   and add a *per-session* worktree on the chosen branch — or on a new branch
   cut from it. Worktrees land under the same `~/vicoa/workspaces` root the
   spawn-time worktree feature uses, so they show up in, and are removable
   from, the existing worktree UI. A clone easily outlives the 30s RPC
   timeout, so this runs in a background thread and returns a job id.
3. `repo_prepare_status` — the client polls the job; once `done` it spawns the
   session with the primary repo as `directory` and the rest as
   `additional_directories`.

Every argv is built here. The RPC surface carries only a repo's
`owner/name` and branch names, both validated before they reach a command, so a
client cannot turn the user's GitHub credential into arbitrary `gh`/git calls.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from vicoa.rpc.worktree_names import disambiguate
from vicoa.rpc.worktree_paths import worktrees_parent_dir

_GH_TIMEOUT_SECONDS = 25
_CLONE_TIMEOUT_SECONDS = 900
_FETCH_TIMEOUT_SECONDS = 300
_LOCAL_GIT_TIMEOUT_SECONDS = 60

# Listing is called every time the picker opens; `gh api --paginate` over a
# few hundred repos takes seconds, so one result is reused for a short while.
_REPO_LIST_TTL_SECONDS = 60.0

# A finished job is kept this long for the client to read, then dropped.
_JOB_TTL_SECONDS = 3600.0

_MAX_REPOS_PER_JOB = 10

# GitHub's own rules: owner is alnum + `-`; a repo name also allows `.` and `_`.
_FULL_NAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")

# `gh`'s credential helper, applied per command so network git works even on
# a machine where `gh auth setup-git` was never run. The empty helper first
# clears any inherited helper that might prompt.
_GH_CREDENTIAL_ARGS = (
    "-c",
    "credential.helper=",
    "-c",
    "credential.helper=!gh auth git-credential",
)


def repos_root() -> Path:
    """`~/vicoa/repos` — read at call time so HOME is honoured in tests."""
    return Path.home() / "vicoa" / "repos"


def base_clone_dir(full_name: str) -> Path:
    owner, name = full_name.split("/", 1)
    return repos_root() / owner / name


def _clone_url(full_name: str) -> str:
    return f"https://github.com/{full_name}.git"


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env["GH_NO_UPDATE_NOTIFIER"] = "1"
    env["GH_PROMPT_DISABLED"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _run(
    argv: list[str], *, cwd: Path | None = None, timeout: float
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=str(cwd) if cwd else str(Path.home()),
        capture_output=True,
        check=False,
        text=True,
        env=_env(),
        timeout=timeout,
    )


def _git(
    repo: Path, *args: str, timeout: float = _LOCAL_GIT_TIMEOUT_SECONDS
) -> subprocess.CompletedProcess[str]:
    return _run(["git", "-C", str(repo), *args], timeout=timeout)


def _git_net(
    repo: Path | None, *args: str, timeout: float
) -> subprocess.CompletedProcess[str]:
    prefix = ["git", *_GH_CREDENTIAL_ARGS]
    if repo is not None:
        prefix += ["-C", str(repo)]
    return _run([*prefix, *args], timeout=timeout)


def _classify_gh_error(stderr: str) -> str:
    lowered = stderr.lower()
    if "gh auth login" in lowered or "not logged in" in lowered or "401" in lowered:
        return "gh_unauthenticated"
    if "404" in lowered or "not found" in lowered:
        return "not_found"
    return "gh_unavailable"


def _gh_api_pages(path: str) -> tuple[list[Any] | None, str | None]:
    """GET every page of a list endpoint; `(items, None)` or `(None, error)`.

    `--slurp` wraps the pages in one outer array, so the output is one JSON
    document however many pages there were.
    """
    if shutil.which("gh") is None:
        return None, "gh_missing"
    try:
        proc = _run(
            ["gh", "api", "--paginate", "--slurp", "-X", "GET", path],
            timeout=_GH_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return None, "gh_unavailable"
    if proc.returncode != 0:
        return None, _classify_gh_error(proc.stderr or proc.stdout)
    try:
        pages = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return None, "gh_unavailable"
    items: list[Any] = []
    for page in pages if isinstance(pages, list) else []:
        if isinstance(page, list):
            items.extend(page)
    return items, None


_repo_list_cache: tuple[float, list[dict[str, Any]]] | None = None
_repo_list_lock = threading.Lock()


def github_repo_list(refresh: bool = False) -> dict[str, Any]:
    """Repositories the machine's `gh` login can access, most recently pushed first.

    Returns `{"repos": [{full_name, private, default_branch, description,
    pushed_at, can_push}]}` or `{"error": "gh_missing" | "gh_unauthenticated" |
    "gh_unavailable"}`.
    """
    global _repo_list_cache
    with _repo_list_lock:
        now = time.monotonic()
        if (
            not refresh
            and _repo_list_cache is not None
            and now - _repo_list_cache[0] < _REPO_LIST_TTL_SECONDS
        ):
            return {"repos": _repo_list_cache[1]}

        items, error = _gh_api_pages("user/repos?per_page=100&sort=pushed")
        if error is not None or items is None:
            return {"error": error or "gh_unavailable"}

        repos: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("full_name"), str):
                continue
            permissions = item.get("permissions") or {}
            repos.append(
                {
                    "full_name": item["full_name"],
                    "private": bool(item.get("private")),
                    "default_branch": item.get("default_branch") or "main",
                    "description": item.get("description") or "",
                    "pushed_at": item.get("pushed_at"),
                    "can_push": bool(permissions.get("push")),
                    "cloned": base_clone_dir(item["full_name"]).is_dir()
                    if _FULL_NAME_RE.match(item["full_name"])
                    else False,
                }
            )
        repos.sort(key=lambda r: r.get("pushed_at") or "", reverse=True)
        _repo_list_cache = (now, repos)
        return {"repos": repos}


def github_branch_list(full_name: str) -> dict[str, Any]:
    """Branches of `owner/name`: `{"default_branch", "branches": [names]}`.

    The default branch is listed first, the rest alphabetically.
    """
    if not isinstance(full_name, str) or not _FULL_NAME_RE.match(full_name):
        return {"error": "invalid_repo"}
    items, error = _gh_api_pages(f"repos/{full_name}/branches?per_page=100")
    if error is not None or items is None:
        return {"error": error or "gh_unavailable"}
    names = sorted(
        {
            item["name"]
            for item in items
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
    )

    default_branch = ""
    try:
        proc = _run(
            ["gh", "api", f"repos/{full_name}", "--jq", ".default_branch"],
            timeout=_GH_TIMEOUT_SECONDS,
        )
        if proc.returncode == 0:
            default_branch = proc.stdout.strip()
    except subprocess.TimeoutExpired:
        pass
    if default_branch in names:
        names.remove(default_branch)
        names.insert(0, default_branch)
    return {
        "default_branch": default_branch or (names[0] if names else ""),
        "branches": names,
    }


def _is_valid_branch_name(name: str) -> bool:
    if not name or name.startswith("-"):
        return False
    proc = subprocess.run(
        ["git", "check-ref-format", "--branch", name],
        capture_output=True,
        check=False,
    )
    return proc.returncode == 0


def _ref_exists(repo: Path, ref: str) -> bool:
    return _git(repo, "rev-parse", "--verify", "--quiet", ref).returncode == 0


def _branch_checked_out_at(repo: Path, branch: str) -> str | None:
    """The worktree path where local `branch` is checked out, if any."""
    proc = _git(repo, "worktree", "list", "--porcelain")
    if proc.returncode != 0:
        return None
    current_path: str | None = None
    for line in proc.stdout.splitlines():
        if line.startswith("worktree "):
            current_path = line[len("worktree ") :]
        elif line == f"branch refs/heads/{branch}":
            return current_path
    return None


def _ensure_base_clone(full_name: str) -> Path:
    """Clone `full_name` into its managed base dir if needed, then fetch.

    The base is cloned with `--no-checkout` and HEAD detached, so it holds only
    objects and refs: every real checkout is a worktree, and no branch is ever
    pinned by the base itself.
    """
    base = base_clone_dir(full_name)
    if not (base / ".git").exists():
        base.parent.mkdir(parents=True, exist_ok=True)
        if base.exists():
            # A previous clone died half-way; start over.
            shutil.rmtree(base)
        proc = _git_net(
            None,
            "clone",
            "--no-checkout",
            "--",
            _clone_url(full_name),
            str(base),
            timeout=_CLONE_TIMEOUT_SECONDS,
        )
        if proc.returncode != 0:
            shutil.rmtree(base, ignore_errors=True)
            raise RuntimeError(
                f"clone failed: {proc.stderr.strip() or proc.returncode}"
            )
        head = _git(base, "rev-parse", "HEAD")
        if head.returncode == 0 and head.stdout.strip():
            _git(base, "update-ref", "--no-deref", "HEAD", head.stdout.strip())
    else:
        proc = _git_net(
            base, "fetch", "--prune", "origin", timeout=_FETCH_TIMEOUT_SECONDS
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"fetch failed: {proc.stderr.strip() or proc.returncode}"
            )
    return base


def _worktree_path(base: Path, branch: str) -> Path:
    parent = worktrees_parent_dir(base)
    parent.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", branch).strip("-.") or "branch"
    return parent / disambiguate(parent, slug) / base.name


def _prepare_one(full_name: str, branch: str, new_branch: str | None) -> dict[str, Any]:
    base = _ensure_base_clone(full_name)
    remote_ref = f"refs/remotes/origin/{branch}"
    if not _ref_exists(base, remote_ref):
        raise RuntimeError(f"branch '{branch}' not found on origin")

    if new_branch:
        if _ref_exists(base, f"refs/heads/{new_branch}"):
            raise RuntimeError(f"branch '{new_branch}' already exists")
        path = _worktree_path(base, new_branch)
        proc = _git(
            base,
            "worktree",
            "add",
            "--no-track",
            "-b",
            new_branch,
            str(path),
            remote_ref,
        )
        checked_out = new_branch
    else:
        in_use = _branch_checked_out_at(base, branch)
        if in_use:
            raise RuntimeError(
                f"branch '{branch}' is already open in another session ({in_use}); "
                "pick a new branch instead"
            )
        path = _worktree_path(base, branch)
        if _ref_exists(base, f"refs/heads/{branch}"):
            proc = _git(base, "worktree", "add", str(path), branch)
            if proc.returncode == 0:
                # Bring a stale local branch up to date; a diverged one is
                # left alone rather than rewritten.
                _git(path, "merge", "--ff-only", remote_ref)
        else:
            proc = _git(
                base, "worktree", "add", "--track", "-b", branch, str(path), remote_ref
            )
        checked_out = branch
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "worktree_add_failed")
    return {"path": str(path), "branch": checked_out, "repo_root": str(base)}


def _remove_worktree(repo_root: str, path: str) -> None:
    _git(Path(repo_root), "worktree", "remove", "--force", path)


_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.Lock()


def _prune_jobs() -> None:
    cutoff = time.time() - _JOB_TTL_SECONDS
    for job_id in [
        j for j, job in _jobs.items() if job.get("finished_at", time.time()) < cutoff
    ]:
        _jobs.pop(job_id, None)


def _validate_picks(repos: Any) -> list[dict[str, Any]]:
    if not isinstance(repos, list) or not repos:
        raise ValueError("repos must be a non-empty list")
    if len(repos) > _MAX_REPOS_PER_JOB:
        raise ValueError("too many repositories")
    picks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in repos:
        if not isinstance(raw, dict):
            raise ValueError("invalid repository entry")
        full_name = raw.get("full_name")
        branch = raw.get("branch")
        new_branch = raw.get("new_branch") or None
        if not isinstance(full_name, str) or not _FULL_NAME_RE.match(full_name):
            raise ValueError(f"invalid repository: {full_name!r}")
        if full_name.lower() in seen:
            raise ValueError(f"repository listed twice: {full_name}")
        seen.add(full_name.lower())
        if not isinstance(branch, str) or not _is_valid_branch_name(branch):
            raise ValueError(f"invalid branch for {full_name}: {branch!r}")
        if new_branch is not None and (
            not isinstance(new_branch, str)
            or not _is_valid_branch_name(new_branch.strip())
        ):
            raise ValueError(f"invalid new branch for {full_name}: {new_branch!r}")
        picks.append(
            {
                "full_name": full_name,
                "branch": branch,
                "new_branch": new_branch.strip()
                if isinstance(new_branch, str)
                else None,
            }
        )
    return picks


def _run_job(job_id: str, picks: list[dict[str, Any]]) -> None:
    created: list[dict[str, Any]] = []
    for index, pick in enumerate(picks):
        with _jobs_lock:
            _jobs[job_id]["repos"][index]["state"] = "running"
        try:
            result = _prepare_one(pick["full_name"], pick["branch"], pick["new_branch"])
        except Exception as exc:  # noqa: BLE001 - reported to the client
            message = str(exc)
            if isinstance(exc, subprocess.TimeoutExpired):
                message = "timed out"
            # All-or-nothing: a session with half its repos is not what was asked
            # for, so undo the worktrees this job already added.
            for done in created:
                _remove_worktree(done["repo_root"], done["path"])
            with _jobs_lock:
                job = _jobs[job_id]
                job["repos"][index].update(state="error", error=message)
                for later in job["repos"][index + 1 :]:
                    later["state"] = "skipped"
                for earlier in job["repos"][:index]:
                    earlier.update(state="rolled_back", path=None)
                job.update(
                    state="error",
                    error=f"{pick['full_name']}: {message}",
                    finished_at=time.time(),
                )
            return
        created.append(result)
        with _jobs_lock:
            _jobs[job_id]["repos"][index].update(state="done", **result)
    with _jobs_lock:
        _jobs[job_id].update(state="done", finished_at=time.time())


def repo_prepare_start(repos: Any) -> dict[str, Any]:
    """Start cloning/fetching `repos` and adding a worktree for each.

    `repos` is `[{full_name, branch, new_branch?}]` — `branch` is an existing
    remote branch; with `new_branch` the worktree gets that new branch cut from
    `branch`. Returns `{"job_id"}`; poll `repo_prepare_status`.
    """
    try:
        picks = _validate_picks(repos)
    except ValueError as exc:
        return {"error": str(exc)}
    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _prune_jobs()
        _jobs[job_id] = {
            "job_id": job_id,
            "state": "running",
            "repos": [
                {
                    "full_name": p["full_name"],
                    "branch": p["new_branch"] or p["branch"],
                    "base_branch": p["branch"],
                    "state": "pending",
                }
                for p in picks
            ],
        }
    threading.Thread(target=_run_job, args=(job_id, picks), daemon=True).start()
    return {"job_id": job_id}


def repo_prepare_status(job_id: str) -> dict[str, Any]:
    """`{job_id, state: running|done|error, error?, repos: [{full_name,
    branch, base_branch, state, path?, repo_root?, error?}]}`."""
    with _jobs_lock:
        job = _jobs.get(job_id) if isinstance(job_id, str) else None
        if job is None:
            return {"error": "unknown_job"}
        return json.loads(json.dumps(job))
