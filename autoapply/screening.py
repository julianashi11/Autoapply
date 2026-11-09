"""Estimates how likely a resume is to get through the first screen for one posting.

There is no public formula for this; it combines the things ATS filters and recruiters
check first (knockout requirements, keyword overlap, formatting, timing) into a
transparent score with the reason for every point gained or lost.
"""

import re
import time

from . import keywords

REQ_START = re.compile(
    r"(minimum|basic|required|must[- ]have)\s+qualifications|requirements|what you('ll)? (need|bring)|"
    r"you (have|bring|are)|who you are|qualifications|what we('re)? look(ing)? for",
    re.I,
)
PREF_START = re.compile(r"preferred|nice[- ]to[- ]have|bonus|plus(es)?:|desired|ideal(ly)?", re.I)


def split_requirements(jd):
    """(required text, preferred text) — best-effort from common section headings."""
    m = REQ_START.search(jd)
    if not m:
        return jd, ""
    rest = jd[m.start():]
    p = PREF_START.search(rest, 20)
    return (rest[:p.start()], rest[p.start():]) if p else (rest, "")


def _grad_year(profile):
    m = re.search(r"20\d\d", str((profile.get("me") or {}).get("graduation") or ""))
    return int(m.group(0)) if m else None


def _gpa(profile):
    try:
        return float((profile.get("me") or {}).get("gpa") or "")
    except ValueError:
        return None


def check_grad(jd, grad):
    """Knockout check on graduation timing. Returns (status, note) or None."""
    if not grad:
        return None
    low = jd.lower()
    if re.search(r"rising (sophomore|junior)|first[- ]year|freshm[ae]n|second[- ]year student", low):
        return ("bad", "Aimed at first- or second-year students; you graduate in "
                f"{grad}, so you'd be a rising senior.")
    years = set()
    for m in re.finditer(r"graduat\w*[^.\n]{0,80}", low):
        years |= {int(y) for y in re.findall(r"\b(20[2-3]\d)\b", m.group(0))}
    if re.search(r"(final|last)[- ]year|graduating (in|by) (december|winter|spring|may|june)? ?2027", low):
        years.add(2027)
    if years:
        if grad in years or (min(years) < grad < max(years)):
            return ("good", f"Graduation window includes {grad}.")
        return ("bad", f"Posting asks for graduation in {', '.join(map(str, sorted(years)))}; you graduate in {grad}.")
    return None


def check_degree(jd):
    low = jd.lower()
    if re.search(r"(currently )?(pursuing|enrolled in) (a )?(ph\.?d|master'?s|graduate degree)", low) and \
            not re.search(r"bachelor|undergrad|b\.?s\.?\b", low):
        return ("bad", "Asks for a master's or PhD student.")
    if re.search(r"computer science|computer engineering|software engineering|related (technical )?field|"
                 r"stem|technical (degree|field)|mathematics", low):
        return ("good", "Your Computer Science & Math major fits the degree requirement.")
    return None


def check_gpa(jd, gpa):
    m = re.search(r"(\d\.\d{1,2})\s*(\+|or (higher|above|better))?\s*(cumulative |overall )?gpa|"
                  r"gpa (of |at least |minimum of |above )+(\d\.\d{1,2})|minimum (cumulative )?gpa[^.\n\d]{0,20}(\d\.\d{1,2})",
                  jd, re.I)
    if not m:
        return None
    need = float(m.group(1) or m.group(6) or m.group(8))
    if need >= 4.0 or need < 2.0:
        return None  # a GPA scale ("/4.0"), not a minimum
    if gpa is None:
        return ("warn", f"Asks for a {need} GPA; add yours to profile.yaml to check.")
    if gpa >= need:
        return ("good", f"GPA {gpa} meets the {need} minimum.")
    return ("bad", f"GPA {gpa} is below the {need} minimum.")


def check_authorization(jd, profile):
    low = jd.lower()
    answers = {k.lower(): str(v) for k, v in (profile.get("answers") or {}).items()}
    needs_sponsor = any("sponsor" in k and v.lower().startswith("y") for k, v in answers.items())
    citizen = (profile.get("me") or {}).get("us_citizen")
    if re.search(r"(active|obtain|eligib\w* (for|to obtain)( a)?|requires?( a)?) (u\.?s\.? )?(government )?security clearance|"
                 r"\bitar\b|must (be|qualify as) a u\.?s\.? (person|citizen)|u\.?s\.? citizenship (is )?required|"
                 r"only u\.?s\.? citizens", low):
        if citizen is True:
            return ("good", "Requires U.S. citizenship or person status, which you have.")
        if citizen is False:
            return ("bad", "Requires U.S. citizenship, U.S. person status or clearance eligibility.")
        return ("warn", "Requires U.S. person status or citizenship (ITAR/clearance). Set me.us_citizen in "
                        "profile.yaml so this can be checked.")
    if needs_sponsor and re.search(r"(not|unable to|cannot|won't|will not) (provide |offer )?sponsor", low):
        return ("bad", "Doesn't sponsor visas, and your profile says you need sponsorship.")
    return None


def check_format(resume_text, pages):
    issues = []
    low = resume_text.lower()
    for h in ("education", "experience", "skills"):
        if h not in low:
            issues.append(f"no '{h.title()}' heading")
    if not re.search(r"[\w.+-]+@[\w-]+\.\w+", resume_text):
        issues.append("email not readable")
    if pages and pages > 1:
        issues.append(f"{pages} pages (internship resumes are expected to be 1; tailoring fixes this)")
    if len(resume_text) < 800:
        issues.append("very little readable text (may be an image or unusual layout)")
    if issues:
        return ("warn", "Formatting: " + "; ".join(issues) + ".")
    return ("good", "Parses cleanly: standard headings, readable text, one page.")


def check_timing(posted):
    if not posted:
        return None
    days = (time.time() - posted) / 86400
    if days <= 3:
        return ("good", f"Posted {max(0, round(days))}d ago. Early applicants get reviewed first.")
    if days <= 14:
        return ("neutral", f"Posted {round(days)}d ago.")
    if days <= 30:
        return ("warn", f"Posted {round(days)}d ago; many roles fill on a rolling basis.")
    return ("warn", f"Posted {round(days)}d ago; it may already be in late-stage interviews.")


def estimate(jd, resume_text, profile, job, pages=None):
    """Returns {score, level, factors:[{status, text}], keywords:{...}}."""
    required, preferred = split_requirements(jd)
    hard = lambda ks: set(ks) - keywords.SOFT
    want_all = hard(keywords.found(jd))
    want_req = hard(keywords.found(required))
    if len(want_req) < 3:  # couldn't find a usable requirements section
        want_req = want_all
    want_other = want_all - want_req
    have = hard(keywords.found(resume_text))
    soft_missing = sorted((keywords.found(jd) & keywords.SOFT) - keywords.found(resume_text))
    pref_only = sorted(hard(keywords.found(preferred)) - want_req)
    total = 2 * len(want_req) + len(want_other)
    got = 2 * len(want_req & have) + len(want_other & have)
    coverage = got / total if total else 0.6

    score = 25 + 55 * coverage          # keywords: 25-80
    factors = [{
        "status": "good" if coverage >= 0.7 else "warn" if coverage >= 0.45 else "bad",
        "text": f"Keyword match {round(coverage * 100)}% (required qualifications weighted double)."
                + (f" Missing from requirements: {', '.join(sorted(want_req - have)[:6])}." if want_req - have else ""),
    }]

    checks = [
        check_grad(jd, _grad_year(profile)),
        check_degree(jd),
        check_gpa(jd, _gpa(profile)),
        check_authorization(jd, profile),
        check_format(resume_text, pages),
        check_timing(job.get("date_posted")),
    ]
    weights = {"good": 4, "neutral": 0, "warn": -5, "bad": -30}
    for c in checks:
        if c:
            score += weights[c[0]]
            factors.append({"status": c[0], "text": c[1]})

    knocked_out = any(c and c[0] == "bad" for c in checks)
    score = max(3, min(95, round(score)))
    if knocked_out:
        score = min(score, 15)
    level = "Unlikely" if score < 35 else "Possible" if score < 55 else "Likely" if score < 75 else "Strong"
    if soft_missing:
        factors.append({"status": "neutral", "text": "Posting stresses " + ", ".join(soft_missing).lower()
                        + "; show these through your bullets (screeners read for them, filters rarely do)."})
    return {
        "score": score, "level": level, "factors": factors, "knockout": knocked_out,
        "required_missing": sorted(want_req - have), "preferred_only": pref_only,
    }
