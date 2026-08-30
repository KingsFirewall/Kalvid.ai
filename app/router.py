"""Provider Router — picks the cheapest workable option for a stage.

Draft stage : cheapest candidate that is configured and up (Runware first).
Final stage : fal.ai, for the SLA and predictable pricing.

Selection order comes from rates.json 'routing', so re-prioritising providers is a
config edit, not a code change.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import settings
from .providers.base import Provider, ProviderError
from .providers.fal import FalProvider
from .providers.mock import MockProvider
from .providers.runware import RunwareProvider
from .rates import Rate, rate_table

log = logging.getLogger(__name__)


class NoProviderAvailable(Exception):
    pass


@dataclass
class Route:
    """What will run, and what it would cost.

    In dry-run these diverge on purpose: `provider` is the free mock, while `rate`
    stays the REAL price. That way the dashboard shows an operator the true cost of a
    render before they ever go live — a dry run that reported $0 estimates would be a
    rehearsal of nothing. `billing_rate` is what actually hits the ledger, so dry-run
    spend is still recorded as $0.
    """
    provider: Provider
    rate: Rate                       # real price — what this WOULD cost
    billing_rate: Rate | None = None  # what the ledger charges; None = same as rate

    @property
    def billable(self) -> bool:
        return self.provider.billable

    @property
    def charged(self) -> Rate:
        return self.billing_rate or self.rate


class ProviderRouter:
    def __init__(self, dry_run: bool | None = None):
        self.dry_run = settings.dry_run if dry_run is None else dry_run
        self._mock = MockProvider()
        self._providers: dict[str, Provider] = {
            "mock": self._mock,
            "fal": FalProvider(),
            "runware": RunwareProvider(),
        }

    def get(self, name: str) -> Provider:
        if name not in self._providers:
            raise NoProviderAvailable(f"unknown provider {name!r}")
        return self._providers[name]

    def resolve(self, stage: str, *, kind: str | None = None) -> Route:
        """First configured, available candidate for the stage.

        In dry-run every route collapses to the mock provider, but the REAL rate is
        kept so estimates and the budget guard behave exactly as they will live.
        """
        candidates = rate_table.candidates(stage)
        if not candidates:
            raise NoProviderAvailable(f"no routing candidates configured for {stage!r}")

        if kind:
            filtered = [r for r in candidates if r.kind == kind]
            candidates = filtered or candidates

        if self.dry_run:
            real = candidates[0]
            mock_key = "mock:mock-clip" if real.kind == "video" else "mock:mock-still"
            return Route(self._mock, real, billing_rate=rate_table.get(mock_key))

        tried = []
        for rate in candidates:
            provider = self._providers.get(rate.provider)
            if provider is None:
                tried.append(f"{rate.key} (no adapter)")
                continue
            if not provider.available():
                tried.append(f"{rate.key} (unconfigured/down)")
                continue
            if not rate.verified:
                # Refusing here is the point: a placeholder price makes the cap a lie.
                tried.append(f"{rate.key} (UNVERIFIED price)")
                continue
            return Route(provider, rate)

        raise NoProviderAvailable(
            f"no usable provider for stage {stage!r}. Tried: " + "; ".join(tried)
        )

    def route_for_rate_key(self, key: str) -> Route:
        """Explicit override when a job needs one specific model."""
        rate = rate_table.get(key)
        if self.dry_run:
            mock_key = "mock:mock-clip" if rate.kind == "video" else "mock:mock-still"
            return Route(self._mock, rate, billing_rate=rate_table.get(mock_key))
        provider = self._providers.get(rate.provider)
        if provider is None or not provider.available():
            raise NoProviderAvailable(f"{rate.provider} unavailable for {key}")
        return Route(provider, rate)


router = ProviderRouter()
