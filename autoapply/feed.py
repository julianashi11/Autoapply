"""Pulls open internships from the SimplifyJobs community-maintained listings feed."""

import json
import re
import urllib.request
from urllib.parse import urlparse

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
        }
