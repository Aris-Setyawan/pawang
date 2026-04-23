"""GitHub Tools — Tier 1 read-only access via `gh` CLI.

Reuses the existing `gh auth` credentials on this host — no token copies are
stored in Pawang's config. If the admin runs `gh auth logout`, these tools
immediately lose access.

Every call is logged to the `github_audit` table for review via /gh audit.

Tier 1 operations implemented here NEVER mutate GitHub state:
  - list repos / issues / PRs
  - view repo / issue / PR details + diff
  - read file contents at any ref
  - search code
  - raw GET-only API escape hatch

Writes (comments, pushes, PR creation) will land in a separate module once
Tier 2 approval-DM integration is ready.
"""

import asyncio
import json
import shutil
import time
from typing import Optional

from core.database import get_db
from core.logger import log


# Max chars returned from any single call — prevents context blowup on huge
# repos / long issue threads. _smart_truncate_output already exists in tools.py
# but we keep this local to avoid a circular import.
_MAX_OUTPUT = 6000


def _truncate(text: str, limit: int = _MAX_OUTPUT) -> str:
    if not text or len(text) <= limit:
        return text
    head = limit // 3
    tail = limit - head
    omitted = len(text) - head - tail
    return (
        text[:head]
        + f"\n\n... [truncated {omitted:,} chars — showing first {head} + last {tail}] ...\n\n"
        + text[-tail:]
    )


def _audit(agent_id: str, user_id: str, action: str, target: str,
           params: dict, success: bool):
    """Record every GitHub operation for later review. Never raises."""
    try:
        db = get_db()
        db.record_github_audit(
            agent_id=agent_id, user_id=user_id, action=action,
            target=target, params=json.dumps(params, ensure_ascii=False)[:2000],
            success=success,
        )
    except Exception as e:
        log.debug(f"github audit log failed: {e}")


async def _run_gh(args: list[str], timeout: int = 30) -> tuple[str, bool]:
    """Run `gh <args>` and return (output, ok). No shell, no injection risk."""
    if not shutil.which("gh"):
        return "gh CLI not installed on this host", False
    try:
        proc = await asyncio.create_subprocess_exec(
            "gh", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout,
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return f"gh command timed out after {timeout}s", False
        out = (stdout or b"").decode("utf-8", errors="replace")
        err = (stderr or b"").decode("utf-8", errors="replace")
        if proc.returncode != 0:
            msg = err.strip() or out.strip() or f"gh exited {proc.returncode}"
            return msg, False
        return out, True
    except FileNotFoundError:
        return "gh CLI not installed on this host", False
    except Exception as e:
        return f"gh exec error: {type(e).__name__}: {e}", False


# ---------- Tier 1 operations ----------

async def repo_list(limit: int = 20, visibility: str = "",
                    agent_id: str = "", user_id: str = "") -> tuple[str, bool]:
    """List repos accessible to the authenticated user."""
    args = ["repo", "list", "--limit", str(max(1, min(limit, 100))),
            "--json", "nameWithOwner,description,isPrivate,updatedAt,primaryLanguage"]
    if visibility in ("public", "private"):
        args += ["--visibility", visibility]
    out, ok = await _run_gh(args)
    _audit(agent_id, user_id, "repo_list", "", {"limit": limit, "visibility": visibility}, ok)
    if not ok:
        return out, False
    try:
        repos = json.loads(out)
        lines = []
        for r in repos:
            lang = (r.get("primaryLanguage") or {}).get("name", "-")
            privacy = "private" if r.get("isPrivate") else "public"
            lines.append(
                f"- {r['nameWithOwner']} [{privacy}] ({lang}) — "
                f"{r.get('description') or '(no description)'}"
            )
        return f"{len(repos)} repos:\n" + "\n".join(lines), True
    except Exception:
        return _truncate(out), True


async def repo_view(repo: str, agent_id: str = "", user_id: str = "") -> tuple[str, bool]:
    """View a repo's README + metadata. repo format: owner/name."""
    out, ok = await _run_gh(["repo", "view", repo])
    _audit(agent_id, user_id, "repo_view", repo, {"repo": repo}, ok)
    return _truncate(out), ok


async def issue_list(repo: str, state: str = "open", limit: int = 20,
                     agent_id: str = "", user_id: str = "") -> tuple[str, bool]:
    """List issues in a repo. state: open | closed | all."""
    if state not in ("open", "closed", "all"):
        state = "open"
    args = ["issue", "list", "--repo", repo, "--state", state,
            "--limit", str(max(1, min(limit, 50)))]
    out, ok = await _run_gh(args)
    _audit(agent_id, user_id, "issue_list", repo,
           {"state": state, "limit": limit}, ok)
    return _truncate(out), ok


async def issue_view(repo: str, number: int, agent_id: str = "",
                     user_id: str = "") -> tuple[str, bool]:
    """View an issue with comments."""
    out, ok = await _run_gh([
        "issue", "view", str(number), "--repo", repo, "--comments",
    ])
    _audit(agent_id, user_id, "issue_view", f"{repo}#{number}",
           {"repo": repo, "number": number}, ok)
    return _truncate(out), ok


async def pr_list(repo: str, state: str = "open", limit: int = 20,
                  agent_id: str = "", user_id: str = "") -> tuple[str, bool]:
    """List PRs in a repo."""
    if state not in ("open", "closed", "merged", "all"):
        state = "open"
    args = ["pr", "list", "--repo", repo, "--state", state,
            "--limit", str(max(1, min(limit, 50)))]
    out, ok = await _run_gh(args)
    _audit(agent_id, user_id, "pr_list", repo,
           {"state": state, "limit": limit}, ok)
    return _truncate(out), ok


async def pr_view(repo: str, number: int, agent_id: str = "",
                  user_id: str = "") -> tuple[str, bool]:
    """View a PR (description, checks, review state) with comments."""
    out, ok = await _run_gh([
        "pr", "view", str(number), "--repo", repo, "--comments",
    ])
    _audit(agent_id, user_id, "pr_view", f"{repo}#{number}",
           {"repo": repo, "number": number}, ok)
    return _truncate(out), ok


async def pr_diff(repo: str, number: int, agent_id: str = "",
                  user_id: str = "") -> tuple[str, bool]:
    """View a PR diff (truncated for context safety)."""
    out, ok = await _run_gh(["pr", "diff", str(number), "--repo", repo])
    _audit(agent_id, user_id, "pr_diff", f"{repo}#{number}",
           {"repo": repo, "number": number}, ok)
    return _truncate(out), ok


async def read_file(repo: str, path: str, ref: str = "",
                    agent_id: str = "", user_id: str = "") -> tuple[str, bool]:
    """Read a file from any repo at any ref (branch, tag, or commit)."""
    endpoint = f"repos/{repo}/contents/{path.lstrip('/')}"
    if ref:
        endpoint += f"?ref={ref}"
    out, ok = await _run_gh([
        "api", endpoint, "-H", "Accept: application/vnd.github.raw",
    ], timeout=45)
    _audit(agent_id, user_id, "read_file", f"{repo}:{path}@{ref or 'HEAD'}",
           {"repo": repo, "path": path, "ref": ref}, ok)
    return _truncate(out, limit=8000), ok


async def search_code(query: str, limit: int = 20, agent_id: str = "",
                      user_id: str = "") -> tuple[str, bool]:
    """Search code across repos accessible to the authenticated user."""
    args = ["search", "code", query, "--limit", str(max(1, min(limit, 50)))]
    out, ok = await _run_gh(args, timeout=45)
    _audit(agent_id, user_id, "search_code", "",
           {"query": query[:200], "limit": limit}, ok)
    return _truncate(out), ok


async def api_get(endpoint: str, agent_id: str = "",
                  user_id: str = "") -> tuple[str, bool]:
    """Raw GET escape hatch. Method is locked to GET — write endpoints can
    still fail at the server even if someone tries, but the explicit `-X GET`
    plus refusing --method/-X in the arg keeps it honest."""
    # Disallow attempts to sneak in a method override.
    bad = ("-X ", "--method ", "--input", "--field", "-f ", "-F ")
    if any(b in f" {endpoint} " for b in bad):
        _audit(agent_id, user_id, "api_get", endpoint,
               {"endpoint": endpoint, "blocked": "method override"}, False)
        return "Blocked: api_get is GET-only — use Tier 2 tools for writes.", False
    # Strip leading slash, gh api does not expect it.
    clean = endpoint.lstrip("/")
    out, ok = await _run_gh(["api", "-X", "GET", clean], timeout=45)
    _audit(agent_id, user_id, "api_get", clean, {"endpoint": clean}, ok)
    return _truncate(out, limit=8000), ok


async def audit_log(limit: int = 20, agent_id: str = "",
                    user_id: str = "") -> tuple[str, bool]:
    """Read recent github_audit rows. Exposed so admin can review what Wulan did."""
    try:
        db = get_db()
        rows = db.get_github_audit(limit=max(1, min(limit, 100)))
        if not rows:
            return "(audit log empty)", True
        lines = []
        for r in rows:
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["created_at"]))
            icon = "✓" if r["success"] else "✗"
            lines.append(
                f"{icon} {ts} [{r['agent_id']}] {r['action']} "
                f"{r['target'] or '-'} ({r['params'][:80]})"
            )
        return "\n".join(lines), True
    except Exception as e:
        return f"audit read error: {e}", False
