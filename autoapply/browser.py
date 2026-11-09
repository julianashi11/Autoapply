"""Owns Playwright on a single thread (its sync API isn't thread-safe) and runs jobs for the web server."""

import queue
import threading
from concurrent.futures import Future
from pathlib import Path

from .fill import application_url, fill_page, looks_closed

ROOT = Path(__file__).resolve().parent.parent
BROWSER_DIR = ROOT / "data" / "browser"


class BrowserWorker:
    def __init__(self):
        self._q = queue.Queue()
        self._pw = None
        self._ctx = None      # visible Chrome, persistent profile (stays logged in)
        self._headless = None  # for PDF rendering and page text
        threading.Thread(target=self._run, daemon=True, name="browser").start()

    def submit(self, fn, *args):
        fut = Future()
        self._q.put((fn, args, fut))
        return fut

    def _run(self):
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        while True:
            fn, args, fut = self._q.get()
            try:
                fut.set_result(fn(self, *args))
            except Exception as e:  # report to the caller, keep the worker alive
                fut.set_exception(e)

    # --- helpers that run on the worker thread ---

    def _visible(self):
        if self._ctx is not None:
            return self._ctx
        self._ctx = self._pw.chromium.launch_persistent_context(
            str(BROWSER_DIR), headless=False, viewport=None, args=["--start-maximized"]
        )
        self._ctx.on("close", lambda _: setattr(self, "_ctx", None))
        return self._ctx

    def _bg(self):
        if self._headless is None or not self._headless.is_connected():
            self._headless = self._pw.chromium.launch()
        return self._headless


def autofill(w, job, profile, resume):
    ctx = w._visible()
    page = ctx.new_page()
    page.goto(application_url(job["url"], job["ats"]), wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(2500)
    page.bring_to_front()
    closed = looks_closed(page)
    if job["ats"] in ("greenhouse", "lever", "ashby"):
        report = fill_page(page, profile, resume)
    else:
        report = {"filled": [], "unfilled": [], "errors": [],
                  "note": "No autofill for this site yet; the page is open for you to fill in."}
    report["closed"] = closed
    report["resume"] = resume.name if resume and "Resume" in report["filled"] else None
    return report


def render_pdf(w, html, out_path, steps):
    """Renders the resume, shrinking spacing/margins/font step by step until it fits one page."""
    from .tailor import FIT_JS

    page = w._bg().new_page()
    try:
        page.set_content(html, wait_until="networkidle", timeout=30000)
        fit = page.evaluate(FIT_JS, [list(s) for s in steps])
        page.pdf(path=str(out_path), format="Letter", print_background=True,
                 prefer_css_page_size=True, margin={"top": "0", "bottom": "0", "left": "0", "right": "0"})
    finally:
        page.close()
    from pypdf import PdfReader
    fit["pages"] = len(PdfReader(str(out_path)).pages)
    return fit


def page_text(w, url):
    page = w._bg().new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2500)
        return page.locator("body").inner_text(timeout=5000)
    finally:
        page.close()
