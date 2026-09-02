"""Preflight must be free, offline-safe, and honest about what blocks a live run."""
from app import doctor


def test_placeholders_are_detected_without_any_network_call(monkeypatch):
    """If a probe ever hits the network on placeholder creds, this test fails."""
    def explode(*a, **kw):
        raise AssertionError("doctor must not make a network call for placeholder creds")

    monkeypatch.setattr(doctor.httpx, "get", explode)
    monkeypatch.setattr(doctor.httpx, "post", explode)

    checks = doctor.run_all()
    names = {c.name: c for c in checks}
    assert names["fal.ai credentials"].ok is False
    assert "placeholder" in names["fal.ai credentials"].detail


def test_one_unverified_price_falls_over_to_the_next_candidate():
    """A stage lists several models in preference order; one bad price is survivable.

    This is the point of having more than one model behind a stage: losing a price
    (a promo lapsing, say) should demote that model, not stop production.
    """
    import dataclasses

    from app.rates import rate_table

    keys = [c.key for c in rate_table.candidates("final")]
    assert len(keys) > 1, "this property needs a stage with a fallback"
    original = rate_table._rates[keys[0]]
    try:
        rate_table._rates[keys[0]] = dataclasses.replace(original, last_verified=None)
        final = [c for c in doctor.check_rates() if c.name == "final price verified"][0]
        assert final.ok, "an unverified first choice must fail over, not block"
        assert keys[0] not in final.detail, "it must not still be naming the bad rate"
    finally:
        rate_table._rates[keys[0]] = original


def test_unverified_prices_are_reported_as_blocking():
    """With NO verified candidate for a stage, that stage must block."""
    import dataclasses

    from app.rates import rate_table

    keys = [c.key for c in rate_table.candidates("final")]
    originals = {k: rate_table._rates[k] for k in keys}
    try:
        for k in keys:
            rate_table._rates[k] = dataclasses.replace(originals[k], last_verified=None)
        final = [c for c in doctor.check_rates() if c.name == "final price verified"][0]
        assert not final.ok and final.blocking, \
            "an unverified price must block, not merely warn — the cap depends on it"
    finally:
        rate_table._rates.update(originals)


def test_an_expiring_promo_price_is_surfaced():
    """A price about to lapse must be visible before it silently doubles."""
    checks = doctor.check_rates()
    expiry = [c for c in checks if c.name.endswith("price expiry")]
    if expiry:
        assert not expiry[0].ok and not expiry[0].blocking
        assert "lapses in" in expiry[0].detail


def test_a_verified_rate_clears_the_price_check(monkeypatch):
    from datetime import date

    from app.rates import rate_table

    key = rate_table.candidates("final")[0].key
    original = rate_table._rates[key]
    try:
        rate_table._rates[key] = type(original)(
            **{**original.__dict__, "last_verified": date.today()})
        final = [c for c in doctor.check_rates() if c.name == "final price verified"][0]
        assert final.ok
    finally:
        rate_table._rates[key] = original
