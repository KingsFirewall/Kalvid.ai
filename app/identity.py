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

import json
from dataclasses import dataclass

from . import db

STRATEGIES = ("reference_image", "lora", "character_id")

# The canonical plates of a reference pack. `identity` is the primary conditioning
# image; the rest exist so a reviewer can check drift from more than one angle.
# These belong to the LOCKED layer: they are frozen into an identity version and
# protected from deletion once something has rendered against them.
IDENTITY_PLATES = ("identity", "turnaround", "detail", "expression")

# The VARIABLE layer: swapped per scene, never frozen. Keeping these out of the
# identity version is the whole point of the separation — a pair of glasses is not
# part of who someone is, and letting a variable leak into the locked layer means
# every future render inherits it.
VARIABLE_PLATES = ("wardrobe", "product")

PLATES = IDENTITY_PLATES + VARIABLE_PLATES

# What a character sheet captures (SOP 2.3). Descriptive metadata that REINFORCES the
# reference pack and drives script/voice generation — it never replaces the pack,
# because text cannot hold a face.
SHEET_FIELDS = (
    "facial_features", "hair", "skin", "body_type", "distinguishing_marks",
    "default_styling", "personality", "speaking_style", "tone", "niche", "backstory",
)

SHEET_LABELS = {
    "facial_features":      "Facial features",
    "hair":                 "Hair",
    "skin":                 "Skin",
    "body_type":            "Body type & proportions",
    "distinguishing_marks": "Distinguishing marks",
    "default_styling":      "Default styling",
    "personality":          "Personality",
    "speaking_style":       "Speaking style",
    "tone":                 "Tone",
    "niche":                "Niche",
    "backstory":            "Backstory",
}

PLATE_LABELS = {
    "identity":   "Identity plate — the primary conditioning image",
    "turnaround": "Turnaround — front, side, back, three-quarter",
    "detail":     "Detail — eyes, face, lips, skin, hair",
    "expression": "Expression — neutral, smiling, speaking, laughing",
    "wardrobe":   "Wardrobe / prop — garment, glasses, watch, jewellery",
    "product":    "Product — the thing being advertised",
}

# Capture rules for a reference plate. These are not style preferences: any lighting
# baked into a plate contaminates every downstream generation. Lock a face under warm
# sunset light and you fight that warmth in every night and indoor scene forever.
PLATE_CAPTURE_RULES = (
    "flat neutral background, 18% grey or pure white",
    "soft even shadowless lighting, no cast shadow",
    "neutral base garment, simple black top",
    "no dramatic accessories, no strong colour cast",
    "no golden hour, no rim light, no coloured gels",
)


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


# --------------------------------------------------------------- versioning
#
# Identity is immutable once locked. An edit creates the next version; everything
# already rendered stays pinned to the version it was made with.
#
# This is not bookkeeping. A persona's reference image is the single input that
# decides who appears on screen. Overwriting it in place retroactively changes the
# provenance of every clip ever delivered under the old face — and there is no way
# to tell, afterwards, which version a given video actually used.


class IdentityLocked(Exception):
    """A locked version cannot be edited. Create the next one instead."""


def versions(persona_id: int) -> list[dict]:
    return [dict(r) for r in db.query(
        "SELECT * FROM identity_versions WHERE persona_id=? ORDER BY version DESC",
        (persona_id,))]


def current_version(persona_id: int) -> dict | None:
    """The highest-numbered LOCKED version — what a new job would pin to."""
    row = db.query_one(
        """SELECT * FROM identity_versions
            WHERE persona_id=? AND status IN ('locked','superseded')
            ORDER BY version DESC LIMIT 1""", (persona_id,))
    return dict(row) if row else None


def draft_version(persona_id: int) -> dict | None:
    row = db.query_one(
        "SELECT * FROM identity_versions WHERE persona_id=? AND status='draft'",
        (persona_id,))
    return dict(row) if row else None


def get_version(version_id: int) -> dict:
    row = db.query_one("SELECT * FROM identity_versions WHERE id=?", (version_id,))
    if row is None:
        raise KeyError(f"identity version {version_id} not found")
    return dict(row)


def open_draft(persona_id: int, **changes) -> dict:
    """Start (or update) the next version. Never touches a locked one.

    Seeded from the current locked version so an edit is a diff, not a re-entry of
    everything that is not changing.
    """
    existing = draft_version(persona_id)
    if existing is not None:
        if changes:
            fields = {k: v for k, v in changes.items() if k in _EDITABLE}
            if fields:
                sets = ", ".join(f"{k}=?" for k in fields)
                db.execute(f"UPDATE identity_versions SET {sets} WHERE id=?",
                           (*fields.values(), existing["id"]))
        return get_version(existing["id"])

    base = current_version(persona_id) or {}
    persona = db.query_one("SELECT * FROM personas WHERE id=?", (persona_id,))
    if persona is None:
        raise KeyError(f"persona {persona_id} not found")

    def pick(field, fallback=None):
        if field in changes:
            return changes[field]
        if base:
            return base.get(field)
        return persona[field] if field in persona.keys() else fallback

    nxt = (db.query_one(
        "SELECT COALESCE(MAX(version),0) AS v FROM identity_versions WHERE persona_id=?",
        (persona_id,))["v"]) + 1
    vid = db.insert(
        """INSERT INTO identity_versions
              (persona_id, version, status, identity_strategy, reference_image_url,
               identity_lock_id, character_sheet, voice_profile, notes)
           VALUES (?,?, 'draft', ?,?,?,?,?,?)""",
        (persona_id, nxt, pick("identity_strategy", "reference_image"),
         pick("reference_image_url"), pick("identity_lock_id"),
         pick("character_sheet"), pick("voice_profile"), pick("notes")))
    return get_version(vid)


def lock(persona_id: int, locked_by: str = "operator") -> dict:
    """Freeze the open draft. From here it is immutable.

    The persona row is updated to match, but only as a cache of "current" — the
    version rows remain the record of what each render actually used.
    """
    draft = draft_version(persona_id)
    if draft is None:
        raise KeyError(f"persona {persona_id} has no open draft to lock")

    binding = IdentityBinding(draft["identity_strategy"], draft["reference_image_url"],
                              draft["identity_lock_id"])
    problems = binding.blocking_problems
    if problems:
        # Locking a broken identity would pin every future render to something that
        # cannot generate. Refuse here, where it is free to fix.
        raise IdentityLocked(
            "cannot lock an identity that could not generate: " + "; ".join(problems))

    db.execute(
        """UPDATE identity_versions SET status='superseded'
            WHERE persona_id=? AND status='locked'""", (persona_id,))
    db.execute(
        """UPDATE identity_versions
              SET status='locked', locked_at=CURRENT_TIMESTAMP, locked_by=?
            WHERE id=?""", (locked_by, draft["id"]))
    db.execute(
        """UPDATE personas SET identity_strategy=?, reference_image_url=?,
                  identity_lock_id=?, voice_profile=?, notes=?
            WHERE id=?""",
        (draft["identity_strategy"], draft["reference_image_url"],
         draft["identity_lock_id"], draft["voice_profile"], draft["notes"], persona_id))
    return get_version(draft["id"])


def edit(persona_id: int, locked_by: str = "operator", **changes) -> dict:
    """Open a draft with these changes and lock it immediately — one new version."""
    open_draft(persona_id, **changes)
    return lock(persona_id, locked_by)


_EDITABLE = ("identity_strategy", "reference_image_url", "identity_lock_id",
             "character_sheet", "voice_profile", "notes")


def binding_from_version(version: dict) -> IdentityBinding:
    """The binding a render should use — read from the PINNED version, not the persona.

    This is the whole point of versioning: a job created against v1 renders against
    v1 even after the persona has moved to v2.
    """
    return IdentityBinding(
        strategy=version["identity_strategy"],
        reference_image_url=version["reference_image_url"],
        identity_lock_id=version["identity_lock_id"],
    )


# Kept for callers that still hold a raw personas row (the persona's CURRENT identity,
# not a pinned one). Prefer binding_from_version anywhere a render is involved.


def sheet(version: dict) -> dict:
    try:
        return json.loads(version.get("character_sheet") or "{}")
    except (ValueError, TypeError):
        return {}
