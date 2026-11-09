"""Collects open internships: company job boards scanned directly, plus the SimplifyJobs community feed."""

import json
import re
import urllib.request
from urllib.parse import urlparse

from .direct import canon

FEED_URL = (
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/"
    "dev/.github/scripts/listings.json"
)

NO_SPONSOR = {"Does Not Offer Sponsorship", "U.S. Citizenship is Required"}


def ats_for(url):
    host = urlparse(url).netloc.lower()
    if "greenhouse.io" in host:
        return "greenhouse"
    if "lever.co" in host:
        return "lever"
    if "ashbyhq.com" in host:
        return "ashby"
    if "myworkdayjobs.com" in host:
        return "workday"
    if "smartrecruiters.com" in host:
        return "smartrecruiters"
    return "other"


def download():
    req = urllib.request.Request(FEED_URL, headers={"User-Agent": "autoapply/0.1"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def matching(listings, search):
    """Yield normalized jobs that match the profile's search settings."""
    track_of = {}
    for track, cats in search["categories"].items():
        for cat in cats:
            track_of[cat] = track

    exclude_title = re.compile(search.get("exclude_title_regex") or r"$^", re.I)
    loc_re = search.get("locations_regex")
    loc_re = re.compile(loc_re, re.I) if loc_re else None
    excluded_cos = {c.lower() for c in search.get("exclude_companies") or []}

    for x in listings:
        if not (x.get("active") and x.get("is_visible", True)):
            continue
        if search["term"] not in (x.get("terms") or []):
            continue
        track = track_of.get(x.get("category"))
        if not track:
            continue
        if exclude_title.search(x["title"]):
            continue
        if x["company_name"].lower() in excluded_cos:
            continue
        if search.get("needs_sponsorship") and x.get("sponsorship") in NO_SPONSOR:
            continue
        locs = x.get("locations") or []
        if loc_re and not any(loc_re.search(l) for l in locs):
            continue
        yield {
            "id": x["id"],
            "company": x["company_name"],
            "title": x["title"],
            "url": x["url"],
            "locations": locs,
            "category": x["category"],
            "track": track,
            "ats": ats_for(x["url"]),
            "sponsorship": x.get("sponsorship"),
            "date_posted": x.get("date_posted"),
            "canon": canon(x["url"]),
            "source": "simplify",
        }


RELEVANT_CATEGORIES = {"Software", "Software Engineering", "Product", "Product Management", "AI/ML/Data",
                       "Data Science, AI & Machine Learning"}


def collect(search, progress=None):
    """All matching postings: company job boards scanned directly, merged with the community feed.

    Returns (jobs, info)."""
    from . import direct

    listings = download()
    feed_jobs = list(matching(listings, search))
    boards = direct.boards_from_feed(listings, RELEVANT_CATEGORIES)
    for extra in (direct.seed_boards(), direct.parse_extra(search.get("extra_boards"))):
        boards.update({k: v for k, v in extra.items() if k not in boards})
    direct_jobs, failed = direct.scan(boards, search, progress=progress)

    merged = {j["canon"]: j for j in direct_jobs}
    out = []
    for j in feed_jobs:
        d = merged.get(j["canon"]) if j["canon"] else None
        if d:
            d["company"], d["sponsorship"], d["source"] = j["company"], j["sponsorship"], "both"
            if j["date_posted"] and (not d["date_posted"] or j["date_posted"] < d["date_posted"]):
                d["date_posted"] = j["date_posted"]
        else:
            out.append(j)
    out.extend(merged.values())
    info = {"boards": len(boards), "failed": failed, "direct": len(direct_jobs),
            "feed": len(feed_jobs), "total": len(out)}
    return out, info
