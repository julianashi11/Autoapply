import json
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "jobs.db"

STATUSES = ("new", "applied", "skipped", "later", "manual")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    company     TEXT NOT NULL,
    title       TEXT NOT NULL,
    url         TEXT NOT NULL,
    locations   TEXT NOT NULL,
    category    TEXT NOT NULL,
    track       TEXT NOT NULL,          -- swe | pm
    ats         TEXT NOT NULL,
    sponsorship TEXT,
    date_posted INTEGER,
    active      INTEGER NOT NULL DEFAULT 1,
    status      TEXT NOT NULL DEFAULT 'new',
    status_at   TEXT,
    notes       TEXT
);
"""


def connect():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def upsert(conn, job):
    """Insert a new job or refresh its listing fields. Never touches status."""
    conn.execute(
        """
        INSERT INTO jobs (id, company, title, url, locations, category, track, ats,
                          sponsorship, date_posted, active)
        VALUES (:id, :company, :title, :url, :locations, :category, :track, :ats,
                :sponsorship, :date_posted, 1)
        ON CONFLICT(id) DO UPDATE SET
            company=excluded.company, title=excluded.title, url=excluded.url,
            locations=excluded.locations, category=excluded.category,
            track=excluded.track, ats=excluded.ats, sponsorship=excluded.sponsorship,
            active=1
        """,
        {**job, "locations": json.dumps(job["locations"])},
    )


def set_status(conn, job_id, status, notes=None):
    assert status in STATUSES, status
    conn.execute(
        "UPDATE jobs SET status=?, status_at=datetime('now','localtime'), "
        "notes=COALESCE(?, notes) WHERE id=?",
        (status, notes, job_id),
    )
    conn.commit()
