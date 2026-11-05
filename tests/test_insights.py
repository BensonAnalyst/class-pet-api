"""Tests for the OpenAI-powered /insights endpoint. The OpenAI client is always faked: no network, no key, no cost."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import insights
from app.main import app, get_llm_factory, get_source

NOW = datetime.now(timezone.utc)


def row(eid, delta, days_ago, label="Helpful"):
    return {"enrollment_id": eid, "delta": delta, "label": label,
            "created_at": (NOW - timedelta(days=days_ago)).isoformat()}


ENROLLS = [{"id": f"enr-{c}", "students": {"name": n}} for c, n in
           zip("abcdefg", ["Ann Tan", "Ben Lim", "Cara Ong", "Dev Rao", "Eli Goh", "Fay Koh", "Zed Secret"])]


def history():
    rows = []
    for c in "abcdef":                              # steady students
        rows += [row(f"enr-{c}", 3, 20), row(f"enr-{c}", 3, 5)]
    rows += [row("enr-g", 8, 20), row("enr-g", -2, 5, "Talking in class")]   # dropped
    return rows


class FakeSource:
    def enrollments(self, class_id):
        return ENROLLS

    def history(self, ids):
        return history()


class FakeLLM:
    """Mimics client.chat.completions.create and records what it was sent."""
    def __init__(self, content=None, finish="stop", refusal=None):
        self.calls = []
        self._content, self._finish, self._refusal = content, finish, refusal
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        content = self._content or json.dumps({
            "headline": "One student may need a check-in",
            "summary": "S7 dropped sharply compared with the class.",
            "students_to_check": [
                {"alias": "S7", "observation": "Net points fell from +8 to -2.", "suggested_action": "Have a quiet chat this week."},
                {"alias": "S99", "observation": "invented", "suggested_action": "invented"}],
            "class_actions": ["Celebrate small wins on Friday."]})
        msg = SimpleNamespace(content=content, refusal=self._refusal)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason=self._finish)],
                               usage=SimpleNamespace(prompt_tokens=400, completion_tokens=100))


@pytest.fixture
def client():
    app.dependency_overrides[get_source] = lambda: FakeSource()
    yield TestClient(app)
    app.dependency_overrides.clear()


def use_llm(llm):
    app.dependency_overrides[get_llm_factory] = lambda: (lambda: llm)


def test_insights_happy_path_maps_names_back_and_drops_invented_aliases(client):
    llm = FakeLLM()
    use_llm(llm)
    r = client.get("/v1/classes/c1/insights")
    assert r.status_code == 200
    body = r.json()
    assert [s["name"] for s in body["students_to_check"]] == ["Zed Secret"]   # S99 filtered out
    assert body["usage"]["prompt_tokens"] == 400


def test_request_uses_strict_json_schema(client):
    llm = FakeLLM()
    use_llm(llm)
    client.get("/v1/classes/c1/insights")
    rf = llm.calls[0]["response_format"]
    assert rf["type"] == "json_schema" and rf["json_schema"]["strict"] is True
    assert rf["json_schema"]["schema"]["additionalProperties"] is False


def test_no_student_names_or_real_ids_leave_the_service(client):
    llm = FakeLLM()
    use_llm(llm)
    client.get("/v1/classes/c1/insights")
    sent = json.dumps(llm.calls[0]["messages"])
    for secret in ["Zed", "Ann", "Secret", "enr-g", "enr-a"]:
        assert secret not in sent
    assert "S7" in sent or "S1" in sent          # pseudonyms are present


def test_skips_model_call_when_nobody_is_flagged(client):
    class Quiet(FakeSource):
        def history(self, ids):
            return [row(e["id"], 2, 3) for e in ENROLLS]
    app.dependency_overrides[get_source] = lambda: Quiet()
    llm = FakeLLM()
    use_llm(llm)
    r = client.get("/v1/classes/c1/insights")
    assert r.status_code == 200 and llm.calls == [] and r.json()["students_to_check"] == []


def test_invalid_model_output_returns_502(client):
    use_llm(FakeLLM(content='{"headline": "x"}'))        # missing required keys
    assert client.get("/v1/classes/c1/insights").status_code == 502


def test_truncated_or_refused_output_returns_502(client):
    use_llm(FakeLLM(finish="length"))
    assert client.get("/v1/classes/c1/insights").status_code == 502
    use_llm(FakeLLM(refusal="no"))
    assert client.get("/v1/classes/c1/insights").status_code == 502


def test_missing_api_key_returns_503_without_leaking(client, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    app.dependency_overrides.pop(get_llm_factory, None)   # use the real factory
    r = client.get("/v1/classes/c1/insights")
    assert r.status_code == 503 and "OPENAI_API_KEY" in r.json()["detail"]


def test_cost_estimate_only_when_prices_configured(monkeypatch):
    usage = SimpleNamespace(prompt_tokens=1000, completion_tokens=500)
    monkeypatch.delenv("OPENAI_PRICE_IN_PER_1M", raising=False)
    assert insights._cost(usage) is None
    monkeypatch.setenv("OPENAI_PRICE_IN_PER_1M", "1.0")
    monkeypatch.setenv("OPENAI_PRICE_OUT_PER_1M", "2.0")
    assert insights._cost(usage) == pytest.approx(0.002)    # (1000*1 + 500*2) / 1e6


def test_requires_token():
    assert TestClient(app).get("/v1/classes/c1/insights").status_code == 401
