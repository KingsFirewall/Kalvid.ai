"""Cost ledger + Budget Guard.

Two-phase accounting, which is what makes the cap actually hold:

  reserve()  -- writes a generations row with status='pending' and the ESTIMATED cost
                *before* the provider call fires. The guard counts pending rows, so two
                concurrent approvals cannot both slip under the same cap.
  settle()   -- reconciles that row with what the provider really charged, success or
                failure. Providers bill on failure too, so a failed row keeps its cost.

A reservation is never silently dropped: if the call never fires, release() marks it
'cancelled', which is the only status excluded from spend.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from . import db
from .config import settings
from .rates import Rate, rate_table

# Statuses that represent money that is committed or already spent.
COMMITTED = ("pending", "running", "succeeded", "failed")
_COMMITTED_SQL = "('pending','running','succeeded','failed')"

# Effective cost of a row: what we were actually charged once known, else the estimate.
_COST_EXPR = "COALESCE(g.actual_cost_usd, g.estimated_cost_usd)"


class BudgetExceeded(Exception):
    def __init__(self, scope: str, running: float, estimate: float, cap: float):
        self.scope, self.running, self.estimate, self.cap = scope, running, estimate, cap
        super().__init__(
            f"{scope} budget cap would be exceeded: "
            f"${running:.2f} spent + ${estimate:.2f} estimated > ${cap:.2f} cap"
        )


@dataclass
class BudgetStatus:
    scope: str
    cap: float
    spent: float
    pending: float
    estimate: float

    @property
    def remaining(self) -> float:
        return round(self.cap - self.spent, 4)

    @property
    def would_exceed(self) -> bool:
        return self.cap > 0 and (self.spent + self.estimate) > self.cap + 1e-9

    @property
    def pct_used(self) -> float:
        return round(100 * self.spent / self.cap, 1) if self.cap > 0 else 0.0


def month_bounds(today: date | None = None) -> tuple[str, str]:
    """Calendar-month window, [start, end). Server-local dates — documented in the
    README so 'monthly cap' has one unambiguous meaning."""
    today = today or date.today()
    start = today.replace(day=1)
    end = date(start.year + (start.month == 12), (start.month % 12) + 1, 1)
    return start.isoformat(), end.isoformat()


def client_spend(client_id: int, today: date | None = None) -> tuple[float, float]:
    """(total_committed, of_which_still_pending) for the client this calendar month.

    Reads generations.client_id directly. This used to join out through
    jobs -> personas, which meant any generation without a job — a standalone still
    for an influencer or an ad — contributed nothing to the total and so could be
    spent past the cap without the guard ever seeing it.
    """
    start, end = month_bounds(today)
    row = db.query_one(
        f"""
        SELECT COALESCE(SUM({_COST_EXPR}), 0) AS total,
               COALESCE(SUM(CASE WHEN g.status IN ('pending','running')
                                 THEN {_COST_EXPR} ELSE 0 END), 0) AS pending
          FROM generations g
         WHERE g.client_id = ?
           AND g.status IN {_COMMITTED_SQL}
           AND g.created_at >= ? AND g.created_at < ?
        """,
        (client_id, start, end),
    )
    return round(row["total"], 6), round(row["pending"], 6)


def job_spend(job_id: int) -> float:
    row = db.query_one(
        f"""SELECT COALESCE(SUM({_COST_EXPR}), 0) AS total
              FROM generations g
             WHERE g.job_id = ? AND g.status IN {_COMMITTED_SQL}""",
        (job_id,),
    )
    return round(row["total"], 6)


@dataclass
class Scope:
    """Who this spend is charged to, and under which caps.

    A video generation has a job (and therefore a per-job cap). A standalone still
    has only a client. Both must reach the same guard.
    """
    client_id: int
    client_cap: float
    job_id: int | None = None
    persona_id: int | None = None
    job_cap: float = 0.0


def scope_for_job(job_id: int) -> Scope:
    ctx = _job_context(job_id)
    return Scope(
        client_id=ctx["client_id"], client_cap=ctx["monthly_budget_cap"],
        job_id=job_id, persona_id=ctx["persona_id"],
        job_cap=ctx["job_budget_cap"] or ctx["default_job_cap"],
    )


def scope_for_client(client_id: int, persona_id: int | None = None) -> Scope:
    """Charge a still to a client directly. No job, so no per-job cap applies —
    the client's monthly cap is the only thing standing between you and the bill."""
    row = db.query_one("SELECT * FROM clients WHERE id=?", (client_id,))
    if row is None:
        raise KeyError(f"client {client_id} not found")
    if persona_id is not None:
        owner = db.query_one("SELECT client_id FROM personas WHERE id=?", (persona_id,))
        if owner is None:
            raise KeyError(f"persona {persona_id} not found")
        if owner["client_id"] != client_id:
            # Otherwise a still could be billed to one client while being saved as
            # another client's asset.
            raise ValueError(
                f"persona {persona_id} belongs to client {owner['client_id']}, "
                f"not {client_id}")
    return Scope(client_id=client_id, client_cap=row["monthly_budget_cap"],
                 persona_id=persona_id)


def _job_context(job_id: int):
    row = db.query_one(
        """SELECT j.id AS job_id, j.job_budget_cap, j.target_duration,
                  j.persona_id, p.client_id, c.monthly_budget_cap, c.default_job_cap,
                  c.name AS client_name
             FROM jobs j
             JOIN personas p ON p.id = j.persona_id
             JOIN clients  c ON c.id = p.client_id
            WHERE j.id = ?""",
        (job_id,),
    )
    if row is None:
        raise KeyError(f"job {job_id} not found")
    return row


def check_scope(scope: Scope, estimate: float) -> list[BudgetStatus]:
    """Every cap that applies to this spend, client-level first."""
    spent, pending = client_spend(scope.client_id)
    statuses = [BudgetStatus("client", scope.client_cap, spent, pending, estimate)]
    if scope.job_id is not None and scope.job_cap:
        statuses.append(
            BudgetStatus("job", scope.job_cap, job_spend(scope.job_id), 0.0, estimate))
    return statuses


def check(job_id: int, estimate: float) -> list[BudgetStatus]:
    """Both caps that apply to a job's spend, client-level first."""
    return check_scope(scope_for_job(job_id), estimate)


def reserve(
    *,
    stage: str,
    rate: Rate,
    duration_s: float,
    job_id: int | None = None,
    scope: Scope | None = None,
    variant: str | None = None,
    calls: int = 1,
    payload: str = "",
    billable: bool = True,
    override_by: str | None = None,
) -> int:
    """Claim budget and open a pending ledger row. Returns generation_id.

    Pass either `job_id` (a draft or final render) or `scope` (a standalone still).
    Both take the same lock and hit the same cap.

    Raises BudgetExceeded unless override_by is set (an explicit, logged human decision).
    Raises UnverifiedRate if a billable call would ride on a placeholder price.
    """
    if billable:
        rate_table.require_billable(rate)
    if scope is None:
        if job_id is None:
            raise ValueError("reserve() needs either job_id or scope")
        scope = scope_for_job(job_id)

    estimate = rate.estimate(duration_s=duration_s, calls=calls, variant=variant)

    # Serialised per client, so the read-then-reserve below cannot interleave with
    # another approval slipping under the same cap. See db.exclusive().
    with db.exclusive(scope.client_id) as conn:
        statuses = check_scope(scope, estimate)
        breached = [s for s in statuses if s.would_exceed]

        if breached and not override_by:
            for s in breached:
                conn.execute(db.translate(
                    """INSERT INTO budget_events
                       (client_id, job_id, amount_usd, running_total, cap_at_time,
                        scope, blocked, note)
                       VALUES (?,?,?,?,?,?,1,?)"""),
                    (scope.client_id, scope.job_id, estimate, s.spent, s.cap, s.scope,
                     f"blocked {stage} via {rate.key}"),
                )
            conn.commit()
            s = breached[0]
            raise BudgetExceeded(s.scope, s.spent, s.estimate, s.cap)

        sql = ("""INSERT INTO generations
                  (job_id, persona_id, client_id, stage, provider, model,
                   request_payload, status, estimated_cost_usd)
                  VALUES (?,?,?,?,?,?,?, 'pending', ?)""")
        args = (scope.job_id, scope.persona_id, scope.client_id, stage,
                rate.provider, rate.model, payload, estimate)
        if db.BACKEND == "postgres":
            gen_id = conn.execute(db.translate(sql) + " RETURNING id", args).fetchone()["id"]
        else:
            gen_id = conn.execute(sql, args).lastrowid

        for s in statuses:
            conn.execute(db.translate(
                """INSERT INTO budget_events
                   (client_id, generation_id, job_id, amount_usd, running_total,
                    cap_at_time, scope, blocked, overridden_by, note)
                   VALUES (?,?,?,?,?,?,?,0,?,?)"""),
                (scope.client_id, gen_id, scope.job_id, estimate, s.spent, s.cap,
                 s.scope, override_by,
                 f"{'OVERRIDE ' if override_by and s.would_exceed else ''}reserved "
                 f"{stage} via {rate.key}"),
            )
        conn.commit()
        return gen_id


def mark_running(gen_id: int, provider_job_id: str | None) -> None:
    db.execute(
        "UPDATE generations SET status='running', provider_job_id=? WHERE id=?",
        (provider_job_id, gen_id),
    )


def settle(
    gen_id: int,
    *,
    status: str,
    actual_cost_usd: float | None = None,
    output_url: str | None = None,
    error: str | None = None,
) -> dict:
    """Close a reservation with what really happened. Returns a drift report."""
    row = db.query_one("SELECT * FROM generations WHERE id=?", (gen_id,))
    if row is None:
        raise KeyError(f"generation {gen_id} not found")

    # A provider that reports no cost is not the same as one that charged nothing.
    # Fall back to the estimate so the ledger never under-reports real spend.
    cost = row["estimated_cost_usd"] if actual_cost_usd is None else actual_cost_usd

    db.execute(
        """UPDATE generations
              SET status=?, actual_cost_usd=?, output_url=?, error=?,
                  settled_at=CURRENT_TIMESTAMP
            WHERE id=?""",
        (status, cost, output_url, error, gen_id),
    )

    est = row["estimated_cost_usd"]
    drift_pct = round(100 * (cost - est) / est, 1) if est > 0 else (0.0 if cost == 0 else 100.0)
    report = {
        "generation_id": gen_id,
        "estimated": est,
        "actual": cost,
        "drift_pct": drift_pct,
        "cost_reported_by_provider": actual_cost_usd is not None,
        # A rate that consistently mis-estimates is a rate that needs re-verifying.
        "drift_warning": abs(drift_pct) > settings.cost_drift_warn_pct,
    }
    if report["drift_warning"]:
        # Read the client off the generation row: a still has no job to join through.
        db.execute(
            """INSERT INTO budget_events
               (client_id, generation_id, job_id, amount_usd, running_total,
                cap_at_time, scope, blocked, note)
               VALUES (?,?,?,?,0,0,'client',0,?)""",
            (row["client_id"], gen_id, row["job_id"], cost,
             f"COST DRIFT {drift_pct:+.1f}%: estimated ${est:.4f}, charged ${cost:.4f} "
             f"— re-verify this rate in rates.json"),
        )
    return report


def release(gen_id: int, reason: str = "call never fired") -> None:
    """Free a reservation that never became a real call."""
    db.execute(
        """UPDATE generations SET status='cancelled', actual_cost_usd=0,
                  error=?, settled_at=CURRENT_TIMESTAMP
            WHERE id=? AND status='pending'""",
        (reason, gen_id),
    )
