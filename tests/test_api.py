from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app import analytics
from app.main import app, get_source

NOW = datetime(2026, 11, 3, tzinfo=timezone.utc)


def row(eid, delta, days_ago):
    return {"enrollment_id": eid, "delta": delta, "created_at": (NOW - timedelta(days=days_ago)).isoformat()}


def test_totals():
    assert analytics.totals([row("a", 2, 1), row("a", -1, 2), row("b", 3, 1)]) == {"a": 1, "b": 3}


def test_daily_trend_zero_filled():
    t = analytics.daily_trend([row("a", 2, 0)], 7, now=NOW)
    assert len(t) == 7 and t[-1]["net_points"] == 2 and t[0]["net_points"] == 0


def test_at_risk_flags_negative_and_sharp_drop():
    rows = []
    for i in "abcdef":            # steady students
        rows += [row(i, 3, 20), row(i, 3, 5)]
    rows += [row("x", 8, 20), row("x", -2, 5)]   # sharp drop and negative
    flagged = analytics.at_risk(rows, 14, now=NOW)
    assert flagged[0]["enrollment_id"] == "x"
    assert "negative_net_points" in flagged[0]["reasons"]
    assert all(f["enrollment_id"] == "x" for f in flagged)


class FakeSource:
    def enrollments(self, class_id):
        return [{"id": "a", "students": {"name": "Ann"}}, {"id": "b", "students": {"name": "Ben"}}]

    def history(self, ids):
        return [row("a", 5, 1), row("b", 9, 1)]

    def history_for(self, i):
        return [row(i, 2, 0)]

    def add_point(self, e, label, delta, by):
        return {"enrollment_id": e, "label": label, "delta": delta}


client = TestClient(app)


def test_requires_token():
    assert client.get("/v1/classes/c1/leaderboard").status_code == 401


def test_leaderboard_sorted():
    app.dependency_overrides[get_source] = lambda: FakeSource()
    r = client.get("/v1/classes/c1/leaderboard")
    assert r.status_code == 200 and r.json()[0]["name"] == "Ben"


def test_point_validation():
    app.dependency_overrides[get_source] = lambda: FakeSource()
    assert client.post("/v1/points", json={"enrollment_id": "a", "label": "Helpful", "delta": 99}).status_code == 422
    assert client.post("/v1/points", json={"enrollment_id": "a", "label": "Helpful", "delta": 2}).status_code == 201
