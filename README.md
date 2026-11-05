# Class Pet Garden Analytics API

A small FastAPI service on top of the [Class Pet Garden](https://github.com/BensonAnalyst/class-pet-garden) database. It adds the **behaviour analytics and early-warning flags** from that project's roadmap, as a documented REST API.

## Before vs After

| | Before (browser talks straight to Supabase) | After (this API) |
|---|---|---|
| Analytics | None; teacher eyeballs points | `/at-risk` flags students whose points dropped (negative net, or >2 sigma below class change) |
| Reading the flags | Teacher interprets z-scores | `/insights` turns the flags into a short summary and one suggested action per student |
| Reuse | Logic lives in `index.html` only | Any client (app, script, BI tool) can call the same endpoints |
| Validation | Front-end only | Server-side: label length, delta limited to -10..+10 |
| Quality | No tests | Unit tests + CI that also builds the Docker image |

*Pilot stage: no measured usage numbers yet.*

## Endpoints (auto-docs at `/docs`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness check |
| GET | `/v1/classes/{class_id}/leaderboard?limit=10` | Total points per student |
| GET | `/v1/students/{enrollment_id}/trend?days=30` | Zero-filled daily net points |
| GET | `/v1/classes/{class_id}/at-risk?window_days=14` | Early-warning flags with reasons |
| GET | `/v1/classes/{class_id}/insights?window_days=14` | **AI** plain-English summary for the teacher (OpenAI API, structured output) |
| POST | `/v1/points` | Log a point (validated) |

## AI insights (`/insights`)

Teachers do not want z-scores; they want "who should I talk to this week, and what do I say?". `/insights` turns the `/at-risk` output into that, using the OpenAI Chat Completions API with **structured outputs**.

```
Supabase (RLS) -> point_history -> analytics.at_risk (plain Python, deterministic)
              -> pseudonymised digest (S1, S2, ... no names, no ids)
              -> OpenAI (strict JSON schema) -> pydantic re-validation
              -> map aliases back to names locally -> teacher
```

### Prompt design decisions

| Decision | Why |
|---|---|
| **Statistics are computed in Python, not by the model** | The model only explains numbers it is given, so it cannot invent a statistic. The system prompt forbids recomputing or estimating. |
| **Pseudonymise before the call** | Students are children. Only `S1..Sn`, point totals and behaviour labels leave the service. Real names are re-attached after the answer. A unit test asserts that no name or enrollment id appears in the request. |
| **Strict JSON schema** (`response_format: json_schema, strict: true`) | The reply must match `headline / summary / students_to_check[] / class_actions[]`. It is then re-validated with pydantic (length limits, types). A truncated, refused or malformed reply returns `502`, never a half answer. |
| **Allow-list of aliases** | Any alias the model returns that was not in the input is dropped. |
| **Supportive-tone rules in the system prompt** | A drop in points is framed as a reason to check in, never as blame. Actions must be small and doable this week. |
| **Low temperature (0.2), `max_completion_tokens=700`** | Consistent wording; hard cap on spend per call. |
| **No call when nobody is flagged** | The endpoint returns immediately: zero cost, zero latency. |
| **Cap of 15 flagged students per prompt** | Keeps the prompt size, and the cost, bounded for large classes. |

### Cost per request

Every response includes `usage` (model, prompt tokens, completion tokens). To see a dollar estimate, set `OPENAI_PRICE_IN_PER_1M` and `OPENAI_PRICE_OUT_PER_1M` from [OpenAI's pricing page](https://openai.com/api/pricing/); prices are read from the environment so they never go stale in code.

Size of a typical call (30-student class, 12 flagged; tokens estimated at about 4 characters each, so measure your own with the `usage` field):

| Part | Approx. tokens |
|---|---|
| System prompt + schema | ~300 |
| Digest (12 flagged students) | ~700 |
| **Input total** | **~1,000** |
| Output (summary + 12 short notes, capped at 700) | ~300-600 |

`cost = (input_tokens x price_in + output_tokens x price_out) / 1,000,000`

Illustration only (made-up prices of $0.50 in / $2.00 out per 1M tokens, 1,000 in and 400 out): (1,000 x 0.5 + 400 x 2.0) / 1e6 = **$0.0013 per request**. Replace with the real prices to get your own figure. At one summary per class per week the monthly cost is small, and the no-flags shortcut makes quiet weeks free. A cache per class and day is the obvious next step if usage grows.

### Configuration and secrets

```bash
export OPENAI_API_KEY=...            # never commit; see .env.example. In production use the host's secret store
export OPENAI_MODEL=gpt-4o-mini      # any model that supports structured outputs
```

The key is read from the environment only, is never logged, and never appears in any response. Without it the endpoint returns `503`; the other endpoints are unaffected. `.env` is git-ignored.

### Testing

The OpenAI client is faked in `tests/test_insights.py` (no network, no key, no cost). Tests cover: the happy path, the strict schema in the request, **no names or ids in the outgoing prompt**, skipping the call when nobody is flagged, invalid, truncated and refused model output, a missing key, and the cost maths.

### Limits

- The summary is a drafting aid for the teacher, not a diagnosis; a person decides what to do.
- Pilot stage: not yet validated on real class data, and the model reply was not measured against teacher judgment.

## Security design

The caller's Supabase JWT is forwarded to Supabase, so **Row-Level Security decides what each teacher can read or write**. The service-role key is never used or stored.

## Run

```bash
export SUPABASE_URL=https://<project>.supabase.co
export SUPABASE_ANON_KEY=<publishable key>
export OPENAI_API_KEY=<your key>   # only needed for /insights
pip install -r requirements.txt
uvicorn app.main:app --reload      # http://localhost:8000/docs
pytest -q
docker build -t class-pet-api . && docker run -p 8000:8000 -e SUPABASE_URL -e SUPABASE_ANON_KEY -e OPENAI_API_KEY class-pet-api
```

## Notes
- Student names are read from `students.name` (falls back to `first_name`, then the id); adjust `_names()` in `app/main.py` if your column differs.
- Not deployed yet; runs locally or in Docker.
