"""Provider interface. Adding a provider = one new adapter file, nothing else moves."""
from __future__ import annotations

import abc
from dataclasses import dataclass, field


@dataclass
class GenerationRequest:
    model: str
    kind: str                       # 'image' | 'video'
    prompt: str
    negative_prompt: str = ""
    reference_image_url: str | None = None   # the persona's locked face
    identity_lock_id: str | None = None      # LoRA / provider-side character id
    duration_s: float = 0.0
    # Resolution tier for models that take an enum (fal's minimax: 480P / 768P).
    resolution: str | None = None
    # Falls back for providers that take explicit pixel dimensions instead.
    width: int = 720
    height: int = 1280              # 9:16 default — TikTok/Reels/Shorts
    end_image_url: str | None = None
    seed: int | None = None
    # The model's accepted input names, from rates.json. When set, an adapter must
    # send nothing outside this list — an unsupported field is an error, not a no-op.
    supports: tuple[str, ...] = ()
    extra: dict = field(default_factory=dict)

    def accepts(self, field_name: str) -> bool:
        return not self.supports or field_name in self.supports


@dataclass
class GenerationResult:
    status: str                     # 'running' | 'succeeded' | 'failed'
    provider_job_id: str | None = None
    output_url: str | None = None
    # None means "the provider did not tell us". The ledger then falls back to the
    # estimate rather than recording a false $0.
    cost_usd: float | None = None
    error: str | None = None
    raw: dict = field(default_factory=dict)


class ProviderError(Exception):
    pass


class Provider(abc.ABC):
    name: str = "base"
    billable: bool = True

    @abc.abstractmethod
    def available(self) -> bool:
        """False when unconfigured or known-down, so the router can skip to the next."""

    @abc.abstractmethod
    def submit(self, req: GenerationRequest) -> GenerationResult:
        """Fire the call. Returns immediately with a provider_job_id for polling."""

    @abc.abstractmethod
    def poll(self, provider_job_id: str, req: GenerationRequest) -> GenerationResult:
        """Check a submitted job. Must be safe to call repeatedly."""
