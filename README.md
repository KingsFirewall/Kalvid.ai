# Kalvid AI — AI Ad/UGC Video Generation System

Internal tool for producing AI UGC ad clips for a handful of clients, built so that
**nothing expensive fires without a human looking at something cheap first.**

Phases 1–4 of the build plan are implemented and tested. Phase 5 (queue, Postgres,
failover, client portal) is deliberately not built — see *When to revisit* below.

---

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # fill in credentials when you're ready to go live
.venv/bin/python run.py       # dashboard at http://127.0.0.1:8000
```

It starts in **dry run**: every generation goes to a local mock provider, nothing is
billable, and the whole draft → approve → final loop is exercisable for $0.

```bash
.venv/bin/python -m pytest tests/ -q     # 23 tests
```

### CLI (same operations, no browser)

```bash
python cli.py status
python cli.py client add "Acme Skincare" --cap 50 --job-cap 10
python cli.py persona add 1 "Rania" --ref https://.../locked-face.png --notes "24yo, curly dark hair"
python cli.py job add 1 'She unboxes the serum and says "my skin has never looked this good"'
python cli.py job preview 1     # what it would cost, before spending
python cli.py job draft 1 --wait
python cli.py job approve 1 --wait
python cli.py ledger 1
```

---

## Going live — do these in order

Dry run is the default because going live has prerequisites that are easy to skip.

1. **Set API keys** in `.env` (`FAL_KEY` is required; `RUNWARE_API_KEY` only if you
   want Runware as a draft fallback). Done — both fal and Supabase are configured.
2. **Prices.** `fal:minimax/h3-max/image-to-video` is verified as of 2026-08-30.
   **It lapses on 2026-09-01** — re-check and update `last_verified` then, or live
   calls will be refused. The Runware entries are still placeholders; they are unused
   unless you route drafts to them. An unverified rate cannot back a billable call:
   a cap computed from a made-up price is not a cap.
3. **Create the storage bucket** (already done): `python cli.py setup-storage`.
4. **Set caps.** Every client needs a `monthly_budget_cap`; a `default_job_cap` is
   strongly recommended.
5. **Sanity-check the adapter against one real render.** The payload is built from the
   documented schema and filtered to declared fields, but no live generation has been
   run yet — the first one is the real test.
6. **Run the preflight** — it verifies credentials and prices without spending
   anything (no generation is ever submitted):

   ```bash
   python cli.py doctor
   ```

   It exits non-zero while any blocking issue remains.
7. **Set `KALVID_DRY_RUN=false`**, then run one job with a deliberately tiny cap and
   confirm the ledger's actual cost matches what the provider's dashboard reports.

---

## How the cost controls actually work

**Two-phase accounting.** Every paid call writes a `generations` row with an estimated
cost *before* the request fires, then reconciles it with the real charge afterwards.
The guard counts pending rows, so two concurrent approvals cannot both slip under the
same cap. A call that never fires is released, not silently left holding budget.

**The draft is a short low-res clip, not a still.** A still proves identity, framing
and lighting; it says nothing about motion — and motion artifacts are what actually
ruin a take. The default draft is a 3s quarter-res video, which is the cheap check
that predicts the final. (`KALVID_DRAFT_KIND=image` falls back to a still.)

**The draft must be genuinely cheaper — verify, don't assume.** With this model the
saving comes from resolution (480P vs 768P) plus a shorter duration. With a
fixed-duration model a "short" draft is billed at full length and the gate stops
paying for itself. The dashboard and `job preview` report the draft as a percentage
of the final and warn above 50%.

**A failed call still costs.** Providers bill on failure. A failed generation records
a real cost, never an assumed $0.

**No automatic retries, ever.** A failure goes back to a human for a prompt tweak.
Blind auto-retry is precisely how credits vanish.

**Cost drift is flagged.** When a settled cost differs from the estimate by more than
`KALVID_COST_DRIFT_PCT` (default 25%), the ledger writes a warning naming the rate to
re-verify. A price table that drifts silently makes every cap wrong.

**Overrides are explicit and attributed.** Exceeding a cap requires an `override_by`
value, recorded in `budget_events`. There is no silent way past a cap.

---

## The model catalogue — one API key, eleven models

Everything routes through **fal**, so `FAL_KEY` alone unlocks all of these. Switching
is a reorder in `rates.json` under `routing` — the router takes the first candidate
that is configured, available and **verified**, so a model with a lapsed price is
demoted automatically rather than taking production down with it.

All prices verified **2026-08-31** from the fal model pages; schemas from each
endpoint's OpenAPI document.

### Video — all image-to-video, so the persona's face carries

| Model | Price | 8s ad | Duration | Notes |
|---|---|---|---|---|
| `minimax/h3-max/image-to-video` | $0.025–0.04/s | **$0.32** | any int | Cheapest. **Promo lapses 2026-09-01** |
| `fal-ai/wan-25-preview/image-to-video` | $0.05/$0.10/$0.15 per s | $1.50 | 5 or 10 only | **Takes `audio_url`** — does lipsync too |
| `fal-ai/kling-video/v2.5-turbo/pro/…` | $0.07/s | $0.70 | 5 or 10 only | Volume workhorse; no resolution tier |
| `fal-ai/bytedance/seedance/v1/pro/…` | ~$0.124/s @1080p | $0.99 | 2–12 | Premium; see the pricing caveat below |
| `fal-ai/veo3.1/image-to-video` | $0.20/s | $1.60 | 4/6/8 | Highest quality. `generate_audio` forced off |
| `fal-ai/bytedance/omnihuman` | $0.14/s | $1.12 | audio ≤30s | Audio-driven avatar; takes no prompt |

### Image

| Model | Price | Kind |
|---|---|---|
| `fal-ai/flux/schnell` | $0.003/MP | text-to-image |
| `fal-ai/qwen-image` | $0.02/MP | text-to-image, better at legible text |
| `fal-ai/bytedance/seedream/v4/text-to-image` | $0.03/image | text-to-image, flat per-image |
| `fal-ai/flux/dev/image-to-image` | $0.03/MP | identity-preserving |
| `fal-ai/nano-banana/edit` | $0.039/image | identity-preserving, conditions on a **list** |

### Three things this catalogue forced into the adapter

**`duration` is not always an integer.** minimax takes `8`; wan, kling and seedance
take the *string* `"8"`; veo takes `"8s"`. fal validates strictly and rejects the wrong
shape rather than coercing it, so each rate declares `duration_format` and the adapter
serialises accordingly. This was a live bug — any of the four would have returned HTTP
400 on a billed attempt.

**Some models take a list of references.** `nano-banana/edit` wants `image_urls`, not
`image_url`. Rates declare `reference_field`. That list form is also the natural way to
condition on a whole reference pack rather than one plate.

**Veo generates its own audio by default.** Left on, it invents a different voice on
every clip — which kills the persistent-voice pipeline — *and* doubles the price to
$0.40/s. `generate_audio` is pinned `false` in its params, with a test asserting it.

### Caveats worth knowing before you route to them

- **Fixed durations change the draft economics.** wan and kling only emit 5s or 10s, so
  a 3s draft is **billed as 5s**. `job preview` already reports the draft as a
  percentage of the final and warns above 50% — on kling it is exactly 50%, because
  there is no cheaper resolution tier to draft at.
- **Seedance's real pricing is token-based** — `(h × w × fps × duration) / 1024` at
  $2.5 per million. The table holds $0.124/s, derived from fal's own quoted "$0.62 per
  1080p 5 second video", so **the 480p draft estimate is wrong**. Cost drift will flag
  it. Verify before routing drafts there.
- **OmniHuman takes no prompt.** Framing and action come entirely from the reference
  plate, so it is a lipsync stage, not a general renderer.

## The default video model: `minimax/h3-max/image-to-video`

Configured in `rates.json`. Schema and pricing read from the fal model page.

### Pricing — verified 2026-08-30, and it lapses on 2026-09-01

| Resolution | Now (promo, 50% off) | From 2026-09-01 |
|---|---|---|
| 480P | $0.025 /s | $0.05 /s |
| 768P | $0.040 /s | $0.080 /s |

So an 8s vertical ad currently costs **$0.32** to render, with a 3s 480P draft at
**$0.075** — the draft is 23% of the final.

The promo end date is in `rates.json` as `price_expires`. **On 2026-09-01 the rate
automatically reads as unverified and the router refuses live calls** until someone
re-checks the price and updates the entry. `python cli.py doctor` warns for the seven
days before that. This is deliberate: a promotional rate that silently doubles is
exactly how a budget cap starts lying.

### Input schema

The model accepts `prompt`, `duration` (integer seconds, default 5), `resolution`
(`480P` | `768P`), `seed`, `image_url`, `end_image_url`, `prompt_expansion_mode`,
`enable_safety_checker`, `sync_mode` — and **nothing else**.

Two consequences the adapter enforces:

- **No `negative_prompt`.** The prompt structurer still produces one (other models
  take it), but it is filtered out for this model rather than sent. It is never folded
  into the positive prompt as a workaround — models render the terms you meant to
  exclude.
- **No `width`/`height`.** Resolution is a two-value enum, and the **output aspect
  ratio follows the input image**. Your 9:16 framing therefore comes from the
  persona's reference still, not from a parameter — a 16:9 reference silently yields
  a 16:9 ad.

`rates.json` declares the accepted fields under `supports`; the adapter sends nothing
outside that list, so an unsupported parameter can never be smuggled into a request.

### Resolution is the draft/final lever

Draft renders at 480P, final at 768P — set per stage in `rates.json` under `params`.
Because 480P is genuinely cheap, **drafts run on the same model as finals**, which
makes a draft a far more faithful preview than a different cheap model would be.

### `image_url` is optional to the model, mandatory to us

Omitting it routes the request to text-to-video, which returns a perfectly good video
of **a stranger**. A persona job without a reference still is refused before the call
fires (`identity_via_image` in `rates.json`).

### Set durations deliberately

`duration` is a plain integer billed by the second, so cost scales linearly — a 10s ad
costs 25% more than an 8s one. There are no fixed tiers on this model. (The tier
machinery still exists for models that do have them.)

---

## Architecture

```
Dashboard / CLI
      │
      ├── Job Orchestrator ──── draft → [human] → final     app/jobs.py
      ├── Still Generator ───── image → saved asset          app/images.py
      ├── Cost Ledger + Budget Guard (reserve/settle)       app/ledger.py
      ├── Prompt Structurer (brief → technical prompt)      app/prompts.py
      ├── Identity Binding (how a face stays consistent)    app/identity.py
      └── Provider Router ──── mock | runware | fal         app/router.py
                                                            app/providers/
```

| File | Role |
|---|---|
| `app/schema.sql` / `schema_pg.sql` | Tables: clients, personas, jobs, generations, budget_events, assets |
| `app/ledger.py` | Two-phase reserve/settle, budget guard, drift detection |
| `app/jobs.py` | State machine + background worker; the draft-then-final gate |
| `app/images.py` | Still generation and the persona asset library |
| `app/schema_upgrade.py` | Idempotent in-place migrations for an existing ledger |
| `app/router.py` | Cheapest workable provider per stage; refuses unverified prices |
| `app/identity.py` | The three identity-lock strategies and their validation |
| `app/prompts.py` | Deterministic brief → structured technical prompt |
| `app/storage.py` | Archives renders to Supabase Storage (local disk fallback) |
| `app/db.py` | Dual-backend access; the only module that knows the dialect |
| `app/migrate.py` | SQLite → Postgres copy, preserving ids |
| `rates.json` | Price table — the guard is only as honest as this file |

### Job state machine

```
created ─┐
         ├─> drafting ─> draft_ready ─> approved ─> rendering ─> complete
draft_ready ┘                 │                        │
                              └─────> rejected <───────┘
(drafting|rendering) ─> failed ─> drafting     (retry is human, never automatic)
```

A final render is reachable **only** from `draft_ready`. Every transition into a
spending state is an atomic conditional `UPDATE`, so a double-clicked Approve loses
the race and fires nothing.

### Persona identity

The one thing the plan named but did not specify. Three strategies, chosen per persona:

- `reference_image` — a locked still seeds every generation. Cheapest, works
  everywhere, drifts across clips.
- `lora` — a character model trained once on the persona. Strongest consistency,
  costs an upfront training run.
- `character_id` — a provider-side persistent character handle. Strong, but ties the
  persona to that provider.

A persona missing the field its strategy needs is **rejected at creation**, rather
than discovered after paying for a render of the wrong face.

---

## Scripts — Claude writes the line, not the camera

`app/scripts.py` turns a scene description into two artifacts, kept separate as the
platform SOP requires:

- **`dialogue`** — the spoken line, written by Claude in the persona's voice
- **`visual_direction`** — what the camera sees; one or two sentences

**The visual prompt stays deterministic.** `app/prompts.py` still owns shot, lens,
lighting, framing and grade, and the model is explicitly told its lens and lighting
suggestions will be discarded. This is the point: a re-draft is only a real comparison
if the same brief produces the same technical prompt. Nobody needs creative variation
in "28mm equivalent, eye-level".

**The line reaches the model verbatim.** `to_brief()` puts the dialogue in quotes, and
`structure()` extracts quoted text unchanged — so the line an operator approved is the
line the video model is told to say, character for character.
`tests/test_scripts.py` pins both properties.

```bash
POST /api/scripts        # {persona_id, scene, platform, duration_s, product, tone}
GET  /api/scripts/preview
```

**A script is a metered call.** It reserves and settles through the same ledger and the
same per-client cap as a render, with `stage='script'`. At roughly a cent against
$0.32–$1.50 for the video it feeds, the reason to meter it is the invariant — every
paid call gets a row before it fires — not the money. Unlike a render, the cost is
known exactly: `settle()` uses measured token usage rather than the flat reservation
estimate.

**Without `ANTHROPIC_API_KEY` the app is unaffected.** The Creator shows the button
disabled with a note, and you write the line yourself as before. In dry run a mock
writer returns deterministic placeholder text that is *obviously* fake, so nothing
plausible-looking can be shipped by accident.

Configure with `KALVID_SCRIPT_MODEL` (default `claude-opus-5`) and
`KALVID_SCRIPT_EFFORT` (default `medium` — short-form creative copy does not repay
deep reasoning).

## Images and the asset library

A video needs a face to hold onto. Making one used to mean hosting an image somewhere
and pasting a URL into a form. Now you generate it here, and it stays.

```bash
# create a face for a new influencer, then look at /images and lock the best one
python cli.py status                  # confirm DRY RUN first
```

**Two routes, and the difference is not cosmetic.**

| Route | Model | Price | What it does |
|---|---|---|---|
| `still` | `fal-ai/flux/schnell` | $0.003 | Text-to-image. Invents a **new** face, or a product/ad still. |
| `still_identity` | `fal-ai/flux/dev/image-to-image` | $0.03 | Seeded from the persona's locked reference. Returns the **same** face. |

Picking the wrong one does not produce an error. It produces a polished, plausible
photograph of a different person. So the choice is explicit in the API (`keep_face`)
and named on screen, and a persona with no locked reference cannot select the second
one at all — it is refused before anything is charged.

Both prices are **per megapixel, rounded up**, verified 2026-08-30 from the fal model
pages. The 720×1280 default is 0.92 MP and therefore bills as 1 MP. **Change
`image_size` in `rates.json` and the estimate stops being true** — 1080×1920 is 2.07 MP
and bills as 3. `flux/dev/image-to-image` has no size parameter at all: its output
follows the input image, so a large reference still costs proportionally more. The
cost-drift warning is what catches both.

**Stills spend directly — there is no draft gate.** The gate exists because a video
costs ~$0.32 and a draft predicts it for $0.075. An image is $0.003. Gating it would
mean paying for a preview of a preview. The price is still shown before the button,
and the spend still goes through the same reserve/settle and the same client cap.

**A batch is N reservations, not one.** Ask for 8 images against a cap with room for 2
and you get 2, then a refusal — not 8, and not 0.

### Every paid call now names the client it charges

`generations` gained `client_id` (and nullable `job_id`/`persona_id`). This is a
correctness fix, not bookkeeping: spend used to be found by joining
`generations → jobs → personas`, so **any generation without a job contributed nothing
to the monthly total** and could be repeated past the cap indefinitely. A still has no
job. `tests/test_images.py` pins this.

Existing databases are migrated in place on startup by `app/schema_upgrade.py`, which
is idempotent. SQLite cannot alter a `CHECK` constraint, so the table is rebuilt with
foreign keys disabled for the swap — the `budget_events` audit trail survives.

### Identity versioning — locked, and never overwritten

A persona's reference image is the single input that decides who appears on screen.
It is therefore **immutable once locked**. Editing it does not overwrite anything; it
cuts the next version, supersedes the old one, and leaves every job pinned to the
version it was briefed against.

```bash
GET  /api/personas/{id}/versions      # full history: current, open draft, all versions
POST /api/personas/{id}/versions      # cut v(n+1); the previous is superseded
```

The property that matters: **a job re-drafted after an identity change renders the
person it was briefed for, not whoever the persona has since become.** `jobs.py`
reads `identity_strategy` / `reference_image_url` / `identity_lock_id` from the
job's pinned `identity_versions` row, never from the live `personas` row.
`tests/test_identity_versions.py` pins this by name.

`personas.*` is kept in sync with the current locked version, but only as a cache of
"who she is now". The version rows are the record of what each render actually used.

Promoting an asset to the locked face (`POST /api/assets/{id}/primary`) cuts a version
rather than mutating the column — that call used to overwrite it in place, which
silently rewrote the provenance of everything already delivered under the old face.
An identity that could not generate cannot be locked at all: it is refused where it is
still free to fix.

`assets.identity_version_id` records which identity was current when a still was made.
That is provenance, not a preservation order — an ordinary still stays deletable. Only
a canonical `plate` backing a locked version is protected.

### Saved assets

Every generated still is filed in `assets` against its persona. One per persona can be
promoted to the locked face, which writes `personas.reference_image_url` — the column
generations actually read. A "primary" flag that the renderer never saw would be a
label that lies, so promotion sets both. The locked face cannot be deleted while it is
locked.

After that the loop the tool exists for is: **pick an influencer, write a brief,
generate.** No uploading.

## Storage

### Database — Supabase Postgres

The ledger lives in Supabase Postgres. Set `SUPABASE_DB_URL` in `.env` and the app
uses it; leave it blank and it falls back to SQLite (`data/kalvid.db`). Force either
with `KALVID_DB_BACKEND=postgres|sqlite`.

```bash
python cli.py db status      # which backend, and row counts
python cli.py db init        # apply the schema
python cli.py db migrate     # copy an existing SQLite ledger across, ids preserved
```

**Use a pooler connection string, not the "Direct connection" one.** Direct
connections are IPv6-only on new Supabase projects and time out from most machines.
Copy the *Session pooler* URI from Settings → Database.

Two details that matter and are handled in `app/db.py`:

- **Prepared statements are disabled** (`prepare_threshold=None`). Supabase's
  transaction pooler multiplexes connections and cannot keep server-side prepared
  statements alive between transactions; leaving them on gives intermittent
  "prepared statement does not exist" errors that only show up under load.
- **The budget guard's atomicity is backend-specific.** SQLite uses `BEGIN
  IMMEDIATE`; Postgres uses a transaction-scoped advisory lock keyed on the client
  id, which serialises reservations per client without blocking unrelated work.
  `tests/test_postgres.py` re-runs the two-thread race against Postgres, because this
  is the one guarantee that must hold identically on both.

Application SQL is written once and translated in `app/db.py`: `?` placeholders
become `%s`, and the schema keeps booleans as SMALLINT 0/1 and uses
`CURRENT_TIMESTAMP`, so nothing above that module knows which database is live.

**RLS is enabled on every table with no permissive policy**, so the public anon key
reaches nothing. The app connects as the service role, which bypasses RLS.

**"Monthly" means calendar month in the server's local timezone**, `[1st, 1st)`.

### Finished renders

Renders go to `outputs/<client>/job-<id>/` and to Supabase Storage
(`kalvid-renders`, private). Provider output URLs expire, so every render is pulled
down and re-hosted before the ledger records it; clients receive a signed URL.
Archiving never fails a completed render — if the copy fails it logs a warning and
keeps the provider URL.

```bash
python cli.py setup-storage    # create the bucket, prove an upload round-trip
```

## When to revisit (thresholds, not a calendar)

- **~15–20 active clients, or concurrent job bursts** → a real job queue. The current
  worker is a 4-thread pool; fine for a handful of clients, not for bursts.
- **A client asks to self-serve** → client portal + auth. Not before.
- **A fal outage during a deadline** → a second final-stage provider as failover. The
  router already selects from an ordered candidate list, so this is a config change
  plus an adapter.

## Security note

v1 is internal-only and has **no authentication**. Bind to localhost (the default).
If you put it on a VPS, put it behind a VPN, SSH tunnel, or an authenticating proxy —
do not expose it directly. `SUPABASE_SERVICE_ROLE_KEY` bypasses row-level security and
must stay server-side.
