"""Founder Alpha R14: READ-ONLY observation of the project's real GitHub state.

Three separate facts, deliberately kept apart so none of them can impersonate another:

  `remote`   — what GitHub actually returned, and when (durable snapshot, migration 0089);
  `local`    — what the checkout on this machine says (optional `LOCAL_REPO_PATH`, read-only git);
  `error`    — why the most recent read failed, if it did.

Honesty rules (these are the product):

  - a failed refresh NEVER overwrites the last verified picture. The old snapshot is served
    with `stale: true` and its age, and the failure is reported alongside it;
  - missing credentials are a VALUE (`configured: false`), not an error and not a guess — no
    head SHA, no PR count, no "0 unpushed";
  - the local branch is compared against its OWN upstream, never a hardcoded `origin/main`; if
    no upstream ref exists the comparison is reported as UNKNOWN (`compare_ref: null`), not 0;
  - nothing here writes to GitHub. There is no push, merge or comment path in this module.
"""

from __future__ import annotations

import logging
import subprocess
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.integrations.github_client import GitHubClient
from app.models.github_status import GithubStatusSnapshot

logger = logging.getLogger(__name__)

_MAX_COMMITS = 8
_MAX_PRS = 10
_MAX_CHECKS_NAMED = 5
_GIT_TIMEOUT_SECONDS = 5


class GithubStatusNotConfigured(RuntimeError):
    pass


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() + "Z" if value else None


async def fetch_remote_status() -> dict[str, Any]:
    """Exactly what GitHub returned, with no interpretation. Raises on failure."""
    settings = get_settings()
    client = GitHubClient()
    if not client.is_configured():
        raise GithubStatusNotConfigured("GITHUB_TOKEN och/eller GITHUB_REPO är inte satta.")

    branch = settings.github_tracked_branch
    commits = await client.list_commits(sha=branch, per_page=_MAX_COMMITS) if branch else await client.list_commits(per_page=_MAX_COMMITS)

    head = commits[0] if commits else None
    head_sha = (head or {}).get("sha")
    checks_raw = await client.list_check_runs(head_sha) if head_sha else []
    conclusions = [str(c.get("conclusion") or c.get("status") or "").lower() for c in checks_raw]
    prs_raw = await client.list_pull_requests(state="open", per_page=_MAX_PRS)
    issues_raw = await client.list_issues(state="open", per_page=1)

    return {
        "branch": branch,
        "head_sha": head_sha,
        "head_commit": (
            {
                "subject": ((head.get("commit") or {}).get("message") or "").splitlines()[0][:200],
                "author": ((head.get("commit") or {}).get("author") or {}).get("name"),
                "committed_at": ((head.get("commit") or {}).get("author") or {}).get("date"),
            }
            if head
            else None
        ),
        "recent_commits": [
            {
                "sha": c.get("sha"),
                "subject": ((c.get("commit") or {}).get("message") or "").splitlines()[0][:200],
            }
            for c in commits[:_MAX_COMMITS]
        ],
        "open_prs": [
            {
                "number": p.get("number"),
                "title": (p.get("title") or "")[:200],
                "draft": bool(p.get("draft")),
                "head": ((p.get("head") or {}).get("ref")),
                "base": ((p.get("base") or {}).get("ref")),
                "updated_at": p.get("updated_at"),
            }
            for p in prs_raw[:_MAX_PRS]
        ],
        "open_issue_count": len(issues_raw),
        "checks": {
            "total": len(checks_raw),
            "passing": sum(1 for c in conclusions if c == "success"),
            "failing": sum(1 for c in conclusions if c in ("failure", "timed_out", "cancelled", "action_required")),
            "pending": sum(1 for c in conclusions if c in ("queued", "in_progress", "waiting", "requested", "pending")),
            "named": [
                {"name": (c.get("name") or "")[:80], "conclusion": c.get("conclusion") or c.get("status")}
                for c in checks_raw[:_MAX_CHECKS_NAMED]
            ],
        },
        "fetched_at": datetime.utcnow().isoformat() + "Z",
    }


def _git(repo: str, *args: str) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if result.returncode != 0:
        return False, (result.stderr or result.stdout or "git misslyckades").strip()[:200]
    return True, result.stdout.strip()


def local_git_state() -> dict[str, Any]:
    """Read-only view of the checkout. Absent/unreadable is a stated value, never an error."""
    settings = get_settings()
    repo = settings.local_repo_path
    if not repo:
        return {"available": False, "reason": "LOCAL_REPO_PATH är inte satt", "path": None}

    ok, head = _git(repo, "rev-parse", "HEAD")
    if not ok:
        return {"available": False, "reason": f"kunde inte läsa HEAD: {head}", "path": repo}

    ok_subject, subject = _git(repo, "log", "-1", "--pretty=%s")
    ok_branch, branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")

    # The branch's OWN upstream first (R14 finding: assuming origin/main made the whole local
    # read fail in a worktree without that ref). No upstream -> UNKNOWN, never "0 unpushed".
    ok_upstream, upstream = _git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    if not ok_upstream or not upstream:
        return {
            "available": True,
            "path": repo,
            "head_sha": head,
            "head_subject": subject if ok_subject else None,
            "branch": branch if ok_branch else None,
            "compare_ref": None,
            "unpushed_count": None,
            "compare_note": "ingen upstream-referens finns för grenen — jämförelse med GitHub är okänd",
        }

    ok_count, count = _git(repo, "rev-list", "--count", f"{upstream}..HEAD")
    return {
        "available": True,
        "path": repo,
        "head_sha": head,
        "head_subject": subject if ok_subject else None,
        "branch": branch if ok_branch else None,
        "compare_ref": upstream,
        "unpushed_count": int(count) if ok_count and count.isdigit() else None,
        "compare_note": None if ok_count else f"kunde inte jämföra: {count}",
    }


def latest_snapshot(db: Session, *, owner_id: uuid.UUID) -> GithubStatusSnapshot | None:
    return (
        db.execute(
            select(GithubStatusSnapshot)
            .where(GithubStatusSnapshot.owner_id == owner_id)
            .order_by(GithubStatusSnapshot.fetched_at.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )


def save_snapshot(db: Session, *, owner_id: uuid.UUID, repo: str, payload: dict[str, Any]) -> GithubStatusSnapshot:
    row = GithubStatusSnapshot(
        owner_id=owner_id,
        repo=repo,
        payload=payload,
        fetched_at=datetime.utcnow(),
    )
    db.add(row)
    db.flush()
    return row


def dev_status(db: Session, *, owner_id: uuid.UUID) -> dict[str, Any]:
    """The current, honest picture: configured? verified? how old? local vs remote."""
    settings = get_settings()
    client = GitHubClient()
    local = local_git_state()

    if not client.is_configured():
        return {
            "configured": False,
            "repo": settings.github_repo,
            "remote": None,
            "last_verified_at": None,
            "age_minutes": None,
            "stale": True,
            "local": local,
            "local_matches_remote_head": None,
            "error": None,
            "note": "GitHub är inte konfigurerad (GITHUB_TOKEN/GITHUB_REPO) — ingen status kan verifieras.",
        }

    snapshot = latest_snapshot(db, owner_id=owner_id)
    if snapshot is None:
        return {
            "configured": True,
            "repo": settings.github_repo,
            "remote": None,
            "last_verified_at": None,
            "age_minutes": None,
            "stale": True,
            "local": local,
            "local_matches_remote_head": None,
            "error": None,
            "note": "GitHub är konfigurerad men aldrig verifierad — kör en uppdatering.",
        }

    age = max(0, int((datetime.utcnow() - snapshot.fetched_at).total_seconds() // 60))
    remote = snapshot.payload or {}
    matches = None
    if local.get("available") and remote.get("head_sha"):
        matches = local.get("head_sha") == remote.get("head_sha")

    return {
        "configured": True,
        "repo": snapshot.repo,
        "remote": remote,
        "last_verified_at": _iso(snapshot.fetched_at),
        "age_minutes": age,
        "stale": age > settings.github_status_stale_minutes,
        "local": local,
        "local_matches_remote_head": matches,
        "error": None,
        "note": None,
    }


async def refresh(db: Session, *, owner_id: uuid.UUID) -> dict[str, Any]:
    """Reads GitHub once. On failure the previous snapshot is untouched and the error is stated."""
    settings = get_settings()
    client = GitHubClient()
    if not client.is_configured():
        return {
            "ok": False,
            "reason": "not_configured",
            "message": "GitHub är inte konfigurerad (GITHUB_TOKEN/GITHUB_REPO).",
            "status": dev_status(db, owner_id=owner_id),
            "snapshot_saved": False,
        }
    try:
        payload = await fetch_remote_status()
    except Exception as exc:  # noqa: BLE001 - the failure itself is the product's answer
        logger.warning("GitHub-status refresh failed: %s", exc)
        return {
            "ok": False,
            "reason": "fetch_failed",
            "message": str(exc)[:300],
            "status": dev_status(db, owner_id=owner_id),
            "snapshot_saved": False,
        }

    save_snapshot(db, owner_id=owner_id, repo=settings.github_repo or "", payload=payload)
    db.commit()
    return {
        "ok": True,
        "reason": None,
        "message": None,
        "status": dev_status(db, owner_id=owner_id),
        "snapshot_saved": True,
    }


def summary_for_prompt(status: dict[str, Any]) -> str:
    """One compact line for the conversation path — facts and their freshness, no inference."""
    if not status["configured"]:
        return "GitHub: inte konfigurerad (ingen token) — utvecklingsstatus kan inte verifieras."
    if status["remote"] is None:
        return "GitHub: konfigurerad men aldrig verifierad — kör en uppdatering först."
    remote = status["remote"]
    head = remote.get("head_commit") or {}
    age = status["age_minutes"]
    freshness = f"verifierad för {age} min sedan" if age is not None else "okänd ålder"
    local = status["local"]
    unpushed = (
        f"{local['unpushed_count']} lokala commits ej på GitHub"
        if local.get("available") and local.get("unpushed_count") is not None
        else "lokal jämförelse ej tillgänglig"
    )
    return (
        f"GitHub ({status['repo']}, {freshness}{', INAKTUELL' if status['stale'] else ''}): "
        f"{remote.get('branch') or 'default'} @ {(remote.get('head_sha') or '')[:12]} — "
        f"{(head.get('subject') or '')[:80]}; {len(remote.get('open_prs') or [])} öppna PR, "
        f"{remote.get('open_issue_count')} öppna ärenden; {unpushed}"
    )
