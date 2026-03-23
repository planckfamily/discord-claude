"""
HTTP client for the myvillage dashboard API.

The discord-Claude bot calls this to:
  1. Fetch the assigned persona for a project before starting a session
  2. Report feature lifecycle events (start / complete)
  3. Report session costs after each Claude run
  4. Report activity (user messages + Claude responses) for the 24-hour feed

All calls use Bearer-token auth (BRIDGECREW_API_KEY env var).
If BRIDGECREW_API_URL is not set the client is disabled and all calls are no-ops.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import httpx

log = logging.getLogger(__name__)

_API_URL = os.environ.get("BRIDGECREW_API_URL", "").rstrip("/")
_API_KEY = os.environ.get("BRIDGECREW_API_KEY", "")

if _API_URL and _API_KEY:
    log.info("BridgeCrew integration enabled: %s", _API_URL)
else:
    log.info("BridgeCrew integration disabled (BRIDGECREW_API_URL/KEY not set) — tracking is a no-op")


def _enabled() -> bool:
    return bool(_API_URL and _API_KEY)


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_API_KEY}",
        "Content-Type": "application/json",
    }


def get_project_prompt(project_id: str) -> str:
    """
    Return the persona content assigned to a project.
    Returns empty string if not configured or on any error.
    """
    if not _enabled() or not project_id:
        return ""
    try:
        resp = httpx.get(
            f"{_API_URL}/api/projects/{project_id}/prompt",
            headers=_headers(),
            timeout=5,
        )
        if resp.status_code == 200:
            return resp.json().get("content", "")
        log.warning("get_project_prompt: HTTP %s for project %s", resp.status_code, project_id)
    except Exception as exc:
        log.warning("get_project_prompt failed: %s", exc)
    return ""


def report_feature_started(
    project_id: str,
    feature_name: str,
    session_id: str,
    prompt_template_id: str = "",
    subdir: str = "",
) -> str | None:
    """
    Tell myvillage a feature has started. Returns the feature_id assigned by
    the server, or None on failure.
    """
    if not _enabled():
        return None
    payload = {
        "project_id": project_id,
        "name": feature_name,
        "session_id": session_id,
        "prompt_template_id": prompt_template_id,
        "subdir": subdir or "",
    }
    try:
        resp = httpx.post(
            f"{_API_URL}/api/features",
            headers=_headers(),
            json=payload,
            timeout=5,
        )
        if resp.status_code == 201:
            return resp.json().get("feature_id")
        log.warning("report_feature_started: HTTP %s", resp.status_code)
    except Exception as exc:
        log.warning("report_feature_started failed: %s", exc)
    return None


def report_feature_completed(
    feature_id: str,
    summary: str = "",
    total_cost_usd: float = 0.0,
    git_branch: str = "",
    total_input_tokens: int = 0,
    total_output_tokens: int = 0,
) -> None:
    """Tell myvillage a feature has been completed."""
    if not _enabled() or not feature_id:
        return
    payload: dict = {"status": "completed"}
    if summary:
        payload["summary"] = summary
    if total_cost_usd:
        payload["total_cost_usd"] = total_cost_usd
    if git_branch:
        payload["git_branch"] = git_branch
    if total_input_tokens:
        payload["total_input_tokens"] = total_input_tokens
    if total_output_tokens:
        payload["total_output_tokens"] = total_output_tokens
    try:
        resp = httpx.patch(
            f"{_API_URL}/api/features/{feature_id}",
            headers=_headers(),
            json=payload,
            timeout=5,
        )
        if resp.status_code != 200:
            log.warning("report_feature_completed: HTTP %s", resp.status_code)
    except Exception as exc:
        log.warning("report_feature_completed failed: %s", exc)


def report_activity(
    project_id: str,
    role: str,
    author: str,
    content: str,
    feature_name: str | None = None,
) -> None:
    """Log a user message or Claude response to the 24-hour activity feed."""
    if not _enabled() or not project_id:
        return
    payload = {
        "project_id": project_id,
        "role": role,
        "author": author,
        "content": content[:2000],
        "feature_name": feature_name,
    }
    try:
        resp = httpx.post(
            f"{_API_URL}/api/activity",
            headers=_headers(),
            json=payload,
            timeout=5,
        )
        if resp.status_code != 201:
            log.warning("report_activity: HTTP %s", resp.status_code)
    except Exception as exc:
        log.warning("report_activity failed: %s", exc)


def report_cost(
    project_id: str,
    session_id: str,
    model: str,
    cost_usd: float,
    input_tokens: int = 0,
    output_tokens: int = 0,
    feature_id: str = "",
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
) -> None:
    """Report a session cost entry to myvillage."""
    if not _enabled() or cost_usd <= 0:
        return
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "project_id": project_id,
        "feature_id": feature_id,
        "session_id": session_id,
        "model": model,
        "cost_usd": cost_usd,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "started_at": started_at.isoformat() if started_at else now,
        "completed_at": completed_at.isoformat() if completed_at else now,
    }
    try:
        resp = httpx.post(
            f"{_API_URL}/api/costs",
            headers=_headers(),
            json=payload,
            timeout=5,
        )
        if resp.status_code != 201:
            log.warning("report_cost: HTTP %s", resp.status_code)
    except Exception as exc:
        log.warning("report_cost failed: %s", exc)
