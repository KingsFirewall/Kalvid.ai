"""Preflight checks — verify credentials WITHOUT spending money.

Every probe here is free: no generation is ever submitted. Provider auth is checked
by hitting an endpoint that distinguishes "bad key" (401/403) from "key fine, no such
resource" (404), which costs nothing either way.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import httpx

from .config import settings
from .rates import rate_table


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    blocking: bool = True      # would this stop a live render?

    @property
    def mark(self) -> str:
        return "PASS" if self.ok else ("FAIL" if self.blocking else "WARN")


def _placeholder(v: str | None) -> bool:
    return not v or v.startswith("YOUR_") or "YOUR-PROJECT-REF" in v or "YOUR_DB_PASSWORD" in v


# ---------------------------------------------------------------- providers

def check_fal() -> Check:
    if _placeholder(settings.fal_api_key):
        return Check("fal.ai credentials", False, "FAL_KEY is unset or still a placeholder")
    try:
        # Free auth probe: ask for a request id that cannot exist. A valid key gets a
        # 404/422; an invalid one gets 401/403. Nothing is queued, nothing is charged.
        r = httpx.get(
            "https://queue.fal.run/fal-ai/any/requests/00000000-0000-0000-0000-000000000000/status",
            headers={"Authorization": f"Key {settings.fal_api_key}"},
            timeout=20.0,
        )
    except Exception as exc:
        return Check("fal.ai credentials", False, f"could not reach fal: {exc}")
    if r.status_code in (401, 403):
        return Check("fal.ai credentials", False, f"rejected (HTTP {r.status_code}) — key is wrong")
    return Check("fal.ai credentials", True, f"key accepted (probe returned HTTP {r.status_code})")


def check_runware() -> Check:
    if _placeholder(settings.runware_api_key):
        return Check("Runware credentials", False,
                     "RUNWARE_API_KEY is unset or still a placeholder", blocking=False)
    try:
        # Deliberately malformed task: auth is evaluated before the payload, so a bad
        # key gives 401 while a good key gives a validation error. No task is run.
        r = httpx.post(
            "https://api.runware.ai/v1",
            headers={"Authorization": f"Bearer {settings.runware_api_key}",
                     "Content-Type": "application/json"},
            json=[{"taskType": "__preflight__"}],
            timeout=20.0,
        )
    except Exception as exc:
        return Check("Runware credentials", False, f"could not reach Runware: {exc}", blocking=False)
    if r.status_code in (401, 403):
        return Check("Runware credentials", False,
                     f"rejected (HTTP {r.status_code}) — key is wrong", blocking=False)
    return Check("Runware credentials", True,
                 f"key accepted (probe returned HTTP {r.status_code})", blocking=False)


# ---------------------------------------------------------------- supabase

def check_supabase() -> list[Check]:
    if not settings.supabase_configured:
        return [Check("Supabase credentials", False,
                      "SUPABASE_URL / SERVICE_ROLE_KEY unset or still placeholders",
                      blocking=False)]
    headers = {"Authorization": f"Bearer {settings.supabase_service_role_key}",
               "apikey": settings.supabase_service_role_key}
    try:
        r = httpx.get(f"{settings.supabase_url}/storage/v1/bucket",
                      headers=headers, timeout=20.0)
    except Exception as exc:
        return [Check("Supabase reachable", False, f"could not reach Supabase: {exc}",
                      blocking=False)]

    if r.status_code in (401, 403):
        return [Check("Supabase credentials", False,
                      f"rejected (HTTP {r.status_code}) — service role key is wrong",
                      blocking=False)]
    if r.status_code >= 400:
        return [Check("Supabase credentials", False,
                      f"HTTP {r.status_code}: {r.text[:120]}", blocking=False)]

    checks = [Check("Supabase credentials", True, "service role key accepted")]
    buckets = [b.get("name") for b in r.json()] if isinstance(r.json(), list) else []
    if settings.supabase_bucket in buckets:
        checks.append(Check(f"bucket '{settings.supabase_bucket}'", True, "exists"))
    else:
        checks.append(Check(
            f"bucket '{settings.supabase_bucket}'", False,
            f"not found. Existing buckets: {', '.join(buckets) or 'none'}. "
            f"Create it in Storage, or change SUPABASE_STORAGE_BUCKET.",
            blocking=False))
    return checks


# ---------------------------------------------------------------- rates

def check_rates() -> list[Check]:
    checks = []
    for stage in ("draft", "final"):
        try:
            candidates = rate_table.candidates(stage)
        except KeyError as exc:
            checks.append(Check(f"{stage} routing", False, str(exc)))
            continue
        if not candidates:
            checks.append(Check(f"{stage} routing", False, "no candidates configured"))
            continue
        verified = [r for r in candidates if r.verified]
        if verified:
            r = verified[0]
            variant = r.variant_for(stage)
            checks.append(Check(
                f"{stage} price verified", True,
                f"{r.key} @ ${r.unit_price(variant):g}/{r.unit.replace('per_', '')}"
                + (f" ({variant})" if variant else "")))
            if r.expiring_soon:
                days = (r.price_expires - date.today()).days
                checks.append(Check(
                    f"{stage} price expiry", False,
                    f"price lapses in {days} day(s) on {r.price_expires}"
                    + (f" — {r.price_note}" if r.price_note else "")
                    + ". Live calls will be refused until you re-verify.",
                    blocking=False))
        else:
            checks.append(Check(
                f"{stage} price verified", False,
                f"none of {[r.key for r in candidates]} has a verified price — "
                f"the router will refuse every billable call for this stage"))
    return checks


def run_all() -> list[Check]:
    checks: list[Check] = []
    if settings.dry_run:
        checks.append(Check("mode", True,
                            "DRY RUN — nothing is billable. Set KALVID_DRY_RUN=false to go live.",
                            blocking=False))
    else:
        checks.append(Check("mode", True, "LIVE — provider calls will be charged"))
    checks.append(check_fal())
    checks.append(check_runware())
    checks.extend(check_supabase())
    checks.extend(check_rates())
    return checks
