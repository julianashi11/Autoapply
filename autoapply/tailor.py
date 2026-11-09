"""Tailors a resume to one posting with Claude, and renders it as a one-page PDF that
matches the look of the original (fonts, sizes, margins, links).

Tailoring only reorders, trims and rewords what is already on the resume. It never adds
experience, skills or numbers that aren't there.
"""

import html
import json
import re
import statistics
from collections import Counter
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
degree, metric, tool, language or skill that the resume doesn't already show, except the \
skills listed in <confirmed_skills>, which the student has confirmed they have.
- Add every confirmed skill to the Skills section, grouped with similar ones. Work one \
into an existing bullet only if that bullet's work clearly used it; never invent new \
projects, jobs or bullets for it.
- Keep every number exactly as written.
- It must fit on ONE page at the original's font size. Keep it the same length as the \
original or shorter; if the original is long, cut the least relevant bullets first.

What you may do:
- Reorder sections, entries and bullets so the most relevant ones come first.
- Reword a bullet to use the posting's terminology when it accurately describes the same \
work (resume says "Postgres", posting says "SQL databases" -> "PostgreSQL (SQL)").
- Tighten wording and start bullets with strong verbs. Keep each bullet to one or two \
lines; avoid a bullet whose last line is only a word or two.
- Use standard section titles (Education, Experience, Projects, Leadership, Skills) so \
ATS parsers recognize them.

Keep the original's structure so it lays out the same way:
- Each job, school or project is an entry: `organization` (bold), `role` (job title; for \
a project, its tech stack), `location`, `dates` (shown right-aligned), `subtitle` (an \
italic second line, e.g. the degree and GPA; usually empty for jobs), and `bullets`.
- Labeled lines such as "Relevant Coursework: ...", "Activities: ...", \
"Programming Languages + Technologies: ..." go in the section's `lines`, written as \
"Label: text".
- Sections that are only a bulleted list (e.g. Achievements, Awards) use the section's \
`bullets` with no entries.
- Copy the name and contact items exactly as they appear on the original.

In `changes`, list each change you made in a few words. In `not_shown`, list the skills, \
tools and areas of knowledge the posting asks for that the resume gives no evidence of, \
each as a short name of 1-4 words (e.g. "Azure", "Terraform", "Linux administration", \
"A/B testing"), so the student can confirm the ones they really have. Leave out \
eligibility requirements such as citizenship, clearance, GPA or graduation date."""


class Entry(BaseModel):
    organization: str
    role: str
    location: str
    dates: str
    subtitle: str
    bullets: List[str]


class Section(BaseModel):
    title: str
    entries: List[Entry]
    lines: List[str]    # "Label: text" lines, e.g. "Relevant Coursework: ..."
    bullets: List[str]  # section-level bullet list, e.g. Achievements


class Resume(BaseModel):
    name: str
    contact: List[str]  # phone, email, links, as on the original
    sections: List[Section]
    changes: List[str]
    not_shown: List[str]


# ---------- reading the original PDF ----------

@lru_cache(maxsize=8)
def _pdf_text(path, mtime):
    reader = PdfReader(path)
    text = "\n".join(p.extract_text(extraction_mode="layout") for p in reader.pages)
    text = re.sub(r"[ \t​]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


@lru_cache(maxsize=8)
def _pdf_pages(path, mtime):
    # Count only pages with text: Google Docs exports often end with a blank page.
    return max(1, sum(1 for p in PdfReader(path).pages if (p.extract_text() or "").strip(" \n​")))


def pdf_pages(path):
    path = Path(path)
    return _pdf_pages(str(path), path.stat().st_mtime)


def resume_text(path):
    path = Path(path)
    return _pdf_text(str(path), path.stat().st_mtime)


def _family(fontname):
    """'BAAAAA+PlayfairDisplay-Bold' -> 'Playfair Display'."""
    name = fontname.split("+")[-1].split("-")[0].split(",")[0]
    name = re.sub(r"(PS)?MT$|PS$", "", name)
    name = re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", name)
    return name.strip() or "Georgia"


SYSTEM_FONTS = {"Times New Roman", "Arial", "Helvetica", "Georgia", "Calibri", "Cambria", "Garamond",
                "Verdana", "Tahoma", "Trebuchet MS", "Book Antiqua", "Palatino", "Courier New"}


@lru_cache(maxsize=8)
def _style(path, mtime):
    import pymupdf

    doc = pymupdf.open(path)
    page = doc[0]
    W, H = page.rect.width, page.rect.height
    chars, sizes, spans = Counter(), Counter(), []
    for b in page.get_text("dict")["blocks"]:
        for line in b.get("lines", []):
            for s in line["spans"]:
                t = s["text"].strip(" ​")
                if not t or s["font"].startswith(("Arial", "Symbol")) and t in "●•◦▪":
                    continue
                spans.append(s)
                chars[_family(s["font"])] += len(t)
                sizes[round(s["size"] * 2) / 2] += len(t)
    if not spans:
        return default_style()
    family = chars.most_common(1)[0][0]
    body = sizes.most_common(1)[0][0]
    top = min(spans, key=lambda s: s["bbox"][1])
    name_size = max(s["size"] for s in spans if s["bbox"][1] < top["bbox"][1] + 5)
    bold_caps = [s["size"] for s in spans if s["flags"] & 16 and s["text"].strip().isupper()
                 and len(s["text"].strip()) > 4 and s is not top]
    heading = statistics.median(bold_caps) if bold_caps else body + 0.5
    second = [s for s in spans if top["bbox"][3] - 1 < s["bbox"][1] < top["bbox"][3] + 20]
    contact = statistics.median([s["size"] for s in second]) if second else body
    # Line pitch from consecutive lines of body text.
    ys = sorted({round(s["bbox"][1], 1) for s in spans if abs(s["size"] - body) < 0.3})
    gaps = [b - a for a, b in zip(ys, ys[1:]) if body * 1.05 < b - a < body * 2.2]
    lh = statistics.median(gaps) / body if gaps else 1.4
    xs0 = [s["bbox"][0] for s in spans]
    xs1 = [s["bbox"][2] for s in spans]
    links = {}
    for ln in page.get_links():
        if ln.get("uri"):
            text = page.get_textbox(ln["from"]).strip()
            if text:
                links[text] = ln["uri"]
    colors = Counter(s["color"] for s in spans if s["color"] not in (0,))
    return {
        "family": family,
        "web_font": family not in SYSTEM_FONTS,
        "body": body, "name": round(name_size, 1), "heading": round(heading, 1),
        "contact": round(contact, 1), "lh": round(min(max(lh, 1.15), 1.7), 2),
        "margin_side": round(max(min(min(xs0), W - max(xs1)), 14) / 72, 2),
        "margin_top": round(max(top["bbox"][1] - 4, 14) / 72, 2),
        "link_color": "#%06x" % colors.most_common(1)[0][0] if colors else "#1155cc",
        "links": links,
    }


def detect_style(path):
    """Fonts, sizes, spacing, margins and links of the original resume's first page."""
    path = Path(path)
    try:
        return _style(str(path), path.stat().st_mtime)
    except Exception:
        return default_style()


def default_style():
    return {"family": "Georgia", "web_font": False, "body": 10.0, "name": 14.0, "heading": 10.5,
            "contact": 9.0, "lh": 1.35, "margin_side": 0.5, "margin_top": 0.45,
            "link_color": "#1155cc", "links": {}}


# ---------- tailoring ----------

def _prompt(base_text, job, jd_text, feedback=None, skills=None):
    p = (
        f"<posting>\nCompany: {job['company']}\nRole: {job['title']}\n\n{jd_text}\n</posting>\n\n"
        f"<resume>\n{base_text}\n</resume>\n\n"
    )
    if skills:
        p += f"<confirmed_skills>\n{', '.join(skills)}\n</confirmed_skills>\n\n"
    p += "Tailor the resume to this posting."
    if feedback:
        p += f"\n\n{feedback}"
    return p


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


def tailor_cli(prompt):
    import subprocess
    import tempfile

    cmd = [claude_cli(), "-p", "--output-format", "json", "--no-session-persistence", "--tools", "",
           "--system-prompt", SYSTEM, "--json-schema", json.dumps(Resume.model_json_schema())]
    # Run from an empty folder so no project settings or instructions get pulled in.
    with tempfile.TemporaryDirectory() as tmp:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=600, cwd=tmp)
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


def tailor(api_key, base_text, job, jd_text, feedback=None, skills=None):
    """Uses Claude Code (your Claude login) when it's installed, otherwise the API key.

    `skills`: skills the student confirmed they have that aren't on the resume yet."""
    prompt = _prompt(base_text, job, jd_text, feedback, skills)
    if claude_cli():
        return tailor_cli(prompt)
    if not api_key:
        raise RuntimeError("Install Claude Code (or add an Anthropic API key in Settings) to tailor resumes.")

    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
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


# ---------- rendering ----------

def _e(s):
    return html.escape(s or "")


def _contact_item(item, style):
    text = item.strip()
    url = style["links"].get(text)
    if not url and re.match(r"^(https?://|www\.|[\w.-]+\.(com|io|dev|me|org)/)", text):
        url = text if text.startswith("http") else "https://" + text
    if not url:
        return _e(text)
    return f"<a href='{_e(url)}'>{_e(text)}</a>"


def _label_line(line):
    if ":" in line and len(line.split(":", 1)[0]) <= 45:
        k, v = line.split(":", 1)
        return f"<p class='line'><b>{_e(k)}:</b>{_e(v)}</p>"
    return f"<p class='line'>{_e(line)}</p>"


def _bullets(items):
    # Bullets stay in normal page flow (no absolute positioning), so the PDF's text order
    # matches reading order for ATS parsers.
    return ("<ul>" + "".join(f"<li><span class='b'>\u25cf</span><span class='t'>{_e(b)}</span></li>"
                             for b in items if b.strip()) + "</ul>") if items else ""


FONT_DIR = Path(__file__).resolve().parent.parent / "data" / "fonts"


def font_face_css(family):
    """@font-face rules with static TTFs embedded, so Chrome writes real (extractable) text
    instead of Type3 glyph drawings. Downloads once from Fontsource and caches. Returns '' if
    the font isn't available, in which case the Google Fonts stylesheet is used."""
    import base64
    import urllib.request

    slug = re.sub(r"[^a-z0-9]+", "-", family.lower()).strip("-")
    FONT_DIR.mkdir(parents=True, exist_ok=True)
    rules = []
    for weight, fstyle in (("400", "normal"), ("700", "normal"), ("400", "italic"), ("700", "italic")):
        path = FONT_DIR / f"{slug}-{weight}-{fstyle}.ttf"
        if not path.exists():
            url = f"https://cdn.jsdelivr.net/fontsource/fonts/{slug}@latest/latin-{weight}-{fstyle}.ttf"
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "autoapply"})
                with urllib.request.urlopen(req, timeout=20) as resp:
                    data = resp.read()
                if data[:4] not in (b"\x00\x01\x00\x00", b"true", b"OTTO"):
                    continue
                path.write_bytes(data)
            except Exception:
                continue
        b64 = base64.b64encode(path.read_bytes()).decode()
        rules.append(f"@font-face{{font-family:'{family}';font-weight:{weight};font-style:{fstyle};"
                     f"src:url(data:font/ttf;base64,{b64}) format('truetype')}}")
    return "".join(rules) if len(rules) >= 2 else ""


def render_html(r, style=None):
    """One column, real text, standard headings: easy for ATS parsers, styled like the original."""
    st = style or default_style()
    fam = st["family"]
    font_link = ""
    faces = font_face_css(fam) if st.get("web_font") else ""
    if faces:
        font_link = f"<style>{faces}</style>"
    elif st.get("web_font"):
        q = fam.replace(" ", "+")
        font_link = (f"<link rel='stylesheet' href='https://fonts.googleapis.com/css2?family={q}:"
                     f"ital,wght@0,400;0,700;1,400;1,700&display=block'>")
    css = f"""
@page{{size:letter;margin:0}}
:root{{--fs:{st['body']}pt;--lh:{st['lh']};--mt:{st['margin_top']}in;--ms:{st['margin_side']}in;
  --name:{st['name']}pt;--head:{st['heading']}pt;--contact:{st['contact']}pt;--gap:1}}
*{{box-sizing:border-box}}
html,body{{margin:0;padding:0}}
body{{font-family:'{fam}',Georgia,'Times New Roman',serif;color:#000;-webkit-print-color-adjust:exact}}
.page{{width:8.5in;padding:var(--mt) var(--ms);font-size:var(--fs);line-height:var(--lh)}}
a{{color:{st['link_color']};text-decoration:underline}}
h1{{font-size:var(--name);font-weight:700;text-align:center;margin:0;line-height:1.25}}
.contact{{font-size:var(--contact);text-align:center;margin:0}}
h2{{font-size:var(--head);font-weight:700;text-transform:uppercase;margin:calc(5pt*var(--gap)) 0 calc(2pt*var(--gap));
  padding-bottom:1pt;border-bottom:1px solid #9a9a9a;line-height:1.25;break-after:avoid}}
.entry{{break-inside:avoid}}
.row{{display:flex;justify-content:space-between;align-items:baseline;gap:10pt}}
.row .left{{flex:1 1 auto;min-width:0}}
.row .right{{flex:none;white-space:nowrap;text-align:right}}
.org{{font-weight:700}}
.loc{{font-style:italic}}
.sub{{font-style:italic;margin:0}}
.line{{margin:0;text-wrap:pretty}}
ul{{list-style:none;margin:0;padding:0}}
li{{display:grid;grid-template-columns:0.25in 1fr;padding-left:0.25in}}
li .b{{font-family:Arial,sans-serif;font-size:0.85em}}
li .t{{text-wrap:pretty}}
"""
    out = ["<!doctype html><html><head><meta charset='utf-8'>", font_link, f"<style>{css}</style></head><body><div class='page'>",
           f"<h1>{_e(r['name'])}</h1>",
           f"<p class='contact'>{' | '.join(_contact_item(c, st) for c in r['contact'] if c.strip())}</p>"]
    for sec in r["sections"]:
        out.append(f"<h2>{_e(sec['title'])}</h2>")
        for en in sec.get("entries") or []:
            left = f"<span class='org'>{_e(en.get('organization'))}</span>"
            if en.get("role"):
                left += f" | {_e(en['role'])}"
            if en.get("location"):
                left += f" | <span class='loc'>{_e(en['location'])}</span>"
            out.append(f"<div class='entry'><div class='row'><div class='left'>{left}</div>"
                       f"<div class='right'>{_e(en.get('dates'))}</div></div>")
            if en.get("subtitle"):
                out.append(f"<p class='sub'>{_e(en['subtitle'])}</p>")
            out.append(_bullets(en.get("bullets") or []) + "</div>")
        out += [_label_line(l) for l in sec.get("lines") or [] if l.strip()]
        out.append(_bullets(sec.get("bullets") or []))
    out.append("</div></body></html>")
    return "".join(out)


# Steps tried in order until the page fits: line spacing, then margins, then font size.
def fit_steps(st):
    fs, lh, mt, ms = st["body"], st["lh"], st["margin_top"], st["margin_side"]
    steps = []
    l = lh
    while l >= max(1.2, lh - 0.24) - 1e-9:      # 1. tighten line spacing a little at a time
        steps.append((fs, round(l, 2), mt, ms, 1.0 if l == lh else 0.8))
        l -= 0.02
    lh2 = steps[-1][1]
    for m in (0.4, 0.33, 0.25):                  # 2. smaller margins
        if m < mt:
            steps.append((fs, lh2, m, min(ms, m), 0.6))
    mt2, ms2 = min(mt, 0.25), min(ms, 0.25)
    f = fs                                       # 3. smaller font, down to 8.5pt
    while f - 0.25 >= max(8.5, fs - 1.25):
        f -= 0.25
        steps.append((f, max(1.15, lh2 - 0.03), mt2, ms2, 0.5))
    return steps


FIT_JS = """
async (steps) => {
  await document.fonts.ready;
  const root = document.documentElement.style, page = document.querySelector('.page');
  const limit = 11 * 96 - 2;
  let used = null;
  for (const [fs, lh, mt, ms, gap] of steps) {
    root.setProperty('--fs', fs + 'pt'); root.setProperty('--lh', lh);
    root.setProperty('--mt', mt + 'in'); root.setProperty('--ms', ms + 'in'); root.setProperty('--gap', gap);
    // .page's padding already includes the top and bottom margins
    used = {fs, lh, mt, ms, height: page.getBoundingClientRect().height};
    if (used.height <= limit) return {...used, fits: true, overflow: 0};
  }
  return {...used, fits: false, overflow: used.height - limit};
}
"""


def pdf_name(profile, job):
    me = profile.get("me") or {}
    who = f"{me.get('first_name', '')} {me.get('last_name', '')}".strip() or "Resume"
    co = re.sub(r"[^A-Za-z0-9]+", "", job["company"])[:30]
    return f"{who.replace(' ', '_')}_Resume_{co}.pdf"


def load(job_row):
    return json.loads(job_row["tailored"]) if job_row["tailored"] else None
