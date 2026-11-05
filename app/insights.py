"""LLM-written teacher insights on top of the deterministic analytics.

Design rules (see README "AI insights"):
  1. Numbers are computed in plain Python (analytics.py). The model only *explains* them,
     so it cannot invent statistics.
  2. Students are pseudonymised (S1, S2, ...) before anything leaves this service. Real names are
     mapped back locally after the model answers. No names or ids are sent to OpenAI.
  3. The reply is forced into a strict JSON schema and re-validated with pydantic.
  4. If nobody is flagged, the model is not called at all (zero cost, zero latency).
"""
from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, Field

from . import analytics

MAX_STUDENTS_IN_PROMPT = 15


# ---------- response contract ----------
class StudentNote(BaseModel):
    alias: str = Field(description="Pseudonym such as S3, exactly as given in the input")
    observation: str = Field(max_length=300)
    suggested_action: str = Field(max_length=300)


class InsightsOut(BaseModel):
    headline: str = Field(max_length=140)
    summary: str = Field(max_length=700)
    students_to_check: list[StudentNote] = Field(max_length=MAX_STUDENTS_IN_PROMPT)
    class_actions: list[str] = Field(max_length=3)


# Hand-written strict schema (OpenAI structured outputs: every key required, no extra keys).
JSON_SCHEMA = {
    "name": "class_insights",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["headline", "summary", "students_to_check", "class_actions"],
        "properties": {
            "headline": {"type": "string"},
            "summary": {"type": "string"},
            "students_to_check": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["alias", "observation", "suggested_action"],
                    "properties": {
                        "alias": {"type": "string"},
                        "observation": {"type": "string"},
                        "suggested_action": {"type": "string"},
                    },
                },
            },
            "class_actions": {"type": "array", "items": {"type": "string"}},
        },
    },
}

SYSTEM_PROMPT = (
    "You are an assistant for a primary-school teacher who uses a classroom points system. "
    "You receive pre-computed statistics about a class. Students are pseudonyms (S1, S2, ...).\n"
    "Rules:\n"
    "- Use ONLY the numbers provided. Never invent, estimate or recompute statistics.\n"
    "- Refer to students only by their alias exactly as given.\n"
    "- Be supportive and practical. Treat a drop in points as a reason to check in, never as blame.\n"
    "- Suggested actions must be small, concrete things a teacher can do this week.\n"
    "- Mention at most the students listed in 'flagged'. Plain English, no jargon."
)


# ---------- digest (what the model sees) ----------
def build_digest(enrolls: list[dict], rows: list[dict], window_days: int,
                 now: datetime | None = None) -> tuple[dict, dict[str, str]]:
    """Return (digest, alias_to_enrollment_id). The digest contains no names or real ids."""
    now = now or datetime.now(timezone.utc)
    ids = sorted(e["id"] for e in enrolls)
    alias_of = {eid: f"S{i + 1}" for i, eid in enumerate(ids)}
    flagged_raw = analytics.at_risk(rows, window_days, now=now)[:MAX_STUDENTS_IN_PROMPT]

    recent_start = now - timedelta(days=window_days)
    labels_by_student: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        if analytics._ts(r["created_at"]) >= recent_start:
            labels_by_student[r["enrollment_id"]][(r.get("label") or "").strip()[:40]] += int(r["delta"])

    flagged = []
    for f in flagged_raw:
        eid = f["enrollment_id"]
        worst = sorted(labels_by_student[eid].items(), key=lambda kv: kv[1])[:2]
        flagged.append({
            "alias": alias_of.get(eid, "S?"),
            "recent_net_points": f["recent_net"],
            "prior_net_points": f["prior_net"],
            "change": f["change"],
            "z_score_vs_class": f["z_score"],
            "reasons": f["reasons"],
            "most_negative_labels_recent": [{"label": k, "net": v} for k, v in worst if v < 0],
        })

    totals_all = analytics.totals(rows)
    digest = {
        "window_days": window_days,
        "class_size": len(ids),
        "class_net_points_all_time": sum(totals_all.values()),
        "students_with_no_activity_all_time": sum(1 for eid in ids if eid not in totals_all),
        "flagged": flagged,
    }
    return digest, {a: eid for eid, a in alias_of.items()}


# ---------- model call ----------
class InsightsUnavailable(RuntimeError):
    """Not configured (no API key) or the upstream call failed."""


def make_client():
    """Create the OpenAI client from the environment. The key is never logged or returned."""
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise InsightsUnavailable("OPENAI_API_KEY is not set")
    from openai import OpenAI
    return OpenAI(api_key=key, timeout=30, max_retries=2)


def _cost(usage) -> float | None:
    """Optional cost estimate. Prices (USD per 1M tokens) come from env so they never go stale in code."""
    try:
        p_in = float(os.environ["OPENAI_PRICE_IN_PER_1M"])
        p_out = float(os.environ["OPENAI_PRICE_OUT_PER_1M"])
    except (KeyError, ValueError):
        return None
    return round((usage.prompt_tokens * p_in + usage.completion_tokens * p_out) / 1_000_000, 6)


def generate(client, digest: dict) -> tuple[InsightsOut, dict]:
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(digest, separators=(",", ":"))},
        ],
        response_format={"type": "json_schema", "json_schema": JSON_SCHEMA},
        temperature=0.2,
        max_completion_tokens=700,
    )
    choice = resp.choices[0]
    if getattr(choice.message, "refusal", None) or choice.finish_reason != "stop":
        raise InsightsUnavailable("Model did not return a complete answer")
    try:
        out = InsightsOut.model_validate_json(choice.message.content)
    except Exception as e:  # schema drift or truncated JSON
        raise InsightsUnavailable(f"Model output failed validation: {e.__class__.__name__}")
    # only aliases we actually sent are allowed back
    allowed = {f["alias"] for f in digest["flagged"]}
    out.students_to_check = [s for s in out.students_to_check if s.alias in allowed]
    usage = {"model": model, "prompt_tokens": resp.usage.prompt_tokens,
             "completion_tokens": resp.usage.completion_tokens, "estimated_cost_usd": _cost(resp.usage)}
    return out, usage
