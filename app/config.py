"""Configuration. Everything overridable by environment variable so the same code
runs on a laptop and a $5 VPS without edits."""
import os
from dataclasses import dataclass, field
from pathlib import Path

from .env import load_env  # noqa: F401  -- populates os.environ from .env on import

ROOT = Path(__file__).resolve().parent.parent


def _env_bool(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    db_path: str = field(default_factory=lambda: os.getenv("KALVID_DB", str(ROOT / "data" / "kalvid.db")))
    output_dir: Path = field(default_factory=lambda: Path(os.getenv("KALVID_OUTPUT_DIR", str(ROOT / "outputs"))))
    rates_path: Path = field(default_factory=lambda: Path(os.getenv("KALVID_RATES", str(ROOT / "rates.json"))))

    fal_api_key: str | None = field(default_factory=lambda: os.getenv("FAL_KEY"))
    runware_api_key: str | None = field(default_factory=lambda: os.getenv("RUNWARE_API_KEY"))

    # --- Supabase -----------------------------------------------------------
    # Storage is wired up for delivering finished renders. The Postgres URL is read
    # here so the SQLite -> Postgres migration is a config change when you need it;
    # see README "Storage" for why SQLite is still the default at this scale.
    supabase_url: str | None = field(default_factory=lambda: os.getenv("SUPABASE_URL"))
    supabase_anon_key: str | None = field(default_factory=lambda: os.getenv("SUPABASE_ANON_KEY"))
    supabase_service_role_key: str | None = field(
        default_factory=lambda: os.getenv("SUPABASE_SERVICE_ROLE_KEY"))
    supabase_db_url: str | None = field(default_factory=lambda: os.getenv("SUPABASE_DB_URL"))
    supabase_bucket: str = field(
        default_factory=lambda: os.getenv("SUPABASE_STORAGE_BUCKET", "kalvid-renders"))
    supabase_signed_url_ttl: int = field(
        default_factory=lambda: int(os.getenv("SUPABASE_SIGNED_URL_TTL", "604800")))

    @property
    def postgres_dsn(self) -> str | None:
        """The Supabase Postgres connection string, or None if unset/placeholder."""
        u = self.supabase_db_url
        if not u or "YOUR_DB_PASSWORD" in u or "YOUR-PROJECT-REF" in u:
            return None
        return u

    @property
    def db_backend(self) -> str:
        """'postgres' when a real SUPABASE_DB_URL is configured, else 'sqlite'.
        Override explicitly with KALVID_DB_BACKEND."""
        explicit = os.getenv("KALVID_DB_BACKEND", "").strip().lower()
        if explicit in ("postgres", "sqlite"):
            return explicit
        return "postgres" if self.postgres_dsn else "sqlite"

    host: str = field(default_factory=lambda: os.getenv("KALVID_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(os.getenv("KALVID_PORT", "8000")))
    operator: str = field(default_factory=lambda: os.getenv("KALVID_OPERATOR", "operator"))

    # Master switch. While true, every provider call is routed to the mock adapter
    # and NOTHING is billable. Flip only when you actually mean to spend money.
    dry_run: bool = field(default_factory=lambda: _env_bool("KALVID_DRY_RUN", True))

    # How stale a price in rates.json may get before the dashboard flags it.
    # Provider prices move often; a silently stale table makes the budget guard lie.
    rate_staleness_days: int = field(default_factory=lambda: int(os.getenv("KALVID_RATE_STALENESS_DAYS", "30")))

    # Warn when a settled cost drifts this far from the pre-call estimate.
    cost_drift_warn_pct: float = field(default_factory=lambda: float(os.getenv("KALVID_COST_DRIFT_PCT", "25")))

    poll_interval_s: float = field(default_factory=lambda: float(os.getenv("KALVID_POLL_INTERVAL", "5")))
    poll_timeout_s: float = field(default_factory=lambda: float(os.getenv("KALVID_POLL_TIMEOUT", "1800")))


    @property
    def supabase_configured(self) -> bool:
        """True only when the placeholder values have actually been replaced."""
        return bool(
            self.supabase_url
            and self.supabase_service_role_key
            and "YOUR-PROJECT-REF" not in self.supabase_url
            and not self.supabase_service_role_key.startswith("YOUR_")
        )

    def provider_configured(self, name: str) -> bool:
        key = {"fal": self.fal_api_key, "runware": self.runware_api_key}.get(name)
        return bool(key) and not key.startswith("YOUR_")


settings = Settings()
settings.output_dir.mkdir(parents=True, exist_ok=True)
