"""Enforcement jobs: expiry/quota auto-disable and per-tick stats collection.

`evaluate` is a pure decision function; `run_cycle` performs one enforcement
tick (collect stats → disable violators → reapply the xray config). The app
lifespan schedules `run_cycle` every `TICK_SECONDS`; expected failures (stats
service unreachable, config rejected by the manager) are logged and swallowed
so the background loop never dies.
"""

import logging
from datetime import datetime, timezone

from panel import db
from panel.xray.stats import StatsError, compute_delta

logger = logging.getLogger(__name__)

TICK_SECONDS = 30


def _aware(dt: datetime) -> datetime:
    """Treat a naive datetime as UTC (storage normally carries an offset)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def evaluate(users: list[dict], now: datetime) -> dict[str, str]:
    """Map user id → disable reason for *enabled* users hit by expiry or quota.

    Expiry has precedence over quota; `now == expires_at` is NOT expired
    (strict <). Disabled users are never re-evaluated. Pure: no I/O, no clock.
    """
    now = _aware(now)
    hits: dict[str, str] = {}
    for user in users:
        if not user.get("enabled", 1):
            continue
        expires_raw = user.get("expires_at")
        if expires_raw:
            if _aware(datetime.fromisoformat(expires_raw)) < now:
                hits[user["id"]] = "expired"
                continue
        quota_gb = user.get("quota_gb")
        if quota_gb is not None and (user.get("used_bytes") or 0) >= quota_gb * 1e9:
            hits[user["id"]] = "quota"
    return hits


def run_cycle(
    settings, manager, stats_client, now: datetime | None = None
) -> int:
    """One enforcement tick; returns the number of users newly disabled.

    (1) Each enabled user's raw counter is folded into `used_bytes` via
    `compute_delta` (NULL `last_raw_bytes` read as 0); a `StatsError` skips
    stats for this tick but never fails the cycle. (2) `evaluate` decides who
    must be disabled. (3) If anyone was disabled, the full user list is
    re-applied to xray. `settings` is part of the pinned interface (kept for
    tick configuration); `now` defaults to the current UTC time (injectable
    for tests).
    """
    if now is None:
        now = datetime.now(timezone.utc)

    enabled = [user for user in db.list_users() if user.get("enabled", 1)]
    try:
        for user in enabled:
            raw = stats_client.get_bytes(user["id"])
            new_used, new_raw = compute_delta(
                raw,
                user.get("last_raw_bytes") or 0,
                user.get("used_bytes") or 0,
            )
            db.update_user(
                user["id"], {"used_bytes": new_used, "last_raw_bytes": new_raw}
            )
    except StatsError as exc:
        logger.warning("stats collection skipped this tick: %s", exc)

    # Re-read: stats above may have pushed someone over quota this tick.
    hits = evaluate(db.list_users(), now)
    for user_id, reason in hits.items():
        db.update_user(user_id, {"enabled": 0, "disable_reason": reason})
    if hits:
        ok, msg = manager.apply_config(db.list_users(), db.list_inbounds())
        if not ok:
            # DB now has users disabled while xray keeps serving the old
            # config (rolled back) — the divergence must be visible.
            logger.error("apply_config failed after disabling users: %s", msg)
    return len(hits)
