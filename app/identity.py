"""Persona identity locking.

The PRD asks for "one locked reference identity, reused across every video so the
face stays consistent." There is no single cross-provider primitive for that, so the
strategy is made explicit per persona rather than assumed:

  reference_image  — a locked still is passed as the image-to-video seed on every
                     generation. Cheapest, zero setup, works everywhere. Consistency
                     is good frame-to-frame but drifts across clips, especially on
                     large head turns or long durations.
  lora             — a small character model trained once on the persona, then applied
                     to every generation. Strongest consistency; costs an upfront
                     training run and is tied to models that accept the adapter.
  character_id     — a provider-side persistent character handle, where the provider
                     offers one. Strong consistency, but locks the persona to that
                     provider.

Whichever is chosen, the same locked reference still is what a human compares each
draft against — that is the check that catches drift before the final render pays for it.
"""
from __future__ import annotations

from dataclasses import dataclass

STRATEGIES = ("reference_image", "lora", "character_id")


@dataclass
class IdentityBinding:
    strategy: str
    reference_image_url: str | None
    identity_lock_id: str | None

    def validate(self) -> list[str]:
        """Problems that would let a generation run without a locked identity."""
        problems = []
        if self.strategy not in STRATEGIES:
            problems.append(f"unknown identity_strategy {self.strategy!r}")
        if self.strategy == "reference_image" and not self.reference_image_url:
            problems.append("strategy 'reference_image' needs reference_image_url set")
        if self.strategy in ("lora", "character_id") and not self.identity_lock_id:
            problems.append(f"strategy {self.strategy!r} needs identity_lock_id set")
        if self.strategy in ("lora", "character_id") and not self.reference_image_url:
            # Not fatal, but without it a reviewer has nothing to compare a draft to.
            problems.append(
                "no reference_image_url: drafts can't be visually checked against a "
                "canonical face (warning)"
            )
        return problems

    @property
    def blocking_problems(self) -> list[str]:
        return [p for p in self.validate() if not p.endswith("(warning)")]

    def apply(self, req) -> None:
        """Attach this persona's identity to an outgoing GenerationRequest."""
        req.reference_image_url = self.reference_image_url
        req.identity_lock_id = self.identity_lock_id
        if self.strategy == "lora" and self.identity_lock_id:
            req.extra.setdefault("loras", [{"path": self.identity_lock_id, "scale": 1.0}])
        elif self.strategy == "character_id" and self.identity_lock_id:
            req.extra.setdefault("character_id", self.identity_lock_id)


def binding_from_row(row) -> IdentityBinding:
    return IdentityBinding(
        strategy=row["identity_strategy"],
        reference_image_url=row["reference_image_url"],
        identity_lock_id=row["identity_lock_id"],
    )
