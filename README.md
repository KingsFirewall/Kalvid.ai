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

## The video model: `minimax/h3-max/image-to-video`

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
