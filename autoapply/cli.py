import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

import yaml

from . import db, feed
from .fill import application_url, fill_page, looks_closed, resolve_resume

ROOT = Path(__file__).resolve().parent.parent
PROFILE = ROOT / "profile.yaml"
BROWSER_DIR = ROOT / "data" / "browser"
AUTOFILL_ATS = ("greenhouse", "lever", "ashby")


def load_profile():
    if not PROFILE.exists():
        sys.exit("No profile.yaml yet. Run: cp profile.example.yaml profile.yaml, then fill it in.")
    with open(PROFILE) as f:
        return yaml.safe_load(f)


def fmt_date(ts):
    return datetime.fromtimestamp(ts).strftime("%b %d") if ts else "?"


def query_jobs(conn, args, statuses=("new",)):
    sql = "SELECT * FROM jobs WHERE active=1"
    params = []
    if statuses:
        sql += f" AND status IN ({','.join('?' * len(statuses))})"
        params += statuses
    if getattr(args, "track", None):
        sql += " AND track=?"
        params.append(args.track)
    ats = getattr(args, "ats", None)
    if ats and ats != "all":
        names = ats.split(",")
        sql += f" AND ats IN ({','.join('?' * len(names))})"
        params += names
    if getattr(args, "company", None):
        sql += " AND company LIKE ?"
        params.append(f"%{args.company}%")
    sql += " ORDER BY date_posted DESC"
    if getattr(args, "limit", None):
        sql += f" LIMIT {int(args.limit)}"
    return conn.execute(sql, params).fetchall()


def cmd_fetch(args):
    profile = load_profile()
    print("Downloading listings feed...")
    listings = feed.download()
    jobs = list(feed.matching(listings, profile["search"]))
    conn = db.connect()
    before = {r["id"] for r in conn.execute("SELECT id FROM jobs")}
    conn.execute("UPDATE jobs SET active=0")
    for job in jobs:
        db.upsert(conn, job)
    conn.commit()
    new = [j for j in jobs if j["id"] not in before]
    by_track = {}
    for j in new:
        by_track[j["track"]] = by_track.get(j["track"], 0) + 1
    print(f"{len(jobs)} open {profile['search']['term']} roles match your profile; "
          f"{len(new)} are new since the last fetch {by_track or ''}")
    if new:
        print("Run `./aa list` to see them.")


def cmd_list(args):
    conn = db.connect()
    statuses = None if args.status == "all" else (args.status,)
    rows = query_jobs(conn, args, statuses)
    if not rows:
        print("Nothing here. Try `./aa fetch` or a different --status.")
        return
    for r in rows:
        locs = json.loads(r["locations"])
        loc = locs[0] + (f" +{len(locs) - 1}" if len(locs) > 1 else "") if locs else ""
        print(f"{r['id'][:8]}  {fmt_date(r['date_posted']):6}  {r['track']:3}  {r['ats']:10}  "
              f"{r['status']:7}  {r['company'][:22]:22}  {r['title'][:50]:50}  {loc}")
    print(f"\n{len(rows)} roles")


def cmd_apply(args):
    profile = load_profile()
    conn = db.connect()
    statuses = ("new", "later") if args.include_later else ("new",)
    if args.id:
        rows = conn.execute("SELECT * FROM jobs WHERE id LIKE ?", (args.id + "%",)).fetchall()
    else:
        rows = query_jobs(conn, args, statuses)
    if not rows:
        print("No matching roles. Run `./aa fetch` first, or loosen your filters.")
        return

    for track in {r["track"] for r in rows}:
        if not resolve_resume(profile, track, ROOT):
            sys.exit(f"Resume for '{track}' not found. Set resume.{track} in profile.yaml.")

    from playwright.sync_api import sync_playwright

    print(f"{len(rows)} roles queued. Each one opens in the browser with the form filled in.\n"
          "Check every field, answer anything left blank, and click Submit yourself.\n")
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(BROWSER_DIR), headless=False, viewport=None, args=["--start-maximized"]
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        for n, r in enumerate(rows, 1):
            resume = resolve_resume(profile, r["track"], ROOT)
            print(f"[{n}/{len(rows)}] {r['company']} | {r['title']} ({r['track'].upper()}, {r['ats']})")
            url = application_url(r["url"], r["ats"])
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(2500)
            except Exception as e:
                print(f"   Couldn't load page: {e}")
            if looks_closed(page):
                print("   This posting looks closed.")

            def run_fill():
                if r["ats"] not in AUTOFILL_ATS:
                    print("   No autofill for this site; fill it in by hand.")
                    return
                rep = fill_page(page, profile, resume)
                print(f"   Filled {len(rep['filled'])} fields" +
                      (f" (resume: {resume.name})" if "Resume" in rep["filled"] else " (resume NOT uploaded)"))
                for q in dict.fromkeys(rep["unfilled"]):
                    print(f"   Needs you: {q[:100]}")
                for e in rep["errors"][:5]:
                    print(f"   Problem: {e[:120]}")

            run_fill()
            while True:
                choice = input("   [a]pplied  [s]kip  [l]ater  [r]efill  [q]uit > ").strip().lower()
                if choice == "r":
                    run_fill()
                    continue
                if choice in ("a", "s", "l", "q"):
                    break
            if choice == "q":
                break
            status = {"a": "applied", "s": "skipped", "l": "later"}[choice]
            db.set_status(conn, r["id"], status)
            print(f"   Marked {status}.\n")
        ctx.close()
    cmd_stats(args)


def cmd_mark(args):
    conn = db.connect()
    rows = conn.execute("SELECT id, company, title FROM jobs WHERE id LIKE ?", (args.id + "%",)).fetchall()
    if len(rows) != 1:
        sys.exit(f"{len(rows)} roles match id '{args.id}'; give more characters.")
    db.set_status(conn, rows[0]["id"], args.status, args.note)
    print(f"{rows[0]['company']} | {rows[0]['title']} -> {args.status}")


def cmd_stats(args):
    conn = db.connect()
    rows = conn.execute(
        "SELECT track, status, COUNT(*) n FROM jobs WHERE active=1 OR status='applied' "
        "GROUP BY track, status ORDER BY track, status"
    ).fetchall()
    if not rows:
        print("No roles tracked yet. Run `./aa fetch`.")
        return
    print("\nTrack  Status   Count")
    for r in rows:
        print(f"{r['track']:5}  {r['status']:7}  {r['n']}")


def cmd_export(args):
    conn = db.connect()
    rows = conn.execute("SELECT * FROM jobs ORDER BY status, date_posted DESC").fetchall()
    with open(args.path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["company", "title", "track", "status", "status_at", "posted", "locations", "url", "notes"])
        for r in rows:
            w.writerow([r["company"], r["title"], r["track"], r["status"], r["status_at"],
                        fmt_date(r["date_posted"]), "; ".join(json.loads(r["locations"])),
                        r["url"], r["notes"]])
    print(f"Wrote {len(rows)} rows to {args.path}")


def main():
    ap = argparse.ArgumentParser(prog="aa", description="Find and apply to Summer 2027 SWE/PM internships.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("fetch", help="pull open roles that match your profile")

    def filters(p, limit=None):
        p.add_argument("--track", choices=["swe", "pm"])
        p.add_argument("--ats", help="greenhouse,lever,ashby,workday,... or 'all'")
        p.add_argument("--company", help="substring match on company name")
        p.add_argument("--limit", type=int, default=limit)

    p = sub.add_parser("list", help="show tracked roles")
    filters(p)
    p.add_argument("--status", default="new", choices=[*db.STATUSES, "all"])

    p = sub.add_parser("apply", help="open and autofill applications one at a time")
    filters(p, limit=15)
    p.set_defaults(ats=",".join(AUTOFILL_ATS))
    p.add_argument("--id", help="apply to one role by id prefix")
    p.add_argument("--include-later", action="store_true", help="also revisit roles marked later")

    p = sub.add_parser("mark", help="set a role's status by hand")
    p.add_argument("id")
    p.add_argument("status", choices=db.STATUSES)
    p.add_argument("--note")

    sub.add_parser("stats", help="counts by track and status")

    p = sub.add_parser("export", help="write all tracked roles to a CSV")
    p.add_argument("path", nargs="?", default="applications.csv")

    args = ap.parse_args()
    {"fetch": cmd_fetch, "list": cmd_list, "apply": cmd_apply, "mark": cmd_mark,
     "stats": cmd_stats, "export": cmd_export}[args.cmd](args)


if __name__ == "__main__":
    main()
