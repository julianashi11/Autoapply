"""Local web app: live postings, resume tailoring and autofill. Run with `./aa web`."""

import json
import os
import re
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

import yaml

from . import browser, db, feed, jd, keywords, screening, tailor
from .config import load_search
from .fill import resolve_resume

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"
SETTINGS = ROOT / "data" / "settings.json"
TAILORED_DIR = ROOT / "data" / "tailored"
REFRESH_EVERY = 20 * 60  # seconds between scans

state = {"last_fetch": None, "fetching": False, "fetch_error": None, "new_ids": [],
         "progress": None, "info": None}
worker = None


# ---------- settings & profile ----------

def load_settings():
    try:
        return json.loads(SETTINGS.read_text())
    except Exception:
        return {}


def save_settings(s):
    SETTINGS.parent.mkdir(exist_ok=True)
    SETTINGS.write_text(json.dumps(s))
    os.chmod(SETTINGS, 0o600)


def api_key():
    return os.environ.get("ANTHROPIC_API_KEY") or load_settings().get("anthropic_api_key")


def load_profile():
    with open(ROOT / "profile.yaml") as f:
        return yaml.safe_load(f)


# ---------- feed refresh ----------

def refresh():
    if state["fetching"]:
        return
    state["fetching"] = True
    try:
        def progress(done, total):
            state["progress"] = [done, total]
        jobs, info = feed.collect(load_search(load_profile()), progress=progress)
        conn = db.connect()
        new = db.sync(conn, jobs)
        conn.close()
        state["info"] = info
        state["new_ids"] = new
        state["last_fetch"] = time.time()
        state["fetch_error"] = None
    except Exception as e:
        state["fetch_error"] = str(e)
    finally:
        state["fetching"] = False
        state["progress"] = None


def refresher():
    while True:
        refresh()
        time.sleep(REFRESH_EVERY)


# ---------- job helpers ----------

def tailored_path(row):
    """The tailored PDF for this job if one exists, else None."""
    p = row["tailored_pdf"]
    return Path(p) if p and Path(p).is_file() else None


def get_job(conn, job_id):
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise LookupError("No such job")
    return row


def base_resume_text(profile, track):
    path = resolve_resume(profile, track, ROOT)
    if not path:
        raise RuntimeError(f"Resume for '{track}' not found. Set resume.{track} in profile.yaml.")
    return tailor.resume_text(path)


def resume_as_text(r):
    parts = [r["name"], *r["contact"]]
    for s in r["sections"]:
        parts.append(s["title"])
        for e in s.get("entries") or []:
            parts += [e["organization"], e["role"], *e["bullets"]]
        parts += s.get("lines") or []
    return "\n".join(parts)


def job_detail(conn, job_id):
    row = get_job(conn, job_id)
    profile = load_profile()
    out = dict(row)
    out["locations"] = json.loads(row["locations"])
    lead = conn.execute("SELECT (julianday(feed_seen) - julianday(first_seen)) * 24 FROM jobs WHERE id=?",
                        (job_id,)).fetchone()[0]
    out["lead_hours"] = lead
    out["tailored"] = tailor.load(row)
    out["match"] = out["tailored_match"] = out["screen"] = out["tailored_screen"] = None
    if row["jd"]:
        try:
            base_path = resolve_resume(profile, row["track"], ROOT)
            base = base_resume_text(profile, row["track"])
            out["match"] = keywords.match(row["jd"], base)
            out["screen"] = screening.estimate(row["jd"], base, profile, out, pages=tailor.pdf_pages(base_path))
        except Exception as e:
            out["match_error"] = str(e)
        if out["tailored"]:
            text = resume_as_text(out["tailored"])
            pdf = tailored_path(row)
            out["tailored_match"] = keywords.match(row["jd"], text)
            out["tailored_screen"] = screening.estimate(row["jd"], text, profile, out,
                                                        pages=tailor.pdf_pages(pdf) if pdf else 1)
        best = out["tailored_screen"] or out["screen"]
        fields = {}
        if out["match"] and out["match"]["score"] != row["score"]:
            fields["score"] = out["match"]["score"]
        if best and best["score"] != row["screen"]:
            fields["screen"] = best["score"]
        if fields:
            db.update(conn, row["id"], **fields)
    out["has_pdf"] = tailored_path(row) is not None
    return out


def ensure_jd(conn, row):
    if row["jd"]:
        return row["jd"]
    text = None
    try:
        text = jd.fetch(row["url"], row["ats"])
    except Exception:
        text = None
    if not text:
        text = worker.submit(browser.page_text, row["url"]).result(timeout=90)
    text = (text or "").strip()[:30000]
    profile = load_profile()
    score = None
    try:
        score = keywords.match(text, base_resume_text(profile, row["track"]))["score"]
    except Exception:
        pass
    db.update(conn, row["id"], jd=text, score=score)
    return text


def render_and_save(conn, row, resume):
    TAILORED_DIR.mkdir(parents=True, exist_ok=True)
    out = TAILORED_DIR / tailor.pdf_name(load_profile(), row)
    pages = worker.submit(browser.render_pdf, tailor.render_html(resume), out).result(timeout=60)
    db.update(conn, row["id"], tailored=json.dumps(resume), tailored_pdf=str(out))
    return pages


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def _route(self, method):
        # Only accept requests from this machine's own page.
        host = self.headers.get("Host", "")
        if not re.match(r"^(127\.0\.0\.1|localhost)(:\d+)?$", host):
            return self._send(403, {"error": "forbidden"})
        if method == "POST" and self.headers.get("X-Autoapply") != "1":
            return self._send(403, {"error": "forbidden"})

        path = self.path.split("?")[0]
        conn = db.connect()
        try:
            if method == "GET" and path in ("/", "/index.html"):
                return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            if method == "GET" and path == "/api/state":
                return self._send(200, self.api_state(conn))
            if method == "POST" and path == "/api/refresh":
                threading.Thread(target=refresh, daemon=True).start()
                return self._send(200, {"ok": True})
            if path == "/api/settings":
                if method == "POST":
                    s = load_settings()
                    key = (self._body().get("anthropic_api_key") or "").strip()
                    if key:
                        s["anthropic_api_key"] = key
                    else:
                        s.pop("anthropic_api_key", None)
                    save_settings(s)
                return self._send(200, {"has_key": bool(api_key())})

            m = re.match(r"^/api/job/([\w:.-]+?)(?:/(\w+))?(\.pdf)?$", unquote(path))
            if m:
                return self.api_job(conn, method, m.group(1), m.group(2), bool(m.group(3)))
            return self._send(404, {"error": "not found"})
        except LookupError as e:
            return self._send(404, {"error": str(e)})
        except Exception as e:
            traceback.print_exc()
            return self._send(500, {"error": str(e)})
        finally:
            conn.close()

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def api_state(self, conn):
        rows = conn.execute(
            "SELECT id, company, title, url, locations, track, ats, sponsorship, date_posted, "
            "status, status_at, first_seen, score, screen, tailored_pdf IS NOT NULL AS tailored, "
            "source, found_by, feed_seen, "
            "CAST((julianday(feed_seen) - julianday(first_seen)) * 24 AS REAL) AS lead_hours "
            "FROM jobs WHERE active=1 OR status='applied' ORDER BY date_posted DESC"
        ).fetchall()
        jobs = []
        for r in rows:
            j = dict(r)
            j["locations"] = json.loads(r["locations"])
            jobs.append(j)
        return {
            "jobs": jobs,
            "last_fetch": state["last_fetch"],
            "fetching": state["fetching"],
            "fetch_error": state["fetch_error"],
            "new_ids": state["new_ids"],
            "progress": state["progress"],
            "info": state["info"],
            "has_key": bool(api_key()),
            "can_tailor": bool(api_key() or tailor.claude_cli()),
            "tailor_via": "cli" if tailor.claude_cli() else "key" if api_key() else None,
            "refresh_every": REFRESH_EVERY,
        }

    def api_job(self, conn, method, job_id, action, is_pdf):
        row = get_job(conn, job_id)
        if method == "GET" and action == "resume" and is_pdf:
            p = tailored_path(row)
            if not p:
                return self._send(404, {"error": "No tailored resume yet"})
            return self._send(200, p.read_bytes(), "application/pdf")
        if method == "GET" and action is None:
            return self._send(200, job_detail(conn, job_id))

        if method == "POST" and action == "jd":
            body = self._body()
            if body.get("text"):
                db.update(conn, job_id, jd=body["text"][:30000])
            else:
                ensure_jd(conn, row)
            return self._send(200, job_detail(conn, job_id))

        if method == "POST" and action == "tailor":
            key = api_key()
            text = ensure_jd(conn, row)
            profile = load_profile()
            resume = tailor.tailor(key, base_resume_text(profile, row["track"]), dict(row), text)
            pages = render_and_save(conn, get_job(conn, job_id), resume)
            out = job_detail(conn, job_id)
            out["pages"] = pages
            return self._send(200, out)

        if method == "POST" and action == "resume":
            resume = self._body()["resume"]
            pages = render_and_save(conn, row, resume)
            out = job_detail(conn, job_id)
            out["pages"] = pages
            return self._send(200, out)

        if method == "POST" and action == "discard":
            p = tailored_path(row)
            if p:
                p.unlink()
            db.update(conn, job_id, tailored=None, tailored_pdf=None)
            return self._send(200, job_detail(conn, job_id))

        if method == "POST" and action == "autofill":
            profile = load_profile()
            use_tailored = self._body().get("use_tailored", True)
            pdf = tailored_path(row)
            resume = pdf if use_tailored and pdf else resolve_resume(profile, row["track"], ROOT)
            if not resume:
                return self._send(400, {"error": f"Resume for '{row['track']}' not found."})
            report = worker.submit(browser.autofill, dict(row), profile, resume).result(timeout=180)
            return self._send(200, report)

        if method == "POST" and action == "status":
            db.set_status(conn, job_id, self._body()["status"])
            return self._send(200, {"ok": True})

        return self._send(404, {"error": "not found"})


def serve(port=8765, open_browser=True):
    global worker
    if not (ROOT / "profile.yaml").exists():
        raise SystemExit("No profile.yaml yet. Run: cp profile.example.yaml profile.yaml, then fill it in.")
    db.connect().close()
    worker = browser.BrowserWorker()
    threading.Thread(target=refresher, daemon=True).start()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}"
    print(f"Autoapply is running at {url}  (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
