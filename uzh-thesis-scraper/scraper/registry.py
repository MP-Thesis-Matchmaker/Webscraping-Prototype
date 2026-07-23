"""Registry + state.

The *registry* is the immutable, human-authored source of truth
(`registry/scraping_sources.json`): 99 units across 7 faculties, 142 thesis
source URLs. The *state* (`registry/state.json`) is the mutable per-source
bookkeeping — onboarding status and per-run progress — that makes pause/resume
possible. Both live here so every other stage has a single place to ask "what
sources exist and where are they in their lifecycle?".
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

# --- Canonical project paths (every other module imports these) -------------
ROOT = Path(__file__).resolve().parent.parent
REGISTRY_PATH = ROOT / "registry" / "scraping_sources.json"
STATE_PATH = ROOT / "registry" / "state.json"
CACHE_DIR = ROOT / "cache"
CONTRACTS_DIR = ROOT / "contracts"
OUTPUT_DIR = ROOT / "output"
PREVIEW_DIR = OUTPUT_DIR / "preview"
RUNS_DIR = OUTPUT_DIR / "runs"

# Onboarding lifecycle (does a human trust this source's extraction?).
ONBOARD_UNVERIFIED = "unverified"
ONBOARD_VERIFIED = "verified"
ONBOARD_QUARANTINED = "quarantined"

# Per-run progress (where is this source in the current run?).
RUN_PENDING = "pending"
RUN_FETCHED = "fetched"
RUN_EXTRACTED = "extracted"
RUN_DONE = "done"
RUN_FAILED = "failed"

STATE_VERSION = 1


@dataclass(frozen=True)
class Source:
    """One thesis source URL, denormalized with its unit's context."""

    source_id: str
    url: str
    notes: str
    # Denormalized unit context (handy everywhere downstream):
    unit_id: str
    faculty_code: str
    faculty: str
    unit: str
    classification: str
    urls: tuple = ()  # all URLs when a source lists several (url is urls[0])

    @property
    def cache_dir(self) -> Path:
        return CACHE_DIR / self.source_id


# --- Registry (read-only) ---------------------------------------------------

def load_registry() -> dict:
    with REGISTRY_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def iter_sources() -> Iterator[Source]:
    """Flatten the unit tree into denormalized Source records, in file order."""
    data = load_registry()
    for unit in data["units"]:
        for src in unit["thesis_sources"]:
            # A source usually has one "url"; some list several under "urls"
            # (or, in a few registry rows, as a list under "url").
            raw = src.get("url")
            listed = raw if isinstance(raw, list) else None
            urls = tuple(src.get("urls") or listed
                         or ([raw] if raw else []))
            yield Source(
                source_id=src["source_id"],
                url=(raw if isinstance(raw, str) else (urls[0] if urls else "")),
                notes=src.get("notes", ""),
                urls=urls,
                unit_id=unit["unit_id"],
                faculty_code=unit["faculty_code"],
                faculty=unit["faculty"],
                unit=unit["unit"],
                classification=unit["classification"],
            )


def all_sources() -> list[Source]:
    return list(iter_sources())


def sources_by_id() -> dict[str, Source]:
    return {s.source_id: s for s in iter_sources()}


def get_source(source_id: str) -> Source:
    src = sources_by_id().get(source_id)
    if src is None:
        raise KeyError(f"unknown source_id: {source_id!r}")
    return src


# --- State (mutable, on disk) ----------------------------------------------

def _fresh_state() -> dict:
    return {"version": STATE_VERSION, "sources": {}}


def load_state() -> dict:
    """Load state.json, creating a blank one if absent, and ensure every
    registry source has an entry (new sources default to unverified/pending)."""
    if STATE_PATH.exists():
        with STATE_PATH.open(encoding="utf-8") as fh:
            state = json.load(fh)
    else:
        state = _fresh_state()

    state.setdefault("version", STATE_VERSION)
    entries = state.setdefault("sources", {})
    for src in iter_sources():
        entries.setdefault(
            src.source_id,
            {"onboarding": ONBOARD_UNVERIFIED, "run": RUN_PENDING},
        )
    return state


def save_state(state: dict) -> None:
    """Atomically persist state (write to temp, then replace)."""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    tmp.replace(STATE_PATH)


def source_state(state: dict, source_id: str) -> dict:
    return state["sources"].setdefault(
        source_id, {"onboarding": ONBOARD_UNVERIFIED, "run": RUN_PENDING}
    )


def update_source_state(state: dict, source_id: str, **fields) -> dict:
    entry = source_state(state, source_id)
    entry.update(fields)
    return entry
