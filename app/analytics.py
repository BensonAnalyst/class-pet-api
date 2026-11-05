"""Pure analytics on point_history rows (no I/O, easy to unit-test).

A row is a dict: {"enrollment_id": str, "delta": int, "created_at": ISO-8601 str}.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from statistics import mean, pstdev


_FRACTION = re.compile(r"\.(\d+)")


def _ts(value: str) -> datetime:
    """Parse Supabase timestamps. Python < 3.11 only accepts 3 or 6 fractional digits, but Postgres
    drops trailing zeros (e.g. '.12345'), so normalise the fraction to 6 digits first."""
    v = value.replace("Z", "+00:00")
    v = _FRACTION.sub(lambda m: "." + (m.group(1) + "000000")[:6], v, count=1)
    return datetime.fromisoformat(v)


def totals(rows: list[dict]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for r in rows:
        out[r["enrollment_id"]] += int(r["delta"])
    return dict(out)


def daily_trend(rows: list[dict], days: int, now: datetime | None = None) -> list[dict]:
    """Net points per day for the last `days` days (zero-filled)."""
    now = now or datetime.now(timezone.utc)
    start = (now - timedelta(days=days - 1)).date()
    buckets = {start + timedelta(days=i): 0 for i in range(days)}
    for r in rows:
        d = _ts(r["created_at"]).date()
        if d in buckets:
            buckets[d] += int(r["delta"])
    return [{"date": d.isoformat(), "net_points": v} for d, v in buckets.items()]


def at_risk(rows: list[dict], window_days: int = 14, now: datetime | None = None,
            z_threshold: float = -2.0, min_class_size: int = 5) -> list[dict]:
    """Flag students whose recent momentum dropped.

    recent = net points in the last `window_days`; prior = the window before it.
    A student is flagged when recent < 0 (rule) or the class-relative change is
    more than |z_threshold| standard deviations below the class mean (needs
    `min_class_size` students so the statistic is meaningful).
    """
    now = now or datetime.now(timezone.utc)
    recent_start = now - timedelta(days=window_days)
    prior_start = now - timedelta(days=2 * window_days)
    recent: dict[str, int] = defaultdict(int)
    prior: dict[str, int] = defaultdict(int)
    ids: set[str] = set()
    for r in rows:
        ids.add(r["enrollment_id"])
        t = _ts(r["created_at"])
        if t >= recent_start:
            recent[r["enrollment_id"]] += int(r["delta"])
        elif t >= prior_start:
            prior[r["enrollment_id"]] += int(r["delta"])
    changes = {i: recent[i] - prior[i] for i in ids}
    mu = mean(changes.values()) if changes else 0.0
    sd = pstdev(changes.values()) if len(changes) >= min_class_size else 0.0
    flagged = []
    for i in ids:
        z = (changes[i] - mu) / sd if sd else 0.0
        reasons = []
        if recent[i] < 0:
            reasons.append("negative_net_points")
        if sd and z <= z_threshold:
            reasons.append("sharp_drop_vs_class")
        if reasons:
            flagged.append({"enrollment_id": i, "recent_net": recent[i], "prior_net": prior[i],
                            "change": changes[i], "z_score": round(z, 2), "reasons": reasons})
    return sorted(flagged, key=lambda x: x["change"])
