from __future__ import annotations

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

from . import analytics, insights
from .datasource import SupabaseSource

app = FastAPI(title="Class Pet Garden Analytics API", version="1.0.0",
              description="Behaviour analytics and point logging on top of the Class Pet Garden database. "
                          "Auth: Supabase JWT in the Authorization header; access is enforced by Row-Level Security.")


def get_source(authorization: str | None = Header(default=None)) -> SupabaseSource:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing bearer token")
    return SupabaseSource(authorization.split(" ", 1)[1])


class PointIn(BaseModel):
    enrollment_id: str
    label: str = Field(min_length=1, max_length=80)
    delta: int = Field(ge=-10, le=10, description="Points awarded (negative = deduction)")
    logged_by: str | None = None


@app.get("/health")
def health():
    return {"status": "ok"}


def _names(enrolls: list[dict]) -> dict[str, str]:
    out = {}
    for e in enrolls:
        s = e.get("students") or {}
        out[e["id"]] = s.get("name") or s.get("first_name") or e["id"]
    return out


@app.get("/v1/classes/{class_id}/leaderboard")
def leaderboard(class_id: str, limit: int = Query(10, ge=1, le=100), src: SupabaseSource = Depends(get_source)):
    try:
        enrolls = src.enrollments(class_id)
        totals = analytics.totals(src.history([e["id"] for e in enrolls]))
    except PermissionError as e:
        raise HTTPException(403, str(e))
    names = _names(enrolls)
    rows = [{"enrollment_id": i, "name": names.get(i, i), "total_points": totals.get(i, 0)} for i in names]
    return sorted(rows, key=lambda r: -r["total_points"])[:limit]


@app.get("/v1/students/{enrollment_id}/trend")
def trend(enrollment_id: str, days: int = Query(30, ge=7, le=180), src: SupabaseSource = Depends(get_source)):
    try:
        rows = src.history_for(enrollment_id)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    return analytics.daily_trend(rows, days)


@app.get("/v1/classes/{class_id}/at-risk")
def at_risk(class_id: str, window_days: int = Query(14, ge=7, le=60), src: SupabaseSource = Depends(get_source)):
    try:
        enrolls = src.enrollments(class_id)
        rows = src.history([e["id"] for e in enrolls])
    except PermissionError as e:
        raise HTTPException(403, str(e))
    names = _names(enrolls)
    out = analytics.at_risk(rows, window_days)
    for r in out:
        r["name"] = names.get(r["enrollment_id"], r["enrollment_id"])
    return out


def get_llm_factory():
    """Returns a callable that builds the OpenAI client lazily, so the key is only needed when a
    model call is actually required. Tests override this dependency with a fake."""
    return insights.make_client


@app.get("/v1/classes/{class_id}/insights")
def class_insights(class_id: str, window_days: int = Query(14, ge=7, le=60),
                   src: SupabaseSource = Depends(get_source), make_llm=Depends(get_llm_factory)):
    """Plain-English weekly summary for the teacher. Stats are computed locally; students are
    pseudonymised before the text model sees anything."""
    try:
        enrolls = src.enrollments(class_id)
        rows = src.history([e["id"] for e in enrolls])
    except PermissionError as e:
        raise HTTPException(403, str(e))
    digest, alias_to_id = insights.build_digest(enrolls, rows, window_days)
    if not digest["flagged"]:   # nothing to explain: skip the paid call
        return {"headline": "No students need attention right now",
                "summary": f"No student dropped in the last {window_days} days.",
                "students_to_check": [], "class_actions": [], "usage": None}
    try:
        out, usage = insights.generate(make_llm(), digest)
    except insights.InsightsUnavailable as e:
        raise HTTPException(503 if "not set" in str(e) else 502, str(e))
    except Exception:           # network / upstream error: never leak details or the key
        raise HTTPException(502, "Insight generation failed upstream")
    names = _names(enrolls)
    body = out.model_dump()
    for s in body["students_to_check"]:           # map pseudonyms back to real names, locally
        s["name"] = names.get(alias_to_id.get(s["alias"], ""), s["alias"])
    body["usage"] = usage
    return body


@app.post("/v1/points", status_code=201)
def add_point(p: PointIn, src: SupabaseSource = Depends(get_source)):
    try:
        return src.add_point(p.enrollment_id, p.label, p.delta, p.logged_by)
    except PermissionError as e:
        raise HTTPException(403, str(e))
