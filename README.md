# Class Pet Garden Analytics API

A small FastAPI service on top of the [Class Pet Garden](https://github.com/BensonAnalyst/class-pet-garden) database. It adds the **behaviour analytics and early-warning flags** from that project's roadmap, as a documented REST API.

## Before vs After

| | Before (browser talks straight to Supabase) | After (this API) |
|---|---|---|
| Analytics | None; teacher eyeballs points | `/at-risk` flags students whose points dropped (negative net, or >2 sigma below class change) |
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
| POST | `/v1/points` | Log a point (validated) |

## Security design

The caller's Supabase JWT is forwarded to Supabase, so **Row-Level Security decides what each teacher can read or write**. The service-role key is never used or stored.

## Run

```bash
export SUPABASE_URL=https://<project>.supabase.co
export SUPABASE_ANON_KEY=<publishable key>
pip install -r requirements.txt
uvicorn app.main:app --reload      # http://localhost:8000/docs
pytest -q
docker build -t class-pet-api . && docker run -p 8000:8000 -e SUPABASE_URL -e SUPABASE_ANON_KEY class-pet-api
```

## Notes
- Student names are read from `students.name` (falls back to `first_name`, then the id); adjust `_names()` in `app/main.py` if your column differs.
- Not deployed yet; runs locally or in Docker.
