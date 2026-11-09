"""Tailors a resume to one posting with Claude, and renders it as an ATS-friendly PDF.

Tailoring only reorders, trims and rewords what is already on the resume. It never adds
experience, skills or numbers that aren't there.
"""

import html
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import List

from pydantic import BaseModel
from pypdf import PdfReader

MODEL = "claude-opus-5-5"

SYSTEM = """You tailor a college student's resume to one internship posting so it gets \
through ATS keyword screening and reads well to a recruiter.

Hard rules:
- Use only facts that are in the original resume. Never add an employer, title, date, \
degree, metric, tool, language or skill that the resume doesn't already show.
- Keep every number exactly as written.

What you may do:
- Reorder sections, entries and bullets so the most relevant ones come first.
- Reword a bullet to use the posting's terminology when it accurately describes the same \
work (resume says "Postgres", posting says "SQL databases" -> "PostgreSQL (SQL)").
- Tighten wording and start bullets with strong verbs.
- Drop or shorten the least relevant entries and bullets so it fits on one page \
(about 450-550 words total).
- Use standard section titles (Education, Experience, Projects, Leadership, Skills) so \
ATS parsers recognize them.

In `changes`, list each change you made in a few words. In `not_shown`, list skills or \
requirements the posting asks for that the resume gives no evidence of, so the student \
can add them only if they really have them."""


class Entry(BaseModel):
    organization: str
    role: str
    location: str
    dates: str
    bullets: List[str]


class Section(BaseModel):
    title: str
    entries: List[Entry]
    lines: List[str]  # free-form lines, e.g. "Languages: Python, Java" or coursework


class Resume(BaseModel):
    name: str
    contact: List[str]  # phone, email, links, as on the original
    sections: List[Section]
    changes: List[str]
    not_shown: List[str]


@lru_cache(maxsize=8)
def _pdf_text(path, mtime):
    reader = PdfReader(path)
    text = "\n".join(p.extract_text(extraction_mode="layout") for p in reader.pages)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


@lru_cache(maxsize=8)
def _pdf_pages(path, mtime):
    return len(PdfReader(path).pages)


def pdf_pages(path):
    path = Path(path)
    return _pdf_pages(str(path), path.stat().st_mtime)


def resume_text(path):
    path = Path(path)
    return _pdf_text(str(path), path.stat().st_mtime)


def _prompt(base_text, job, jd_text):
    return (
        f"<posting>\nCompany: {job['company']}\nRole: {job['title']}\n\n{jd_text}\n</posting>\n\n"
        f"<resume>\n{base_text}\n</resume>\n\nTailor the resume to this posting."
    )


def claude_cli():
    """Path to the Claude Code CLI if it's installed (uses your Claude subscription, no API key)."""
    import shutil
    found = shutil.which("claude")
    if found:
        return found
    for p in (Path.home() / ".local/bin/claude", Path("/opt/homebrew/bin/claude"), Path("/usr/local/bin/claude")):
        if p.exists():
            return str(p)
    return None


def tailor_cli(base_text, job, jd_text):
    import subprocess
    import tempfile

    cli = claude_cli()
    cmd = [cli, "-p", "--output-format", "json", "--no-session-persistence", "--tools", "",
           "--system-prompt", SYSTEM, "--json-schema", json.dumps(Resume.model_json_schema())]
    # Run from an empty folder so no project settings or instructions get pulled in.
    with tempfile.TemporaryDirectory() as tmp:
        proc = subprocess.run(cmd, input=_prompt(base_text, job, jd_text), capture_output=True,
                              text=True, timeout=600, cwd=tmp)
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"Claude Code didn't return a result: {(proc.stderr or proc.stdout)[:300]}")
    if out.get("is_error") or not out.get("structured_output"):
        msg = out.get("result") or out.get("subtype") or "unknown error"
        if "login" in str(msg).lower() or "auth" in str(msg).lower():
            msg += " (run `claude` in a terminal once and log in)"
        raise RuntimeError(f"Claude Code couldn't tailor the resume: {str(msg)[:300]}")
    return Resume.model_validate(out["structured_output"]).model_dump()


def tailor(api_key, base_text, job, jd_text):
    """Uses Claude Code (your Claude login) when it's installed, otherwise the API key."""
    if claude_cli():
        return tailor_cli(base_text, job, jd_text)
    if not api_key:
        raise RuntimeError("Install Claude Code (or add an Anthropic API key in Settings) to tailor resumes.")

    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    prompt = _prompt(base_text, job, jd_text)
    response = client.beta.messages.parse(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM,
        output_config={"effort": "medium"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=[{"role": "user", "content": prompt}],
        output_format=Resume,
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined to tailor this resume.")
    if response.stop_reason == "max_tokens" or response.parsed_output is None:
        raise RuntimeError("Tailoring was cut off; try again.")
    return response.parsed_output.model_dump()


def _e(s):
    return html.escape(s or "")


def render_html(r):
    """A plain one-column layout: real text, standard headings, no tables or graphics."""
    out = [
        "<!doctype html><html><head><meta charset='utf-8'><style>",
        "@page{size:letter;margin:0.5in 0.6in}",
        "body{font-family:Calibri,Arial,Helvetica,sans-serif;font-size:10.5pt;line-height:1.28;color:#000;margin:0}",
        "h1{font-size:18pt;margin:0;text-align:center;letter-spacing:.5px}",
        ".contact{text-align:center;font-size:9.5pt;margin:2px 0 6px}",
        "h2{font-size:10.5pt;text-transform:uppercase;border-bottom:1px solid #000;margin:9px 0 3px;padding-bottom:1px}",
        ".row{display:flex;justify-content:space-between;gap:12px}",
        ".org{font-weight:bold}.role{font-style:italic}",
        "ul{margin:1px 0 4px 0;padding-left:16px}li{margin:0}p{margin:1px 0}",
        "</style></head><body>",
        f"<h1>{_e(r['name'])}</h1>",
        f"<div class='contact'>{' | '.join(_e(c) for c in r['contact'])}</div>",
    ]
    for sec in r["sections"]:
        out.append(f"<h2>{_e(sec['title'])}</h2>")
        for en in sec.get("entries") or []:
            left = f"<span class='org'>{_e(en['organization'])}</span>"
            if en.get("role"):
                left += f" | <span class='role'>{_e(en['role'])}</span>"
            right = " | ".join(x for x in (_e(en.get("location")), _e(en.get("dates"))) if x)
            out.append(f"<div class='row'><div>{left}</div><div>{right}</div></div>")
            if en.get("bullets"):
                out.append("<ul>" + "".join(f"<li>{_e(b)}</li>" for b in en["bullets"]) + "</ul>")
        for line in sec.get("lines") or []:
            if ":" in line:
                k, v = line.split(":", 1)
                out.append(f"<p><b>{_e(k)}:</b>{_e(v)}</p>")
            else:
                out.append(f"<p>{_e(line)}</p>")
    out.append("</body></html>")
    return "".join(out)


def pdf_name(profile, job):
    me = profile.get("me") or {}
    who = f"{me.get('first_name', '')} {me.get('last_name', '')}".strip() or "Resume"
    co = re.sub(r"[^A-Za-z0-9]+", "", job["company"])[:30]
    return f"{who.replace(' ', '_')}_Resume_{co}.pdf"


def load(job_row):
    return json.loads(job_row["tailored"]) if job_row["tailored"] else None
