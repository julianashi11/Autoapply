import json
import sqlite3
import threading
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

# Columns added after the first version; created on demand for existing databases.
EXTRA_COLUMNS = {
    "first_seen": "TEXT",
    "jd": "TEXT",              # job description text
    "score": "INTEGER",        # keyword match % for the base resume
    "tailored": "TEXT",        # tailored resume JSON
    "tailored_pdf": "TEXT",    # path to the tailored PDF
    "canon": "TEXT",           # source-independent posting key, e.g. gh:12345
    "source": "TEXT",          # direct | simplify | both (latest scan)
    "found_by": "TEXT",        # which source saw it first
    "feed_seen": "TEXT",       # when the community feed first listed it
    "last_seen": "TEXT",
    "screen": "INTEGER",       # screening-chance estimate (0-100) for the resume you'd send
}

STALE_AFTER = "-3 hours"  # a posting missing from scans this long is treated as closed

_lock = threading.Lock()
_migrated = False


def connect():
    global _migrated
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    if _migrated:
        return conn
    with _lock:
        conn.executescript(SCHEMA)
        have = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
        for col, typ in EXTRA_COLUMNS.items():
            if col not in have:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {typ}")
        conn.execute("UPDATE jobs SET first_seen=datetime('now','localtime') WHERE first_seen IS NULL")
        from .direct import canon
        for r in conn.execute("SELECT id, url FROM jobs WHERE canon IS NULL").fetchall():
            conn.execute("UPDATE jobs SET canon=?, found_by=COALESCE(found_by,'simplify'), "
                         "feed_seen=COALESCE(feed_seen, first_seen) WHERE id=?", (canon(r["url"]), r["id"]))
        conn.commit()
        _migrated = True
    return conn


def upsert(conn, job):
    """Insert a new job or refresh its listing fields. Never touches status.

    `first_seen` / `found_by` / `feed_seen` may be given (e.g. from the cloud scanner);
    otherwise they're set from this sighting. The earliest first_seen always wins."""
    conn.execute(
        """
        INSERT INTO jobs (id, company, title, url, locations, category, track, ats,
                          sponsorship, date_posted, active, first_seen, canon, source,
                          found_by, feed_seen, last_seen)
        VALUES (:id, :company, :title, :url, :locations, :category, :track, :ats,
                :sponsorship, :date_posted, 1, COALESCE(:first_seen, datetime('now','localtime')),
                :canon, :source, COALESCE(:found_by, :source),
                COALESCE(:feed_seen, CASE WHEN :source != 'direct' THEN datetime('now','localtime') END),
                datetime('now','localtime'))
        ON CONFLICT(id) DO UPDATE SET
            company=excluded.company, title=excluded.title, url=excluded.url,
            locations=excluded.locations, category=excluded.category,
            track=excluded.track, ats=excluded.ats,
            sponsorship=COALESCE(excluded.sponsorship, jobs.sponsorship),
            date_posted=COALESCE(MIN(jobs.date_posted, excluded.date_posted), excluded.date_posted, jobs.date_posted),
            canon=COALESCE(excluded.canon, jobs.canon), source=excluded.source,
            first_seen=MIN(jobs.first_seen, excluded.first_seen),
            found_by=CASE WHEN excluded.first_seen < jobs.first_seen THEN excluded.found_by ELSE jobs.found_by END,
            feed_seen=COALESCE(MIN(jobs.feed_seen, excluded.feed_seen), jobs.feed_seen, excluded.feed_seen),
            last_seen=datetime('now','localtime'), active=1
        """,
        {"canon": None, "source": "simplify", "first_seen": None, "found_by": None, "feed_seen": None,
         **job, "locations": json.dumps(job["locations"])},
    )


def set_status(conn, job_id, status, notes=None):
    assert status in STATUSES, status
    with _lock:
        conn.execute(
            "UPDATE jobs SET status=?, status_at=datetime('now','localtime'), "
            "notes=COALESCE(?, notes) WHERE id=?",
            (status, notes, job_id),
        )
        conn.commit()


def update(conn, job_id, **fields):
    cols = ", ".join(f"{k}=?" for k in fields)
    with _lock:
        conn.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))
        conn.commit()


def sync(conn, jobs, complete=False):
    """Record a scan's postings. Returns the ids that are new.

    complete=True means `jobs` is the full list of open roles (the cloud scanner's), so
    anything missing from it is closed right away; otherwise roles close after STALE_AFTER."""
    with _lock:
        before = {r["id"] for r in conn.execute("SELECT id FROM jobs")}
        # Keep each posting's existing row (and status) whichever source finds it.
        by_canon = {r["canon"]: r["id"] for r in conn.execute("SELECT id, canon FROM jobs WHERE canon IS NOT NULL")}
        for job in jobs:
            if job.get("canon") in by_canon:
                job["id"] = by_canon[job["canon"]]
            upsert(conn, job)
        if complete:
            conn.execute("CREATE TEMP TABLE IF NOT EXISTS open_ids (id TEXT PRIMARY KEY)")
            conn.execute("DELETE FROM open_ids")
            conn.executemany("INSERT OR IGNORE INTO open_ids VALUES (?)", [(j["id"],) for j in jobs])
            conn.execute("UPDATE jobs SET active=0 WHERE active=1 AND id NOT IN (SELECT id FROM open_ids)")
        else:
            conn.execute(f"UPDATE jobs SET active=0 WHERE last_seen IS NULL OR last_seen < datetime('now','localtime','{STALE_AFTER}')")
        conn.commit()
    return [j["id"] for j in jobs if j["id"] not in before]
