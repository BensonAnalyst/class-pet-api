"""Supabase PostgREST access. The caller's own JWT is forwarded, so the
database's Row-Level Security decides what each teacher may see or write.
The service-role key is never used."""
from __future__ import annotations

import os

import httpx

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")


class SupabaseSource:
    def __init__(self, jwt: str):
        self._headers = {"apikey": SUPABASE_ANON_KEY, "Authorization": f"Bearer {jwt}"}

    def _get(self, path: str, params: dict) -> list[dict]:
        r = httpx.get(f"{SUPABASE_URL}/rest/v1/{path}", headers=self._headers, params=params, timeout=15)
        if r.status_code in (401, 403):
            raise PermissionError("Supabase rejected the token")
        r.raise_for_status()
        return r.json()

    def enrollments(self, class_id: str) -> list[dict]:
        return self._get("enrollments", {"class_id": f"eq.{class_id}", "select": "id,students(*)"})

    def history(self, enrollment_ids: list[str]) -> list[dict]:
        if not enrollment_ids:
            return []
        ids = ",".join(enrollment_ids)
        return self._get("point_history", {"enrollment_id": f"in.({ids})",
                                           "select": "enrollment_id,label,delta,created_at",
                                           "order": "created_at.asc"})

    def history_for(self, enrollment_id: str) -> list[dict]:
        return self.history([enrollment_id])

    def add_point(self, enrollment_id: str, label: str, delta: int, logged_by: str | None) -> dict:
        body = {"enrollment_id": enrollment_id, "label": label, "delta": delta, "logged_by": logged_by}
        r = httpx.post(f"{SUPABASE_URL}/rest/v1/point_history", headers={**self._headers, "Prefer": "return=representation"},
                       json=body, timeout=15)
        if r.status_code in (401, 403):
            raise PermissionError("Not allowed to write this point")
        r.raise_for_status()
        return r.json()[0]
