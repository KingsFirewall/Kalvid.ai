"""Prompt structuring — the CinePrompt-style layer.

Expands a loose brief into explicit technical fields before it reaches a provider.
Costs nothing and is the highest-leverage piece in the system: the difference between
one clean draft and five muddy attempts is mostly prompt precision.

Deliberately deterministic and template-driven — no LLM call, so structuring is free,
instant, and produces the same prompt for the same brief (which makes a re-draft a
real comparison rather than a fresh roll of the dice).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Vertical-first: these are TikTok/Reels/Shorts assets.
PLATFORM_SPECS = {
    "tiktok":    {"aspect": "9:16", "width": 720,  "height": 1280, "style": "handheld, natural, UGC-authentic"},
    "instagram": {"aspect": "9:16", "width": 720,  "height": 1280, "style": "polished, well-lit, lifestyle"},
    "youtube":   {"aspect": "16:9", "width": 1280, "height": 720,  "style": "clean, stable, editorial"},
    "shorts":    {"aspect": "9:16", "width": 720,  "height": 1280, "style": "punchy, high-energy, UGC-authentic"},
}

# A UGC clip is a person talking to a phone. These defaults encode that so the model
# is not left to invent framing.
DEFAULTS = {
    "shot_type": "medium close-up, chest-up framing",
    "camera": "handheld phone camera, slight natural sway, eye-level",
    "lens": "28mm equivalent, mild wide-angle selfie perspective",
    "lighting": "soft diffused daylight from a window, key on the face, no harsh shadows",
    "motion": "subject speaks to camera with natural micro-movements and blinks",
    "background": "lightly blurred domestic interior, uncluttered",
    "grade": "true-to-life color, neutral white balance, no heavy filter",
}

# What consistently ruins a UGC take. Cheaper to forbid up front than to re-render.
NEGATIVE = (
    "distorted face, warped features, extra fingers, deformed hands, "
    "mismatched identity, face morphing between frames, text artifacts, watermark, "
    "logo, oversaturated, plastic skin, uncanny smoothing, jitter, flicker"
)


@dataclass
class StructuredPrompt:
    subject: str
    action: str
    shot_type: str
    camera: str
    lens: str
    lighting: str
    motion: str
    background: str
    grade: str
    aspect: str
    duration_s: int
    spoken_line: str = ""
    negative: str = NEGATIVE
    overrides: dict = field(default_factory=dict)

    def render(self) -> str:
        parts = [
            f"{self.subject}. {self.action}.",
            f"Shot: {self.shot_type}.",
            f"Camera: {self.camera}.",
            f"Lens: {self.lens}.",
            f"Lighting: {self.lighting}.",
            f"Motion: {self.motion}.",
            f"Background: {self.background}.",
            f"Color: {self.grade}.",
            f"Format: {self.aspect} vertical, {self.duration_s}s."
                if self.aspect == "9:16" else
                f"Format: {self.aspect}, {self.duration_s}s.",
        ]
        if self.spoken_line:
            parts.insert(1, f'Speaking directly to camera: "{self.spoken_line}".')
        return " ".join(parts)

    def to_dict(self) -> dict:
        return {
            "prompt": self.render(), "negative_prompt": self.negative,
            "subject": self.subject, "action": self.action,
            "shot_type": self.shot_type, "camera": self.camera, "lens": self.lens,
            "lighting": self.lighting, "motion": self.motion,
            "background": self.background, "grade": self.grade,
            "aspect": self.aspect, "duration_s": self.duration_s,
            "spoken_line": self.spoken_line,
        }


def _extract_spoken_line(brief: str) -> tuple[str, str]:
    """Pull a quoted line out of the brief — that is the script, not scene description."""
    m = re.search(r'["\u201c]([^"\u201d]{4,300})["\u201d]', brief)
    if not m:
        return "", brief
    remainder = brief[: m.start()] + " " + brief[m.end():]
    # Removing the quote can strand the verb that introduced it ("...and says  then
    # holds it up"). Drop those so the action reads as a clean scene description.
    remainder = re.sub(r"\b(and\s+)?(says?|said|saying|tells?|adds?)\b\s*[,:]?\s*",
                       " ", remainder, flags=re.I)
    remainder = re.sub(r"\s*,\s*(?=,|$)", " ", remainder)
    remainder = re.sub(r"^\s*(and|then|,)\s+", "", remainder.strip(), flags=re.I)
    remainder = re.sub(r"\s+([,.;:])", r"\1", remainder)      # no space before punctuation
    remainder = re.sub(r"([,;:])\s*(?=[,.;:])", "", remainder)  # no doubled punctuation
    return m.group(1).strip(), " ".join(remainder.split()).strip(" ,;:")


def structure(
    brief: str,
    *,
    persona_name: str = "the subject",
    persona_notes: str = "",
    platform: str = "tiktok",
    duration_s: int = 8,
    overrides: dict | None = None,
) -> StructuredPrompt:
    overrides = overrides or {}
    spec = PLATFORM_SPECS.get(platform.lower(), PLATFORM_SPECS["tiktok"])
    spoken, remainder = _extract_spoken_line(brief)

    subject = persona_name
    if persona_notes:
        subject = f"{persona_name} ({persona_notes.strip().rstrip('.')})"

    action = " ".join(remainder.split()) or "speaking directly to camera"
    # The action stands as its own sentence after the subject, so give it a capital.
    action = action[:1].upper() + action[1:]

    def pick(key: str) -> str:
        return overrides.get(key) or DEFAULTS[key]

    lighting = pick("lighting")
    motion = pick("motion")
    if spoken:
        motion = f"{motion}, lip-sync matched to the spoken line"

    return StructuredPrompt(
        subject=subject,
        action=action,
        shot_type=pick("shot_type"),
        camera=pick("camera"),
        lens=pick("lens"),
        lighting=lighting,
        motion=motion,
        background=pick("background"),
        grade=f"{pick('grade')}, {spec['style']}",
        aspect=spec["aspect"],
        duration_s=duration_s,
        spoken_line=spoken,
        overrides=overrides,
    )
