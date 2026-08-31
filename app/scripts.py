"""Claude writes the dialogue. It does not write the camera directions.

That split is the whole design. `app/prompts.py` stays deterministic — the same brief
always produces the same shot, lens, lighting and framing, which is what makes a
re-draft a real comparison instead of a fresh roll of the dice. Handing those fields to
a model would throw that away for no gain: nobody needs creative variation in
"28mm equivalent, eye-level".

What a model is genuinely better at is the part templates cannot do — writing a line
that sounds like a specific person selling a specific thing in eight seconds.

So this module produces exactly two artifacts, kept separate as the platform SOP
requires: a `dialogue` (what she says) and a `visual_direction` (what the camera sees).
The visual direction is a *suggestion* that flows into the brief; the technical prompt
is still built deterministically from it downstream.

Cost: roughly a cent per script, against $0.32–$1.50 for the video it feeds. It is
metered anyway, through the same reserve/settle ledger as everything else, because
"every paid call gets a row before it fires" is an invariant worth more than the money.
"""
from __future__ import annotations

import json
import logging

from . import db, identity as identity_mod, ledger
from .config import settings
from .rates import UnverifiedRate, rate_table

log = logging.getLogger(__name__)

RATE_KEY = "anthropic:claude-opus-5"
MAX_TOKENS = 2000


class ScriptError(Exception):
    """The script could not be produced — no key, no persona, or a refusal."""


SYSTEM = """You write short-form UGC ad scripts for AI-generated influencers.

You are writing ONE spoken line plus a short visual direction. Not a screenplay, not a \
storyboard, not marketing copy.

What makes these work:
- A real person talks like a person. Contractions, half-sentences, one idea.
- The first three words decide whether anyone watches the rest.
- Specific beats generic. "My jaw actually dropped" beats "amazing results".
- Never write a slogan. Never write ad-copy cadence. Never say "game-changer", \
"level up", "obsessed with", "run don't walk", or "here's the thing".
- No emoji, no hashtags, no stage directions inside the dialogue.

The dialogue must be speakable in the given duration at a natural pace — roughly \
two and a half words per second. Count, and stay under it.

The visual direction describes only what the camera sees: setting, what her hands are \
doing, what the product does. One or two sentences. Do NOT specify lens, focal length, \
lighting setup, colour grade, aspect ratio or camera model — those are set elsewhere \
and yours will be discarded."""


SCHEMA = {
    "type": "object",
    "properties": {
        "hook": {
            "type": "string",
            "description": "The first 3-6 words of the dialogue, which decide "
                           "whether anyone keeps watching.",
        },
        "dialogue": {
            "type": "string",
            "description": "The complete spoken line, including the hook. "
                           "Speakable within the duration at a natural pace.",
        },
        "visual_direction": {
            "type": "string",
            "description": "What the camera sees: setting, hands, product. "
                           "One or two sentences. No lens or lighting.",
        },
        "word_count": {
            "type": "integer",
            "description": "Number of words in `dialogue`.",
        },
        "why_it_works": {
            "type": "string",
            "description": "One short sentence on the choice made, for the operator.",
        },
    },
    "required": ["hook", "dialogue", "visual_direction", "word_count", "why_it_works"],
    "additionalProperties": False,
}


def available() -> bool:
    return settings.provider_configured("anthropic")


def _persona_context(persona_id: int) -> tuple[dict, dict]:
    persona = db.query_one("SELECT * FROM personas WHERE id=?", (persona_id,))
    if persona is None:
        raise KeyError(f"persona {persona_id} not found")
    version = identity_mod.current_version(persona_id)
    return dict(persona), (version or {})


def _user_prompt(persona: dict, version: dict, *, scene: str, product: str,
                 platform: str, duration_s: int, tone: str) -> str:
    sheet = identity_mod.sheet(version) if version else {}
    lines = [
        f"INFLUENCER: {persona['name']}",
    ]
    if persona.get("notes"):
        lines.append(f"Appearance: {persona['notes']}")
    if persona.get("voice_profile"):
        lines.append(f"Voice: {persona['voice_profile']}")
    for key in ("personality", "speaking_style", "catchphrases", "niche", "backstory"):
        if sheet.get(key):
            lines.append(f"{key.replace('_', ' ').title()}: {sheet[key]}")
    lines += [
        "",
        f"PLATFORM: {platform}",
        f"DURATION: {duration_s} seconds "
        f"(about {max(4, int(duration_s * 2.5))} words maximum)",
    ]
    if tone:
        lines.append(f"TONE: {tone}")
    if product:
        lines.append(f"PRODUCT: {product}")
    lines += ["", "SCENE:", scene.strip()]
    return "\n".join(lines)


def _mock(scene: str, duration_s: int) -> dict:
    """Dry-run script. Deterministic and obviously fake, so nobody ships it by accident."""
    subject = " ".join(scene.strip().split()[:6]) or "the product"
    return {
        "hook": "Okay so I was wrong",
        "dialogue": f"Okay so I was wrong about {subject} — three days in and I'm "
                    f"genuinely surprised.",
        "visual_direction": f"She holds the product up to the camera on a bright "
                            f"counter, turning it once. {scene.strip()[:120]}",
        "word_count": 16,
        "why_it_works": "DRY RUN — this is placeholder text from the mock writer, "
                        "not Claude. Set ANTHROPIC_API_KEY and KALVID_DRY_RUN=false.",
        "mock": True,
    }


def preview(persona_id: int) -> dict:
    """What a script would cost, and whether it can run at all."""
    try:
        rate = rate_table.get(RATE_KEY)
    except KeyError as exc:
        return {"error": str(exc)}
    return {
        "model": rate.model,
        "rate_key": rate.key,
        "estimate_usd": rate.usd,
        "billable": not settings.dry_run and available(),
        "configured": available(),
        "dry_run": settings.dry_run,
        "rate_verified": rate.verified,
        "note": rate.price_note,
    }


def generate(*, persona_id: int, scene: str, platform: str = "tiktok",
             duration_s: int = 8, product: str = "", tone: str = "",
             override_by: str | None = None) -> dict:
    """Write one script. Returns the artifacts plus what it cost.

    Charged to the persona's client through the same guard as a render — a script is
    cheap, not free, and an untracked paid call is exactly what this system exists to
    prevent.
    """
    if not scene.strip():
        raise ScriptError("describe the scene first — there is nothing to write from")

    persona, version = _persona_context(persona_id)
    rate = rate_table.get(RATE_KEY)
    scope = ledger.scope_for_client(persona["client_id"], persona_id)
    live = available() and not settings.dry_run

    gen_id = ledger.reserve(
        stage="script", scope=scope, rate=rate, duration_s=0.0,
        identity_version_id=version.get("id"),
        payload=json.dumps({"model": rate.model, "scene": scene[:2000],
                            "platform": platform, "duration_s": duration_s}),
        billable=live, override_by=override_by,
    )

    if not live:
        result = _mock(scene, duration_s)
        ledger.settle(gen_id, status="succeeded", actual_cost_usd=0.0,
                      output_url=None, error=None)
        result["generation_id"] = gen_id
        result["cost_usd"] = 0.0
        result["model"] = "mock-writer" if not available() else f"{rate.model} (dry run)"
        return result

    try:
        import anthropic
    except ImportError as exc:                                   # pragma: no cover
        ledger.release(gen_id, "anthropic SDK not installed")
        raise ScriptError(f"anthropic SDK not installed: {exc}")

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    user = _user_prompt(persona, version, scene=scene, product=product,
                        platform=platform, duration_s=duration_s, tone=tone)
    try:
        response = client.messages.create(
            model=settings.script_model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM,
            thinking={"type": "adaptive"},
            output_config={
                "effort": settings.script_effort,
                "format": {"type": "json_schema", "schema": SCHEMA},
            },
            messages=[{"role": "user", "content": user}],
        )
    except anthropic.APIStatusError as exc:
        ledger.settle(gen_id, status="failed", actual_cost_usd=0.0,
                      error=f"{type(exc).__name__} {exc.status_code}: {exc.message}")
        raise ScriptError(f"Claude API error {exc.status_code}: {exc.message}")
    except anthropic.APIConnectionError as exc:
        ledger.settle(gen_id, status="failed", actual_cost_usd=0.0,
                      error=f"connection error: {exc}")
        raise ScriptError(f"could not reach the Claude API: {exc}")

    # Unlike a render, this cost is known exactly — settle at measured usage rather
    # than letting the flat reservation estimate stand.
    usage = response.usage
    cost = rate.token_cost(usage.input_tokens, usage.output_tokens)

    if response.stop_reason == "refusal":
        detail = getattr(response, "stop_details", None)
        ledger.settle(gen_id, status="failed", actual_cost_usd=cost,
                      error=f"refusal: {getattr(detail, 'category', 'unknown')}")
        raise ScriptError(
            "Claude declined to write this one"
            + (f" ({detail.category})" if detail and detail.category else "")
            + ". Rephrase the scene, or write the line yourself.")

    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        result = json.loads(text)
    except ValueError:
        ledger.settle(gen_id, status="failed", actual_cost_usd=cost,
                      error="response was not valid JSON")
        raise ScriptError("Claude returned something that was not a script")

    ledger.settle(gen_id, status="succeeded", actual_cost_usd=cost)
    result.update(generation_id=gen_id, cost_usd=cost, model=response.model,
                  input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                  mock=False)
    return result


def to_brief(script: dict) -> str:
    """Fold the two artifacts into the brief format the job pipeline already reads.

    The spoken line goes in quotes because `prompts.structure()` extracts quoted text
    verbatim — which is exactly the guarantee wanted here: the line an operator
    approved is the line the model is told to say.
    """
    visual = (script.get("visual_direction") or "").strip().rstrip(".")
    dialogue = (script.get("dialogue") or "").strip().strip('"')
    if not dialogue:
        return visual
    return f'{visual} and says "{dialogue}"' if visual else f'She says "{dialogue}"'
