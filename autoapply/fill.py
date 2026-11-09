"""Best-effort form filling for Greenhouse, Lever and Ashby application pages.

It fills fields and uploads the resume. It never clicks Submit: you review
the form in the browser and submit it yourself.
"""

import re
from pathlib import Path
from urllib.parse import urlparse

# JS: the question text for a form control.
LABEL_JS = r"""
el => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  let t = '';
  if (el.id) {
    const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
    if (l) t = l.innerText;
  }
  const lb = el.getAttribute('aria-labelledby');
  if (!t && lb) t = lb.split(/\s+/).map(i => document.getElementById(i)?.innerText || '').join(' ');
  if (!t) t = el.getAttribute('aria-label') || '';
  if (!t) { const l = el.closest('label'); if (l) t = l.innerText; }
  if (!t) {
    let p = el.parentElement;
    for (let i = 0; i < 5 && p && !t; i++, p = p.parentElement) {
      const l = p.querySelector('label, legend, .application-label, [class*="label"]');
      if (l && !l.contains(el)) t = l.innerText;
    }
  }
  if (!t) t = el.placeholder || el.name || '';
  return clean(t);
}
"""

# JS: the question text for a radio group, given the group's name.
RADIO_QUESTION_JS = r"""
name => {
  const rs = [...document.querySelectorAll(`input[type=radio][name="${CSS.escape(name)}"]`)];
  if (!rs.length) return '';
  const fs = rs[0].closest('fieldset');
  if (fs && fs.querySelector('legend')) return fs.querySelector('legend').innerText.trim();
  let p = rs[0].parentElement;
  while (p && !rs.every(r => p.contains(r))) p = p.parentElement;
  for (let i = 0; i < 4 && p; i++, p = p.parentElement) {
    const own = l => rs.some(r => l.contains(r) || (r.id && l.htmlFor === r.id));
    const l = [...p.querySelectorAll('label, legend, .application-label, [class*="label"], [class*="question"]')]
      .find(l => !own(l) && l.innerText.trim());
    if (l) return l.innerText.replace(/\s+/g, ' ').trim();
  }
  return name;
}
"""

REQUIRED_JS = r"""
(el, label) => el.required || el.getAttribute('aria-required') === 'true' || /\*\s*$/.test(label)
"""

OPTION_TEXT_JS = r"""
el => {
  if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) return l.innerText.trim(); }
  const l = el.closest('label');
  return l ? l.innerText.trim() : (el.value || '');
}
"""


def application_url(url, ats):
    """Jump straight to the application form when the ATS puts it on its own page."""
    path = urlparse(url).path.rstrip("/")
    if ats == "ashby" and not path.endswith("/application"):
        return url.split("?")[0].rstrip("/") + "/application"
    if ats == "lever" and not path.endswith("/apply"):
        return url.split("?")[0].rstrip("/") + "/apply"
    return url


def build_rules(profile):
    """Ordered (regex, value) rules: contact fields first, then custom answers."""
    me = profile["me"]
    full = f"{me.get('first_name', '')} {me.get('last_name', '')}".strip()
    rules = [
        (r"first\s*name|given name|preferred (first )?name", me.get("first_name")),
        (r"last\s*name|family name|surname", me.get("last_name")),
        (r"^\s*(full\s*|legal\s*|your\s*)?name\b", full),
        (r"e-?mail", me.get("email")),
        (r"phone|mobile", me.get("phone")),
        (r"linked\s*in", me.get("linkedin")),
        (r"github", me.get("github")),
        (r"website|portfolio|personal (site|url)|other (url|link)", me.get("website") or me.get("github")),
        (r"^\s*(current\s*)?(location|city)\b", me.get("location")),
        (r"current (company|employer)|^\s*(organization|company)\s*\*?$", me.get("school")),
    ]
    rules += list((profile.get("answers") or {}).items())
    out = []
    for rx, v in rules:
        # An answer can list several wordings, best first, e.g. ["I am not a protected veteran", "No"].
        values = [str(x) for x in v if x not in (None, "")] if isinstance(v, list) else ([str(v)] if v not in (None, "") else [])
        if values:
            out.append((re.compile(rx, re.I), values))
    return out


# Follow-ups and fields that should never be guessed.
NEVER_RE = re.compile(r"^\s*(if (yes|so|applicable)|please (explain|specify|describe))|password|accommodation|salary|compensation|signature", re.I)


def answer_for(label, rules):
    label = label.strip()
    if not label or NEVER_RE.search(label):
        return None
    for rx, value in rules:
        if rx.search(label):
            return value
    return None


def pick_option(options, values):
    """Index of the option that best matches the answer, or None.

    `values` is one answer or a list of wordings in order of preference."""
    if isinstance(values, str):
        values = [values]
    texts = [o.strip().lower() for o in options]
    for value in values:
        v = value.strip().lower()
        # Short answers like "No" must match whole words ("No", "No, ...") not "not" or "none".
        word = re.compile(rf"(^|\W){re.escape(v)}(\W|$)")
        for test in (
            lambda t: t == v,
            lambda t: bool(word.match(t)),
            lambda t: bool(word.search(t)) if len(v) <= 3 else v in t,
            lambda t: len(t) > 3 and t in v,
        ):
            for i, t in enumerate(texts):
                if t and t not in ("select...", "select", "--", "please select") and test(t):
                    return i
    return None


def _visible(loc):
    try:
        return loc.is_visible()
    except Exception:
        return False


def fill_frame(frame, rules, resume, report):
    # Resume upload.
    files = frame.locator("input[type=file]")
    target = None
    for i in range(files.count()):
        f = files.nth(i)
        hint = " ".join(
            filter(None, [f.get_attribute("id"), f.get_attribute("name"), f.evaluate(LABEL_JS)])
        ).lower()
        if "cover" in hint:
            continue
        if re.search(r"resume|cv", hint):
            target = f
            break
        target = target or f
    if target is not None and resume:
        try:
            target.set_input_files(str(resume))
            report["filled"].append("Resume")
        except Exception as e:
            report["errors"].append(f"resume upload: {e}")

    # Text inputs, textareas and native selects.
    fields = frame.locator(
        "input:not([type=hidden]):not([type=file]):not([type=radio]):not([type=checkbox])"
        ":not([type=submit]):not([type=button]):not([type=password]):not([role=combobox]), textarea, select"
    )
    for i in range(fields.count()):
        el = fields.nth(i)
        if not _visible(el):
            continue
        label = el.evaluate(LABEL_JS)
        value = answer_for(label, rules)
        tag = el.evaluate("e => e.tagName.toLowerCase()")
        try:
            if tag == "select":
                if el.evaluate("e => e.selectedIndex > 0"):
                    continue
                if value is None:
                    _note_unfilled(el, label, report)
                    continue
                options = el.evaluate("e => [...e.options].map(o => o.text)")
                idx = pick_option(options, value)
                if idx is None:
                    _note_unfilled(el, label, report)
                    continue
                el.select_option(index=idx)
                report["filled"].append(label)
            else:
                if el.input_value():
                    continue
                if value is None:
                    _note_unfilled(el, label, report)
                    continue
                el.fill(value[0])
                report["filled"].append(label)
        except Exception as e:
            report["errors"].append(f"{label}: {e}")

    # Custom dropdowns (e.g. Greenhouse's react-select comboboxes).
    combos = frame.locator("input[role=combobox]")
    for i in range(combos.count()):
        el = combos.nth(i)
        if not _visible(el):
            continue
        label = el.evaluate(LABEL_JS)
        value = answer_for(label, rules)
        # Skip comboboxes that already show a chosen value.
        if el.evaluate(
            "e => !!e.closest('[class*=container], [class*=control]')?.querySelector('[class*=single-value], [class*=singleValue]')"
        ):
            continue
        if value is None:
            _note_unfilled(el, label, report)
            continue
        try:
            el.click()
            frame.wait_for_timeout(500)
            opts = frame.locator("[role=option]")
            texts = [opts.nth(j).inner_text() for j in range(opts.count())]
            idx = pick_option(texts, value) if texts else None
            if idx is None:
                # Searchable lists (schools, cities) only show matches after typing.
                el.fill(value[0])
                frame.wait_for_timeout(700)
                texts = [opts.nth(j).inner_text() for j in range(opts.count())]
                idx = pick_option(texts, value)
            if idx is None and texts and re.search(r"location|city", label, re.I):
                idx = 0
            if idx is None:
                el.fill("")
                el.press("Escape")
                _note_unfilled(el, label, report)
                continue
            opts.nth(idx).click()
            report["filled"].append(label)
        except Exception as e:
            report["errors"].append(f"{label}: {e}")

    # Radio groups.
    radios = frame.locator("input[type=radio]")
    seen = set()
    for i in range(radios.count()):
        name = radios.nth(i).get_attribute("name")
        if not name or name in seen:
            continue
        seen.add(name)
        group = frame.locator(f'input[type=radio][name="{name}"]')
        if any(group.nth(j).is_checked() for j in range(group.count())):
            continue
        question = frame.evaluate(RADIO_QUESTION_JS, name)
        value = answer_for(question, rules)
        if value is None:
            report["unfilled"].append(question)
            continue
        texts = [group.nth(j).evaluate(OPTION_TEXT_JS) for j in range(group.count())]
        idx = pick_option(texts, value)
        if idx is None:
            report["unfilled"].append(question)
            continue
        try:
            radio = group.nth(idx)
            try:
                radio.check(force=True, timeout=3000)
            except Exception:
                # Styled radios: click the input via the DOM instead.
                radio.evaluate("e => e.click()")
            if not radio.is_checked():
                raise RuntimeError("radio did not stay selected")
            report["filled"].append(question)
        except Exception as e:
            report["errors"].append(f"{question}: {e}")


def _note_unfilled(el, label, report):
    try:
        if el.evaluate(REQUIRED_JS, label):
            report["unfilled"].append(label or "(unlabeled field)")
    except Exception:
        pass


def fill_page(page, profile, resume):
    rules = build_rules(profile)
    report = {"filled": [], "unfilled": [], "errors": []}
    for frame in page.frames:
        try:
            if frame.locator("input, textarea, select").count() == 0:
                continue
            fill_frame(frame, rules, resume, report)
        except Exception as e:
            report["errors"].append(f"frame {frame.url[:60]}: {e}")
    filled = set(report["filled"])
    report["filled"] = list(dict.fromkeys(report["filled"]))
    report["unfilled"] = [q for q in dict.fromkeys(report["unfilled"]) if q not in filled]
    return report


CLOSED_RE = re.compile(
    r"no longer (accepting|available|open)|job (is )?not found|position has been filled|"
    r"this job (posting )?(has )?(expired|closed)|page you('re| are) looking for",
    re.I,
)


def looks_closed(page):
    try:
        return bool(CLOSED_RE.search(page.locator("body").inner_text(timeout=3000)[:5000]))
    except Exception:
        return False


def resolve_resume(profile, track, root):
    path = (profile.get("resume") or {}).get(track)
    if not path:
        return None
    # Tolerate paths pasted with their own quotes (e.g. from Finder's "Copy as Pathname").
    p = Path(path.strip().strip("'\"")).expanduser()
    if not p.is_absolute():
        p = root / p
    return p if p.exists() else None
