"""Cloud scanner, run by GitHub Actions on a schedule.

1. Loads the previous scan's state from the live site (jobs.json).
2. Scans company job boards and the community list.
3. Emails any roles it hasn't seen before.
4. Writes the site (index.html + jobs.json) for GitHub Pages to publish.

Environment: SITE_URL, EMAIL_TO, SMTP_USER, SMTP_PASSWORD (Gmail app password),
optional SMTP_HOST / SMTP_PORT.
"""

import html
import json
import os
import re
import shutil
import smtplib
import sys
import time
import urllib.error
import urllib.request
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from . import feed
from .config import load_search

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "_site"
KEEP_SEEN_DAYS = 180


def load_previous(site_url):
    """Previous state, {} on the very first run. Raises if the site can't be read,
    so a network hiccup never makes every role look new."""
    url = f"{site_url.rstrip('/')}/jobs.json?t={int(time.time())}"
    req = urllib.request.Request(url, headers={"User-Agent": "autoapply-cloud", "Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {}
        raise


def merge(jobs, prev):
    """Carry first-seen times forward and work out which roles are new."""
    now = int(time.time())
    seen = dict(prev.get("seen") or {})              # id -> first seen
    feed_seen = dict(prev.get("feed_seen") or {})     # id -> first time the community list had it
    found_by = dict(prev.get("found_by") or {})       # id -> direct | simplify | both
    new = []
    first_run = not prev
    for j in jobs:
        jid = j["id"]
        if jid not in seen:
            # On the first run, date each role by its posting date so the page isn't all "new".
            seen[jid] = min(now, j.get("date_posted") or now) if first_run else now
            found_by[jid] = j["source"]
            new.append(j)
        if j["source"] in ("simplify", "both") and jid not in feed_seen:
            feed_seen[jid] = now
        j["first_seen"] = seen[jid]
        j["found_by"] = found_by.get(jid, j["source"])
        fs = feed_seen.get(jid)
        j["lead_hours"] = round((fs - seen[jid]) / 3600, 1) if fs and j["found_by"] == "direct" else None
    cutoff = now - KEEP_SEEN_DAYS * 86400
    active = {j["id"] for j in jobs}
    seen = {k: v for k, v in seen.items() if v >= cutoff or k in active}
    return new, {
        "seen": seen,
        "feed_seen": {k: v for k, v in feed_seen.items() if k in seen},
        "found_by": {k: v for k, v in found_by.items() if k in seen},
    }


def public_job(j):
    keys = ("id", "canon", "company", "title", "url", "locations", "track", "ats", "sponsorship",
            "date_posted", "source", "found_by", "first_seen", "lead_hours")
    return {k: j.get(k) for k in keys}


def email_html(new, site_url):
    def rows(track):
        out = []
        for j in sorted((j for j in new if j["track"] == track),
                        key=lambda j: (j["source"] != "direct", j["company"].lower())):
            where = ", ".join(j["locations"][:2]) + (f" +{len(j['locations']) - 2}" if len(j["locations"]) > 2 else "")
            tag = (" <span style='background:#eaf0ff;color:#2f5bea;border-radius:9px;padding:1px 7px;"
                   "font-size:11px;font-weight:600'>Company site</span>") if j["source"] == "direct" else ""
            out.append(
                f"<tr><td style='padding:8px 0;border-bottom:1px solid #eee'>"
                f"<b>{html.escape(j['company'])}</b>{tag}<br>"
                f"<a href='{html.escape(j['url'])}' style='color:#2f5bea'>{html.escape(j['title'])}</a><br>"
                f"<span style='color:#666;font-size:12px'>{html.escape(where)}</span></td></tr>")
        return "".join(out)

    parts = ["<div style='font-family:-apple-system,Segoe UI,Arial,sans-serif;max-width:640px'>"]
    for track, name in (("swe", "Software engineering"), ("pm", "Product management")):
        r = rows(track)
        if r:
            n = sum(1 for j in new if j["track"] == track)
            parts.append(f"<h3 style='margin:18px 0 4px'>{name} ({n})</h3><table style='width:100%;border-collapse:collapse'>{r}</table>")
    parts.append(f"<p style='color:#666;font-size:12px;margin-top:18px'>\"Company site\" means it was found on the "
                 f"company's own job board, not on the GitHub list. "
                 f"<a href='{html.escape(site_url)}'>See every open role</a>.</p></div>")
    return "".join(parts)


def send_resend(key, to, subject, text, html_body):
    """Sends through Resend (resend.com). Its test sender can mail the address you signed up with."""
    body = json.dumps({
        "from": os.environ.get("EMAIL_FROM") or "Autoapply <onboarding@resend.dev>",
        "to": [a.strip() for a in to.split(",")], "subject": subject, "text": text, "html": html_body,
    }).encode()
    req = urllib.request.Request("https://api.resend.com/emails", data=body, headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": "autoapply-cloud"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


def send_email(new, site_url):
    to, user, pw = os.environ.get("EMAIL_TO"), os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASSWORD")
    resend_key = os.environ.get("RESEND_API_KEY")
    if not (to and (resend_key or (user and pw))):
        print("Email not configured (EMAIL_TO plus RESEND_API_KEY or SMTP_USER / SMTP_PASSWORD); skipping.")
        return
    companies = list(dict.fromkeys(j["company"] for j in sorted(new, key=lambda j: j["source"] != "direct")))
    subject = (f"{len(new)} new internship{'s' if len(new) != 1 else ''}: "
               + ", ".join(companies[:3]) + (f" +{len(companies) - 3} more" if len(companies) > 3 else ""))
    text = "\n".join(f"{j['company']} | {j['title']} | {j['url']}" for j in new)
    if resend_key:
        send_resend(resend_key, to, subject, text, email_html(new, site_url))
        print(f"Emailed {len(new)} new roles to {to} via Resend.")
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, f"Autoapply <{user}>", to
    text = "\n".join(f"{j['company']} | {j['title']} | {j['url']}" for j in new)
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(email_html(new, site_url), "html"))
    host = os.environ.get("SMTP_HOST") or ("smtp.gmail.com" if user.endswith("@gmail.com") else "smtp-mail.outlook.com"
                                            if re.search(r"@(outlook|hotmail|live)\.com$", user) else "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT") or (465 if "gmail" in host else 587))
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=30)
    else:
        server = smtplib.SMTP(host, port, timeout=30)
        server.starttls()
    with server as s:
        s.login(user, pw)
        s.sendmail(user, [a.strip() for a in to.split(",")], msg.as_string())
    print(f"Emailed {len(new)} new roles to {to}.")


def test_email(site_url):
    """Sends the three newest roles from the live site, to check the email setup."""
    prev = load_previous(site_url)
    jobs = sorted(prev.get("jobs") or [], key=lambda j: -(j.get("first_seen") or 0))[:3]
    if not jobs:
        sys.exit("No roles on the live site yet; run a scan first.")
    if not (os.environ.get("RESEND_API_KEY") or os.environ.get("SMTP_PASSWORD")):
        sys.exit("No email secret is set (RESEND_API_KEY, or SMTP_USER + SMTP_PASSWORD).")
    send_email(jobs, site_url)


def main():
    site_url = os.environ.get("SITE_URL", "").rstrip("/")
    if os.environ.get("TEST_EMAIL") == "true":
        return test_email(site_url)
    prev = load_previous(site_url) if site_url else {}
    first_run = not prev

    jobs, info = feed.collect(load_search())
    print(f"Scan: {info}")
    if prev and info["direct"] < 0.5 * len([j for j in prev.get("jobs", []) if j.get("source") != "simplify"]):
        sys.exit("Scan returned far fewer roles than last time; not publishing (likely a network problem).")

    new, state = merge(jobs, prev)
    email_failed = False
    if first_run:
        print(f"First run: now tracking {len(jobs)} roles. Emails start with the next new posting.")
    elif new:
        try:
            send_email(new, site_url)
        except Exception as e:
            # Leave these roles unseen so the next run emails them again.
            print(f"::error::Email failed: {e}")
            for j in new:
                for k in ("seen", "found_by"):
                    state[k].pop(j["id"], None)
            email_failed = True
    else:
        print("No new roles.")

    OUT.mkdir(exist_ok=True)
    shutil.copy(ROOT / "site" / "index.html", OUT / "index.html")
    data = {"updated": int(time.time()), "info": info,
            "jobs": [public_job(j) for j in sorted(jobs, key=lambda j: -(j.get("date_posted") or 0))],
            **state}
    (OUT / "jobs.json").write_text(json.dumps(data, separators=(",", ":")))
    print(f"Wrote {len(jobs)} roles to {OUT}.")
    if email_failed:
        (ROOT / ".email_failed").write_text("1")


if __name__ == "__main__":
    main()
