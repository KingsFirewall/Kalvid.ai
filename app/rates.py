"""Provider price table + cost estimation.

The Budget Guard is only as honest as this file. Two rules keep it from lying:
  1. A rate with last_verified=None is UNVERIFIED and cannot back a billable call.
  2. A rate older than settings.rate_staleness_days is flagged in the dashboard.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime

from .config import settings


class UnverifiedRate(Exception):
    """Raised when a billable call is attempted against an unverified price."""


@dataclass(frozen=True)
class Rate:
    key: str
    provider: str
    model: str
    kind: str          # 'image' | 'video'
    unit: str          # 'per_call' | 'per_second'
    usd: float
    last_verified: date | None
    source: str = ""
    # This model carries persona identity through image_url, so a persona job must
    # supply a reference still. (The model itself may accept text-only; we do not.)
    identity_via_image: bool = False
    # Some video models only emit fixed lengths; a 3s request is billed at the tier.
    # Empty for models that bill by the second.
    fixed_durations: tuple[int, ...] = ()
    # How this model spells `duration`. fal is not consistent about it and validates
    # strictly, so guessing means a 400 instead of a render:
    #   int             -> 8      (minimax)
    #   string          -> "8"    (wan, kling, seedance)
    #   seconds_suffix  -> "8s"   (veo)
    duration_format: str = "int"
    # Server-enforced bounds. A request outside them is rejected by fal with a 422
    # that arrives as a failed result rather than a refused submit — so it looks like
    # a render that failed, not a request that was never valid.
    min_duration: float = 0.0
    max_duration: float = 0.0
    # Some editing models take a LIST of conditioning images (nano-banana's
    # image_urls) rather than a single image_url. That is also how a reference pack
    # gets used as a pack rather than one plate at a time.
    reference_field: str = "image_url"
    # The model's accepted input field names. Empty = adapter uses its own defaults.
    supports: tuple[str, ...] = ()
    # Per-stage input values; the 'all' key applies to every stage.
    params: dict = field(default_factory=dict)
    # Price varies by an input parameter (e.g. resolution tier): {"480P": 0.025, ...}
    usd_by: dict = field(default_factory=dict)
    price_by: str = ""
    # Promotional pricing with a known end date. Past it the rate reads as unverified.
    price_expires: date | None = None
    price_note: str = ""
    # Token-billed models (Claude). `usd` stays the flat RESERVATION estimate; these
    # give settle() the exact cost once real usage is known.
    usd_per_mtok_in: float = 0.0
    usd_per_mtok_out: float = 0.0

    def stage_params(self, stage: str) -> dict:
        return {**self.params.get("all", {}), **self.params.get(stage, {})}

    @property
    def verified(self) -> bool:
        """Never verified, or verified at a price that has since lapsed, means no."""
        if self.last_verified is None:
            return False
        if self.price_expires and date.today() >= self.price_expires:
            return False
        return True

    @property
    def expiring_soon(self) -> bool:
        if not self.price_expires:
            return False
        return 0 <= (self.price_expires - date.today()).days <= 7

    def unit_price(self, variant: str | None = None) -> float:
        """Price per unit for a variant (e.g. '480P'), falling back to the base rate."""
        if variant and self.usd_by:
            return float(self.usd_by.get(variant, self.usd))
        return self.usd

    def age_days(self, today: date | None = None) -> int | None:
        if self.last_verified is None:
            return None
        return ((today or date.today()) - self.last_verified).days

    def is_stale(self, today: date | None = None) -> bool:
        age = self.age_days(today)
        return age is None or age > settings.rate_staleness_days

    def billed_duration(self, duration_s: float) -> float:
        """What the provider actually charges for, not what we asked for.

        Clamped to the model's declared bounds first: asking a 5s-minimum model for a
        3s draft does not produce a 3s clip, it produces a 422 — and the draft is
        billed at 5s regardless, which is what the estimate must say.
        """
        d = max(duration_s, 0.0)
        if self.min_duration:
            d = max(d, self.min_duration)
        if self.max_duration:
            d = min(d, self.max_duration)
        if not self.fixed_durations:
            return d
        allowed = sorted(self.fixed_durations)
        for opt in allowed:
            if d <= opt:
                return float(opt)
        return float(allowed[-1])

    def estimate(self, duration_s: float = 0.0, calls: int = 1,
                 variant: str | None = None) -> float:
        price = self.unit_price(variant)
        if self.unit == "per_second":
            return round(price * self.billed_duration(duration_s) * calls, 6)
        return round(price * calls, 6)

    @property
    def token_billed(self) -> bool:
        return bool(self.usd_per_mtok_in or self.usd_per_mtok_out)

    def token_cost(self, input_tokens: int, output_tokens: int) -> float:
        """Exact cost from measured usage. Cached reads are billed at ~0.1x, but the
        API reports them separately and we do not yet cache, so this is the full rate."""
        return round(input_tokens / 1e6 * self.usd_per_mtok_in
                     + output_tokens / 1e6 * self.usd_per_mtok_out, 6)

    def variant_for(self, stage: str) -> str | None:
        """The priced parameter's value for a stage — e.g. resolution '480P'."""
        return self.stage_params(stage).get(self.price_by) if self.price_by else None


class RateTable:
    def __init__(self, path=None):
        self.path = path or settings.rates_path
        self._rates: dict[str, Rate] = {}
        self._routing: dict[str, list[str]] = {}
        self.reload()

    def reload(self) -> None:
        raw = json.loads(self.path.read_text())
        rates = {}
        for key, r in raw.get("rates", {}).items():
            lv = r.get("last_verified")
            rates[key] = Rate(
                key=key,
                provider=r["provider"],
                model=r["model"],
                kind=r.get("kind", "video"),
                unit=r.get("unit", "per_call"),
                usd=float(r.get("usd") or 0.0),
                last_verified=datetime.strptime(lv, "%Y-%m-%d").date() if lv else None,
                source=r.get("source", ""),
                identity_via_image=bool(r.get("identity_via_image", False)),
                fixed_durations=tuple(r.get("fixed_durations", ()) or ()),
                duration_format=r.get("duration_format", "int"),
                min_duration=float(r.get("min_duration") or 0.0),
                max_duration=float(r.get("max_duration") or 0.0),
                reference_field=r.get("reference_field", "image_url"),
                supports=tuple(r.get("supports", ()) or ()),
                params=r.get("params", {}) or {},
                usd_by=r.get("usd_by", {}) or {},
                price_by=r.get("price_by", ""),
                price_expires=(datetime.strptime(r["price_expires"], "%Y-%m-%d").date()
                               if r.get("price_expires") else None),
                price_note=r.get("price_note", ""),
                usd_per_mtok_in=float(r.get("usd_per_mtok_in") or 0.0),
                usd_per_mtok_out=float(r.get("usd_per_mtok_out") or 0.0),
            )
        self._rates = rates
        self._routing = raw.get("routing", {})

    def get(self, key: str) -> Rate:
        if key not in self._rates:
            raise KeyError(f"No rate entry for {key!r}. Add it to {self.path.name}.")
        return self._rates[key]

    def candidates(self, stage: str) -> list[Rate]:
        """Preference-ordered options for a stage, per rates.json 'routing'."""
        return [self.get(k) for k in self._routing.get(stage, []) if k in self._rates]

    def all(self) -> list[Rate]:
        return list(self._rates.values())

    def stale(self) -> list[Rate]:
        # The mock rate is pinned far in the future; it is never a real warning.
        return [r for r in self._rates.values() if r.provider != "mock" and r.is_stale()]

    def require_billable(self, rate: Rate) -> None:
        if not rate.verified:
            raise UnverifiedRate(
                f"Rate {rate.key!r} has never been verified (last_verified=null). "
                f"Refusing a billable call against a placeholder price — the budget guard "
                f"cannot protect you with a made-up number. Set the real price and "
                f"last_verified in {self.path.name}."
            )


rate_table = RateTable()
