"""Loads search settings: search.yaml (shared, committed) plus any overrides in profile.yaml."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def load_search(profile=None):
    with open(ROOT / "search.yaml") as f:
        search = yaml.safe_load(f) or {}
    search.update((profile or {}).get("search") or {})
    return search
