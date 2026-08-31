import os
import tempfile
from pathlib import Path

# Must be set before app.config is imported — settings are read at import time.
_tmp = Path(tempfile.mkdtemp(prefix="kalvid-test-"))
os.environ["KALVID_DB"] = str(_tmp / "test.db")
os.environ["KALVID_OUTPUT_DIR"] = str(_tmp / "outputs")
os.environ["KALVID_DRY_RUN"] = "1"
os.environ["KALVID_POLL_INTERVAL"] = "0.05"

import pytest

from app import db


@pytest.fixture(autouse=True)
def fresh_db():
    """Each test gets an empty database."""
    db.init_db()
    conn = db.get_conn()
    conn.executescript(
        "DELETE FROM assets; DELETE FROM budget_events; DELETE FROM generations; "
        "DELETE FROM identity_versions; "
        "DELETE FROM jobs; DELETE FROM personas; DELETE FROM clients;"
    )
    conn.commit()
    yield


@pytest.fixture
def client_id():
    return db.insert(
        "INSERT INTO clients (name, monthly_budget_cap, default_job_cap) VALUES (?,?,?)",
        ("Acme", 100.0, 0.0),
    )


@pytest.fixture
def persona_id(client_id):
    return db.insert(
        """INSERT INTO personas (client_id, name, identity_strategy,
                                 reference_image_url, notes)
           VALUES (?,?,?,?,?)""",
        (client_id, "Rania", "reference_image",
         "https://example.test/rania-locked.png", "24yo, curly dark hair"),
    )
