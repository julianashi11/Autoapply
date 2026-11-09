"""Scans companies' own job boards (Greenhouse, Lever, Ashby, Workday, SmartRecruiters)
through their public APIs, so new internships show up as soon as the company posts them,
before community lists pick them up.
"""

import json
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

UA = "Mozilla/5.0 (Macintosh) autoapply/0.2"

# ---------- title / location rules ----------

INTERN_RE = re.compile(r"\bintern(ship)?s?\b|\bco-?op\b|\bsummer (analyst|associate|engineer)", re.I)
SWE_RE = re.compile(
    r"software|\bswe\b|developer|back.?end|front.?end|full.?stack|web (dev|eng)|mobile (dev|eng)|"
    r"\bios\b|android|devops|site reliability|\bsre\b|programmer|forward deployed|computer science|"
    r"(platform|infrastructure|cloud|security|data|backend|frontend|product|tools|"
    r"developer productivity|api) eng|technology (engineer|analyst) intern|coding",
    re.I,
)
ML_RE = re.compile(r"machine learning|\bml\b|\bai\b|data (engineer|scien)|research engineer|deep learning|llm", re.I)
PM_RE = re.compile(r"product manag|\bapm\b|associate product|product (intern|owner|strategy)|"
                   r"technical program manag|\btpm\b", re.I)
NOT_SWE_RE = re.compile(
    r"mechanical|electrical|civil|chemical|hardware|manufactur|process eng|quality|structural|"
    r"field eng|sales|mining|petroleum|industrial|construction|asic|\brtl\b|analog|\brf\b|"
    r"test eng|validation|silicon|layout|environmental|biomedical|aerospace|nuclear|facilit|"
    r"marketing|finance|account|legal|recruit|\bhr\b|people|design verification|optical|"
    r"physical design|packaging|thermal|power eng|substation|water|geotech|transportation eng|"
    r"support|help ?desk|technician|road|highway|traffic|bridge|survey|\bfield |molding",
    re.I,
)
OTHER_TERM_RE = re.compile(r"\b(fall|autumn|spring|winter)\b", re.I)
YEAR_RE = re.compile(r"\b20(2\d)\b")

US_STATES = (
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM "
    "NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC"
).split()
US_RE = re.compile(
    r"united states|\busa?\b|u\.s\.|\bremote\b|"
    r"alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|georgia|hawaii|"
    r"idaho|illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|massachusetts|michigan|"
    r"minnesota|mississippi|missouri|montana|nebraska|nevada|new hampshire|new jersey|new mexico|"
    r"new york|north carolina|north dakota|ohio|oklahoma|oregon|pennsylvania|rhode island|"
    r"south carolina|south dakota|tennessee|texas|utah|vermont|virginia|washington|wisconsin|wyoming|"
    r"san francisco|seattle|boston|chicago|austin|nyc|los angeles|bay area|silicon valley|atlanta|denver|"
    r"pittsburgh|st\.? louis|palo alto|mountain view|sunnyvale|san jose|menlo park|redmond|"
    r"(,|\s)(" + "|".join(US_STATES) + r")\b",
    re.I,
)
NON_US_RE = re.compile(
    r"canada|toronto|vancouver|montreal|ontario|london|united kingdom|\buk\b|ireland|dublin|germany|"
    r"berlin|munich|france|paris|india|bengaluru|bangalore|hyderabad|pune|china|shanghai|beijing|"
    r"shenzhen|singapore|japan|tokyo|korea|seoul|israel|tel aviv|poland|warsaw|netherlands|amsterdam|"
    r"spain|madrid|barcelona|mexico|brazil|são paulo|sao paulo|australia|sydney|taiwan|taipei|"
    r"switzerland|zurich|sweden|stockholm|romania|czech|hungary|portugal|lisbon|philippines|vietnam|"
    r"malaysia|indonesia|argentina|colombia|chile|costa rica|emea|apac|latam|hong kong|"
    r"\(h/f\)|\(f/h\)|\(m/w/d\)|\(w/m/d\)|stagiaire|praktikum|werkstudent",
    re.I,
)


def classify(title, tracks):
    """Return 'swe', 'pm' or None for an internship title."""
    if not INTERN_RE.search(title):
        return None
    if "pm" in tracks and PM_RE.search(title):
        return "pm"
    if "swe" in tracks and not NOT_SWE_RE.search(title):
        if SWE_RE.search(title) or (tracks["swe"].get("ml") and ML_RE.search(title)):
            return "swe"
    return None


def right_term(title, posted, term_year, posted_after):
    """True if the title fits the target summer (e.g. 2027)."""
    years = {int("20" + y) for y in YEAR_RE.findall(title)}
    if years and term_year not in years:
        return False
    if OTHER_TERM_RE.search(title) and not re.search(r"summer", title, re.I):
        return False
    if not years and posted and posted < posted_after:
        return False  # undated evergreen posting from before this recruiting cycle
    return True


def is_us(locations, title=""):
    if NON_US_RE.search(title):
        return False
    if not locations:
        return True
    for loc in locations:
        if US_RE.search(loc) and not (NON_US_RE.search(loc) and not re.search(r"united states|\busa?\b", loc, re.I)):
            return True
    return False


# ---------- canonical ids (dedupe across sources) ----------

def canon(url):
    """A source-independent key for a posting, or None."""
    u = urlparse(url)
    host, parts = u.netloc.lower(), [p for p in u.path.split("/") if p]
    q = parse_qs(u.query)
    for k in ("gh_jid", "token"):
        if k in q and q[k][0].isdigit():
            return f"gh:{q[k][0]}"
    if "greenhouse.io" in host and "jobs" in parts:
        i = parts.index("jobs")
        if i + 1 < len(parts) and parts[i + 1].isdigit():
            return f"gh:{parts[i + 1]}"
    if "lever.co" in host and len(parts) >= 2:
        return f"lever:{parts[1]}"
    if "ashbyhq.com" in host and len(parts) >= 2:
        return f"ashby:{parts[1]}"
    if "myworkdayjobs.com" in host and parts:
        m = re.search(r"_([A-Za-z0-9-]+?)(?:-\d+)?$", parts[-1])
        if m:
            return f"wd:{host.split('.')[0]}:{m.group(1).lower()}"
    if "smartrecruiters.com" in host and len(parts) >= 2:
        m = re.match(r"(\d+)", parts[1])
        if m:
            return f"sr:{m.group(1)}"
    return None


# ---------- board list ----------

def boards_from_feed(listings, categories):
    """Every company board that has ever posted a relevant role in the community feed."""
    boards, names = {}, {}
    for x in listings:
        if x.get("category") not in categories:
            continue
        u = urlparse(x["url"])
        host, parts = u.netloc.lower(), [p for p in u.path.split("/") if p]
        key = None
        if "greenhouse.io" in host:
            q = parse_qs(u.query)
            slug = q.get("for", [None])[0] or (parts[0] if parts and parts[0] not in ("embed",) else None)
            key = ("greenhouse", slug) if slug else None
        elif "lever.co" in host and parts:
            key = ("lever", parts[0])
        elif "ashbyhq.com" in host and parts:
            key = ("ashby", parts[0])
        elif "myworkdayjobs.com" in host:
            site = [p for p in parts if not re.match(r"^[a-z]{2}-[A-Z]{2}$", p)]
            if site and site[0] != "job":
                key = ("workday", f"{host}/{site[0]}")
        elif "smartrecruiters.com" in host and parts:
            key = ("smartrecruiters", parts[0])
        if key:
            boards[key] = True
            names[key] = x["company_name"]
    return names


# Well-known tech companies whose boards may never appear in the community feed.
SEED_BOARDS = {
    "greenhouse": "stripe airbnb databricks coinbase discord reddit pinterest dropbox lyft instacart roblox "
                  "datadog mongodb asana duolingo brex gusto chime samsara squarespace okta elastic gitlab "
                  "airtable nuro waymo jumptrading akunacapital optiver flexport webflow sofi toast justworks "
                  "abnormalsecurity applovin pagerduty newrelic calendly intercom kalshi peloton block",
    "ashby": "openai linear perplexity cursor runway supabase posthog cognition elevenlabs character pinecone "
             "deel plaid watershed",
    "lever": "outreach rigetti",
}


def seed_boards():
    return {(ats, slug): None for ats, slugs in SEED_BOARDS.items() for slug in slugs.split()}


def parse_extra(entries):
    """profile entries like 'greenhouse:stripe' or 'workday:nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite'."""
    out = {}
    for e in entries or []:
        ats, _, slug = e.partition(":")
        if ats and slug:
            out[(ats.strip().lower(), slug.strip())] = None
    return out


# ---------- per-ATS fetchers ----------

def _get(url, data=None, timeout=20, retries=3):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers={
        "User-Agent": UA, "Accept": "application/json", "Content-Type": "application/json"})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code not in (429, 503) or attempt == retries:
                raise
            wait = e.headers.get("Retry-After")
            time.sleep(float(wait) if wait and wait.isdigit() else 2 * 2 ** attempt)


def _iso(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def fetch_greenhouse(slug):
    data = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    for j in data.get("jobs", []):
        yield {
            "title": j["title"], "company": j.get("company_name"),
            "url": f"https://job-boards.greenhouse.io/{slug}/jobs/{j['id']}",
            "canon": f"gh:{j['id']}", "ats": "greenhouse",
            "locations": [j.get("location", {}).get("name") or ""],
            "posted": _iso(j.get("first_published") or j.get("updated_at") or ""),
        }


def fetch_lever(slug):
    data = _get(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    for j in data:
        cats = j.get("categories") or {}
        yield {
            "title": j["text"], "company": None,
            "url": j.get("hostedUrl"), "canon": f"lever:{j['id']}", "ats": "lever",
            "locations": cats.get("allLocations") or [cats.get("location") or ""],
            "posted": (j.get("createdAt") or 0) / 1000 or None,
        }


def fetch_ashby(slug):
    data = _get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        locs = [j.get("location") or ""] + [s.get("location", "") for s in j.get("secondaryLocations") or []]
        country = (((j.get("address") or {}).get("postalAddress") or {}).get("addressCountry") or "")
        if country and locs[0]:
            locs[0] = f"{locs[0]}, {country}"
        yield {
            "title": j["title"], "company": None,
            "url": j.get("jobUrl"), "canon": f"ashby:{j['id']}", "ats": "ashby",
            "locations": locs, "posted": _iso(j.get("publishedAt") or ""),
        }


def _workday_age(text):
    t = (text or "").lower()
    if "today" in t:
        return 0
    if "yesterday" in t:
        return 1
    m = re.search(r"(\d+)\+? days", t)
    return int(m.group(1)) if m else None


def fetch_workday(slug, max_pages=5):
    host, site = slug.split("/", 1)
    tenant = host.split(".")[0]
    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    now = time.time()
    for page in range(max_pages):
        data = _get(api, {"appliedFacets": {}, "limit": 20, "offset": page * 20, "searchText": "intern"})
        posts = data.get("jobPostings") or []
        if not posts:
            break
        hits = 0
        for j in posts:
            if not INTERN_RE.search(j.get("title", "")):
                continue
            hits += 1
            path = j.get("externalPath", "")
            url = f"https://{host}/en-US/{site}{path}"
            age = _workday_age(j.get("postedOn"))
            yield {
                "title": j["title"], "company": None, "url": url, "canon": canon(url),
                "ats": "workday", "locations": [j.get("locationsText") or ""],
                "posted": now - age * 86400 if age is not None else None,
            }
        if hits == 0 or len(posts) < 20:
            break


def fetch_smartrecruiters(slug):
    data = _get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?q=intern&limit=100")
    for j in data.get("content", []):
        loc = j.get("location") or {}
        where = ", ".join(x for x in (loc.get("city"), loc.get("region"), (loc.get("country") or "").upper()) if x)
        if (loc.get("country") or "").lower() == "us":
            where += ", United States"
        yield {
            "title": j["name"], "company": (j.get("company") or {}).get("name"),
            "url": f"https://jobs.smartrecruiters.com/{slug}/{j['id']}", "canon": f"sr:{j['id']}",
            "ats": "smartrecruiters", "locations": [where],
            "posted": _iso(j.get("releasedDate") or ""),
        }


FETCHERS = {
    "greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby,
    "workday": fetch_workday, "smartrecruiters": fetch_smartrecruiters,
}


# ---------- scan ----------

def scan(boards, search, workers=24, workday_workers=4, progress=None):
    """Scan every board. Returns (jobs, number of boards that couldn't be read)."""
    term_year = int(re.search(r"20\d\d", search["term"]).group(0))
    posted_after = datetime.fromisoformat(
        str(search.get("direct_posted_after") or f"{term_year - 1}-06-01")
    ).replace(tzinfo=timezone.utc).timestamp()
    cats = search["categories"]
    tracks = {}
    if any(c in cats.get("swe", []) for c in ("Software", "Software Engineering")):
        tracks["swe"] = {"ml": "AI/ML/Data" in cats.get("swe", [])}
    if cats.get("pm"):
        tracks["pm"] = {}
    exclude_title = re.compile(search.get("exclude_title_regex") or r"$^", re.I)
    excluded_cos = {c.lower() for c in search.get("exclude_companies") or []}
    loc_re = search.get("locations_regex")
    loc_re = re.compile(loc_re, re.I) if loc_re else None
    us_only = search.get("us_only", True)

    jobs, failed, done = [], 0, 0

    def one(key):
        ats, slug = key
        try:
            return key, list(FETCHERS[ats](slug))
        except Exception:
            return key, None  # board removed, renamed or rate-limited

    # Workday rate-limits across all its customers' sites, so it gets its own small pool.
    with ThreadPoolExecutor(max_workers=workers) as ex, ThreadPoolExecutor(max_workers=workday_workers) as wd:
        futs = [(wd if k[0] == "workday" else ex).submit(one, k) for k in boards if k[0] in FETCHERS]
        for fut in as_completed(futs):
            done += 1
            if progress and done % 100 == 0:
                progress(done, len(futs))
            key, posts = fut.result()
            if posts is None:
                failed += 1
                continue
            name = boards.get(key)
            for p in posts:
                if not p.get("url") or not p.get("canon"):
                    continue
                track = classify(p["title"], tracks)
                if not track or exclude_title.search(p["title"]):
                    continue
                if not right_term(p["title"], p["posted"], term_year, posted_after):
                    continue
                locs = [l for l in p["locations"] if l]
                if us_only and not is_us(locs, p["title"]):
                    continue
                if loc_re and not any(loc_re.search(l) for l in locs):
                    continue
                company = name or p["company"] or key[1].split("/")[-1].replace("-", " ").title()
                if company.lower() in excluded_cos:
                    continue
                jobs.append({
                    "id": p["canon"], "canon": p["canon"], "company": company, "title": p["title"],
                    "url": p["url"], "locations": locs, "category": "direct", "track": track,
                    "ats": p["ats"], "sponsorship": None,
                    "date_posted": int(p["posted"]) if p["posted"] else None, "source": "direct",
                })
    return jobs, failed
