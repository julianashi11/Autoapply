"""Fetches a posting's job description from the ATS's public job-board API."""

import html
import json
import re
import urllib.request
from urllib.parse import urlparse

UA = {"User-Agent": "autoapply/0.1"}


def _get_json(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def html_to_text(s):
    s = html.unescape(s or "")
    s = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>|</div>", "\n", s)
    s = re.sub(r"(?i)<li[^>]*>", "• ", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\xa0]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n\n", s).strip()


def fetch(url, ats):
    """Returns the description text, or None if this ATS isn't supported."""
    parts = [p for p in urlparse(url).path.split("/") if p]
    if ats == "greenhouse":
        # job-boards.greenhouse.io/<board>/jobs/<id>
        if "jobs" in parts:
            i = parts.index("jobs")
            board, job_id = parts[i - 1], parts[i + 1]
        else:
            m = re.search(r"gh_jid=(\d+)", url)
            board, job_id = (parts[0] if parts else ""), (m.group(1) if m else "")
        data = _get_json(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}")
        return html_to_text(data.get("content"))
    if ats == "lever":
        # jobs.lever.co/<company>/<id>[/apply]
        data = _get_json(f"https://api.lever.co/v0/postings/{parts[0]}/{parts[1]}")
        text = data.get("descriptionPlain") or html_to_text(data.get("description"))
        for lst in data.get("lists") or []:
            text += f"\n\n{lst.get('text', '')}\n" + html_to_text(lst.get("content"))
        return text + "\n\n" + (data.get("additionalPlain") or "")
    if ats == "ashby":
        # jobs.ashbyhq.com/<org>/<id>[/application]
        org, job_id = parts[0], parts[1]
        data = _get_json(f"https://api.ashbyhq.com/posting-api/job-board/{org}")
        for job in data.get("jobs", []):
            if job.get("id") == job_id:
                return job.get("descriptionPlain") or html_to_text(job.get("descriptionHtml"))
        return None
    return None
