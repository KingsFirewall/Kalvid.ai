# AI Ad/UGC Video Generation System — Build Plan

## 1. Requirements

**Functional**
- Manage "Personas": each client's AI UGC model — one locked reference identity, reused across every video so the face/character stays consistent without re-gambling on generation each time.
- Manage "Jobs": a request to produce a video (ad or UGC clip) for a persona — script/brief in, finished video out.
- Enforce a **draft-before-final** workflow: generate a cheap still or low-res/short test before ever spending on a full-resolution render, and require explicit approval before the expensive step fires.
- Route each generation to the right backend (primary vs. cheap-fallback provider) and the right model tier (draft-quality vs. final-quality).
- Track cost per generation, per job, per persona, per client — no black-box credit balance, an actual ledger.
- Enforce hard budget caps per client/job so nothing can silently blow past what's been quoted.

**Non-functional**
- Cost minimization is the primary design driver — every architectural choice below is justified against "does this stop us wasting spend."
- Reliability: solid enough that a client-committed turnaround doesn't get jeopardized by a provider outage, but no need for enterprise-grade infra at this scale.
- Small scale: a handful of clients (2-5), a few videos each per month. Don't build for scale you don't have yet.
- Internal only for v1: your team operates it directly; no client logins, no public-facing app.

**Constraints**
- Built by Claude Code, iteratively.
- No existing tech stack to integrate with — greenfield.
- Should be cheap and simple to run (a laptop or a $5-10/mo VPS should be enough at this scale — no Kubernetes, no managed message queues).

## 2. High-Level Design

### Components

```
┌─────────────────────────────────────────────────────────────┐
│  Internal Dashboard (local web app)                          │
│  - Create/edit personas                                      │
│  - Create jobs, review drafts, approve final renders         │
│  - View cost ledger, budget status per client                │
└───────────────────────────┬───────────────────────────────────┘
                             │
┌───────────────────────────▼───────────────────────────────────┐
│  Core Service (Python/Node)                                   │
│  ┌────────────┐ ┌───────────────┐ ┌────────────────────────┐ │
│  │ Persona    │ │ Job Orchestr- │ │ Cost Ledger &           │ │
│  │ Manager    │ │ ator (draft-  │ │ Budget Guard            │ │
│  │            │ │ then-final)   │ │                         │ │
│  └────────────┘ └───────┬───────┘ └────────────────────────┘ │
│                          │                                     │
│                  ┌───────▼────────┐                            │
│                  │ Provider Router │                            │
│                  └───────┬────────┘                            │
└──────────────────────────┼──────────────────────────────────────┘
                            │
              ┌─────────────┼─────────────┐
              ▼             ▼             ▼
        ┌──────────┐  ┌──────────┐  ┌──────────┐
        │ fal.ai   │  │ Runware  │  │ (future  │
        │ (primary,│  │ (cheap   │  │ providers│
        │ SLA)     │  │ draft/   │  │ as        │
        │          │  │ fallback)│  │ needed)   │
        └──────────┘  └──────────┘  └──────────┘
```

### Data flow (one job, start to finish)

1. Team member picks a **Persona** and writes a brief for a new **Job** (what the ad/UGC clip needs to say/show).
2. Core Service runs the brief through a **prompt structurer** (CinePrompt-style: turns loose brief into a precise, technical prompt — lighting, camera, framing — so the model has less room to misfire and needs fewer retries).
3. **Draft stage**: Provider Router sends a cheap still-image (or 2-3s low-res clip) request to the persona's locked reference + the structured prompt. Cost is logged immediately.
4. Draft appears in the dashboard. A human approves, requests a re-draft (still cheap), or kills the job.
5. **Final stage**: only on explicit approval, Provider Router sends the full-resolution/full-duration request to the primary provider (fal.ai). Cost is logged.
6. Finished video lands in a per-client output folder; job marked complete; cost ledger updated with the total real spend for that job.
7. Budget Guard checks running total against the client's monthly cap before every paid call (draft or final) — if a call would exceed the cap, it blocks and asks for confirmation instead of firing silently.

### API contracts (internal, Core Service ⇄ Dashboard)

- `POST /personas` — create persona (name, client, reference images, voice profile, notes)
- `GET /personas/:id`
- `POST /jobs` — create job (persona_id, brief, target platform e.g. TikTok/IG, desired duration)
- `POST /jobs/:id/draft` — trigger draft-stage generation
- `POST /jobs/:id/approve` — trigger final-stage generation (only callable after a draft exists)
- `POST /jobs/:id/reject` — kill job, no further spend
- `GET /jobs/:id/cost` — cost breakdown for this job
- `GET /clients/:id/ledger` — running spend vs. budget cap

### Storage

At this scale, **SQLite** is the right call — zero ops overhead, a single file, trivially backed up. Move to Postgres only if you outgrow single-file concurrency (unlikely at 2-5 clients). Generated video/image files go to local disk or a cheap object store (Cloudflare R2 or S3) — R2 is worth it early since it has no egress fees, which matters once you're sending finished videos to clients repeatedly.

## 3. Deep Dive

### Data model (core tables)

```
clients        (id, name, monthly_budget_cap, contact_info)
personas       (id, client_id, name, reference_image_url, identity_lock_id,
                voice_profile, notes, created_at)
jobs           (id, persona_id, brief, structured_prompt, platform,
                target_duration, status[draft_pending|draft_ready|
                approved|rendering|complete|rejected], created_at)
generations    (id, job_id, stage[draft|final], provider, model,
                request_payload, cost_usd, status, output_url, created_at)
budget_events  (id, client_id, generation_id, amount_usd, running_total,
                cap_at_time, blocked[bool], created_at)
```

`generations` is the ledger's source of truth — every single paid API call, success or failure, gets a row with its real cost. This is what makes "no more surprise $30" enforceable: nothing spends money without writing a row here first, and the Budget Guard reads this table before allowing the next call.

### Provider Router logic

- **Draft stage** → always the cheapest available option that supports the model needed (Runware first; fall back to fal.ai's cheaper models if Runware is down or doesn't support the specific model).
- **Final stage** → fal.ai by default (SLA + predictable pricing), unless a specific job explicitly needs a model only available elsewhere.
- Router is a thin abstraction (one interface, `generate(prompt, persona, stage, model)` ) — swapping or adding a provider later means writing one adapter, not touching the rest of the system.

### Error handling & retries

- A failed generation still gets logged in `generations` with `status=failed` and whatever cost the provider charged (many charge even on failure — the ledger must reflect reality, not assume $0).
- **No automatic blind retries** — a failed or bad-looking draft goes back to a human for a prompt tweak, not an automatic re-fire, since repeated auto-retries are exactly the failure mode that drained the InVideo credits.
- Final-stage failures alert immediately (this is real client-facing money) rather than failing silently into a queue.

### Budget Guard

- Each client has a `monthly_budget_cap`.
- Before every paid call: `running_total + estimated_cost > cap` → block and require explicit override from a team member, logged in `budget_events`.
- Estimated cost is looked up from a small static table of known provider/model rates (updated manually when prices change — these move often, per the research, so don't hardcode assumptions that go stale silently).

### Prompt structuring (CinePrompt-style layer)

- Free to build in-house: a template that expands a loose brief into explicit technical fields (shot type, lighting, camera movement, lens, duration, persona reference) before it ever reaches a provider.
- This is the single highest-leverage, lowest-cost piece of the whole system — it's the difference between "one clean draft" and "five muddy attempts," and it costs nothing but prompt-engineering effort.

## 4. Scale & Reliability (what to revisit as you grow)

At 2-5 clients this doesn't need: a job queue (Redis/Celery), horizontal scaling, multi-region anything, or client auth. Revisit these specifically once:
- **You cross ~15-20 active clients or start getting concurrent job bursts** → add a real job queue so drafts/finals process asynchronously instead of blocking the dashboard.
- **A client asks to self-serve** → that's when a client-facing portal with auth becomes worth building — don't build it speculatively now.
- **SQLite write contention shows up** (multiple team members triggering jobs simultaneously and seeing lock errors) → migrate to Postgres. Straightforward migration, not worth doing preemptively.
- **You want redundancy** → add a second "final stage" provider as automatic failover if fal.ai has downtime during a client deadline.

## 5. Trade-offs made here

| Decision | Chose | Over | Why |
|---|---|---|---|
| Storage | SQLite | Postgres | Zero ops for 2-5 clients; migrate later if contention appears |
| Draft/final split | Always draft first | Direct-to-final | Costs a small extra call but eliminates the repeated-blind-regeneration failure mode that caused the $30 loss |
| Provider strategy | Multi-provider router | Single vendor lock-in | Keeps you able to price-shop and gives failover; small added complexity is worth it for a business, not a hobby project |
| Retries | Manual, human-gated | Automatic retry-on-fail | Auto-retry is how credits vanish silently; a human glance at a bad draft costs seconds, not dollars |
| UI | Internal dashboard only | Client-facing portal | Matches actual v1 need; building auth/client UX now would be speculative work |

## 6. Build phases

**Phase 1 — Core loop (single client, single persona)**
Persona creation with locked reference image, job creation, draft generation via one provider, manual approval, final generation, cost logged for every call. Prove the draft-then-final gate actually prevents waste before adding anything else.

**Phase 2 — Cost ledger & budget guard**
Full `generations`/`budget_events` tables, per-client caps, blocking behavior, a simple dashboard view of spend vs. cap.

**Phase 3 — Multi-provider routing**
Add Runware as the cheap draft-stage provider, fal.ai confirmed as final-stage, router abstraction so a third provider is a one-file addition later.

**Phase 4 — Prompt structuring layer**
Brief → structured technical prompt template, reducing retry-driven waste further.

**Phase 5 (only if needed) — Scale-out**
Job queue, Postgres, failover providers, client-facing portal — triggered by the specific thresholds in section 4, not on a calendar.

---

**Next step:** if this shape looks right, I can start on Phase 1 now — scaffolding the actual project (persona + job models, the fal.ai/Runware adapters, and the draft-then-final flow) as real code you can run.
