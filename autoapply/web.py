"""Local web app: live postings, resume tailoring and autofill. Run with `./aa web`."""

import json
import os
import re
import threading
import time
import traceback
import urllib.request
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
CLOUD_EVERY = 2 * 60        # seconds between checks of the cloud scanner's results
CLOUD_STALE = 45 * 60       # cloud data older than this -> scan locally instead
LOCAL_EVERY = 20 * 60       # seconds between local scans when the cloud is unavailable
REFRESH_EVERY = CLOUD_EVERY

state = {"last_fetch": None, "fetching": False, "fetch_error": None, "new_ids": [],
         "progress": None, "info": None, "mode": None, "cloud_updated": None,
         "last_local": 0, "etag": None}
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


# ---------- keeping postings up to date ----------

def _local_time(ts):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else None


def pull_cloud():
    """Copies the cloud scanner's latest results into the local database.

    Returns True if the data was fresh enough to use, False if it's stale."""
    from .direct import canon as canon_of

    site = (load_search(load_profile()).get("site_url") or "").rstrip("/")
    if not site:
        return False
    req = urllib.request.Request(f"{site}/jobs.json?t={int(time.time())}",
                                 headers={"User-Agent": "autoapply-local", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    state["cloud_updated"] = data.get("updated")
    if time.time() - (data.get("updated") or 0) > CLOUD_STALE:
        return False
    jobs = []
    for j in data.get("jobs", []):
        first = j.get("first_seen")
        lead = j.get("lead_hours")
        jobs.append({
            "id": j["id"], "canon": j.get("canon") or canon_of(j["url"]),
            "company": j["company"], "title": j["title"], "url": j["url"],
            "locations": j.get("locations") or [], "category": "cloud", "track": j["track"],
            "ats": j["ats"], "sponsorship": j.get("sponsorship"), "date_posted": j.get("date_posted"),
            "source": j.get("source") or "simplify", "found_by": j.get("found_by"),
            "first_seen": _local_time(first),
            "feed_seen": _local_time(first + lead * 3600) if first and lead is not None else None,
        })
    conn = db.connect()
    new = db.sync(conn, jobs, complete=True)
    conn.close()
    state["new_ids"] = new
    state["info"] = data.get("info")
    state["last_fetch"] = time.time()
    state["mode"] = "cloud"
    return True


def scan_locally():
    def progress(done, total):
        state["progress"] = [done, total]
    jobs, info = feed.collect(load_search(load_profile()), progress=progress)
    conn = db.connect()
    new = db.sync(conn, jobs)
    conn.close()
    state.update(info=info, new_ids=new, last_fetch=time.time(), mode="local", last_local=time.time())


def refresh(force_local=False):
    if state["fetching"]:
        return
    state["fetching"] = True
    try:
        used_cloud = False
        if not force_local:
            try:
                used_cloud = pull_cloud()
            except Exception as e:
                state["fetch_error"] = f"cloud: {e}"
        if not used_cloud and (force_local or time.time() - state["last_local"] > LOCAL_EVERY):
            scan_locally()
        state["fetch_error"] = None
    except Exception as e:
        state["fetch_error"] = str(e)
    finally:
        state["fetching"] = False
        state["progress"] = None


def request_cloud_scan():
    """Asks GitHub to run the cloud scanner now (needs the gh CLI, logged in)."""
    import shutil
    import subprocess

    site = load_search(load_profile()).get("site_url") or ""
    m = re.match(r"https://([\w-]+)\.github\.io/([\w.-]+)", site)
    gh = shutil.which("gh") or next((p for p in ("/opt/homebrew/bin/gh", "/usr/local/bin/gh") if Path(p).exists()), None)
    if not (m and gh):
        return False
    r = subprocess.run([gh, "workflow", "run", "scan.yml", "-R", f"{m.group(1)}/{m.group(2)}"],
                       capture_output=True, text=True, timeout=30)
    return r.returncode == 0


def refresher():
    while True:
        refresh()
        time.sleep(CLOUD_EVERY)


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
            parts += [e.get("organization", ""), e.get("role", ""), e.get("subtitle", ""), *e.get("bullets", [])]
        parts += (s.get("lines") or []) + (s.get("bullets") or [])
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
    if out["has_pdf"]:
        out["pages"] = tailor.pdf_pages(tailored_path(row))
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


def base_style(row):
    path = resolve_resume(load_profile(), row["track"], ROOT)
    return tailor.detect_style(path) if path else tailor.default_style()


def render_and_save(conn, row, resume):
    """Writes the tailored PDF in the original resume's style. Returns the fit result."""
    TAILORED_DIR.mkdir(parents=True, exist_ok=True)
    out = TAILORED_DIR / tailor.pdf_name(load_profile(), row)
    style = base_style(row)
    fit = worker.submit(browser.render_pdf, tailor.render_html(resume, style), out,
                        tailor.fit_steps(style)).result(timeout=90)
    db.update(conn, row["id"], tailored=json.dumps(resume), tailored_pdf=str(out))
    return fit


def fit_note(fit, style):
    """Plain-English summary of what was adjusted to fit one page."""
    notes = []
    if fit["fs"] < style["body"]:
        notes.append(f"font {style['body']}pt to {fit['fs']}pt")
    if fit["mt"] < style["margin_top"] or fit["ms"] < style["margin_side"]:
        notes.append("smaller margins")
    if fit["lh"] < style["lh"]:
        notes.append("tighter line spacing")
    return notes


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
                body = self._body()
                if body.get("local"):
                    threading.Thread(target=refresh, kwargs={"force_local": True}, daemon=True).start()
                    return self._send(200, {"ok": True, "message": "Scanning company boards from this computer (about 6 minutes)."})
                started = request_cloud_scan()
                threading.Thread(target=refresh, daemon=True).start()
                return self._send(200, {"ok": True, "message": "Cloud scan started; new roles show up here in about 8 minutes."
                                        if started else "Checked for the latest results."})
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
            "FROM jobs WHERE active=1 OR status='applied' ORDER BY first_seen DESC, date_posted DESC"
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
            "mode": state["mode"],
            "cloud_updated": state["cloud_updated"],
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
            base = base_resume_text(profile, row["track"])
            resume = tailor.tailor(key, base, dict(row), text)
            fit = render_and_save(conn, get_job(conn, job_id), resume)
            if not fit["fits"]:
                # Still too long at the smallest allowed size: ask for a shorter version once.
                style = base_style(row)
                lines = max(2, round(fit["overflow"] / (fit["fs"] * 96 / 72 * fit["lh"])) + 1)
                resume = tailor.tailor(key, base, dict(row), text, feedback=(
                    f"Your previous version ran about {lines} lines past one page. Cut at least "
                    f"{lines} lines: drop the least relevant bullets or entries and shorten "
                    f"bullets that wrap onto a second line by only a few words."))
                fit = render_and_save(conn, get_job(conn, job_id), resume)
            out = job_detail(conn, job_id)
            out["pages"], out["fit"] = fit["pages"], fit_note(fit, base_style(row))
            return self._send(200, out)

        if method == "POST" and action == "resume":
            resume = self._body()["resume"]
            fit = render_and_save(conn, row, resume)
            out = job_detail(conn, job_id)
            out["pages"], out["fit"] = fit["pages"], fit_note(fit, base_style(row))
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
