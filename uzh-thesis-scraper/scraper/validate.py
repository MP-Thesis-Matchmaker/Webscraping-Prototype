"""Break detection — classify each source's result every run.

Statuses (plan §4):
  OK            — extracted, schema-valid, content unchanged since verification
  PAGE_CHANGED  — schema-valid but the page hash differs from the verified hash
                  (data is still updated, but the source is flagged for review)
  FETCH_FAILED  — no usable cached page / last fetch errored
  EXTRACT_FAILED— template/LLM produced nothing usable
  SCHEMA_INVALID— required fields missing or malformed (emails/links)

Schema checks are global (no per-source config): they encode what a valid
record of each type must look like, nothing page-specific.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

OK = "ok"
PAGE_CHANGED = "page_changed"
FETCH_FAILED = "fetch_failed"
EXTRACT_FAILED = "extract_failed"
SCHEMA_INVALID = "schema_invalid"

FLAGGED = {PAGE_CHANGED, FETCH_FAILED, EXTRACT_FAILED, SCHEMA_INVALID}

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_URL_RE = re.compile(r"^https?://[^\s]+$", re.I)


@dataclass
class Result:
    source_id: str
    status: str
    page_type: str
    reasons: list[str] = field(default_factory=list)
    record_count: int = 0

    @property
    def flagged(self) -> bool:
        return self.status in FLAGGED

    @property
    def writable(self) -> bool:
        """Whether the freshly extracted data is good enough to store. We write
        on OK and on PAGE_CHANGED (still schema-valid); we never overwrite good
        data on a hard failure."""
        return self.status in (OK, PAGE_CHANGED)


def _valid_email(v) -> bool:
    return isinstance(v, str) and bool(_EMAIL_RE.match(v.strip()))


def _valid_url(v) -> bool:
    return isinstance(v, str) and bool(_URL_RE.match(v.strip()))


def _check_people(records) -> list[str]:
    errs = []
    for i, r in enumerate(records):
        if not (r.get("name") or "").strip():
            errs.append(f"people[{i}]: missing name")
        if r.get("email") and not _valid_email(r["email"]):
            errs.append(f"people[{i}]: bad email {r['email']!r}")
        if r.get("personal_website") and not _valid_url(r["personal_website"]):
            errs.append(f"people[{i}]: bad website {r['personal_website']!r}")
    return errs


def _check_topics(records) -> list[str]:
    errs = []
    for i, r in enumerate(records):
        if not (r.get("topic_description") or r.get("research_area") or "").strip():
            errs.append(f"topics[{i}]: no topic_description/research_area")
        if r.get("supervisor_email") and not _valid_email(r["supervisor_email"]):
            errs.append(f"topics[{i}]: bad supervisor_email {r['supervisor_email']!r}")
        if r.get("source_link") and not _valid_url(r["source_link"]):
            errs.append(f"topics[{i}]: bad source_link")
        if not r.get("topic_id"):
            errs.append(f"topics[{i}]: missing topic_id")
    return errs


def _check_process(records) -> list[str]:
    errs = []
    for i, r in enumerate(records):
        if not (r.get("degree_level") or "").strip():
            errs.append(f"process[{i}]: missing degree_level")
        if not _valid_url(r.get("source_url", "")):
            errs.append(f"process[{i}]: bad source_url")
        for j, link in enumerate(r.get("relevant_links") or []):
            if not _valid_url(link.get("url", "")):
                errs.append(f"process[{i}].links[{j}]: bad url")
    return errs


_SCHEMA = {"people": _check_people, "topics": _check_topics, "process": _check_process}


def classify(source_id: str, page_type: str, *, cached: bool, last_status: int,
             current_sha1: str | None, verified_sha1: str | None,
             records: list, llm_ok: bool = True, allow_empty: bool = False) -> Result:
    res = Result(source_id, OK, page_type, record_count=len(records))

    # 1. fetch
    if not cached or not (200 <= (last_status or 0) < 300):
        res.status = FETCH_FAILED
        res.reasons.append(f"no usable cache (last_status={last_status})")
        return res

    # 2. extraction produced something usable. `allow_empty` distinguishes a
    #    genuinely-empty source (e.g. a JSON thesis market with no open topics
    #    right now) from a template that silently matched nothing.
    if page_type in ("people", "topics") and not records and not allow_empty:
        res.status = EXTRACT_FAILED
        res.reasons.append("template matched 0 records")
        return res
    if page_type == "process":
        rec = records[0] if records else {}
        if not llm_ok or not (rec.get("process_description") or "").strip():
            res.status = EXTRACT_FAILED
            res.reasons.append("no usable process summary")
            return res

    # 3. schema
    errs = _SCHEMA.get(page_type, lambda _r: [])(records)
    if errs:
        res.status = SCHEMA_INVALID
        res.reasons = errs[:10]
        return res

    # 4. page change (valid data, but flag for review)
    if verified_sha1 and current_sha1 and current_sha1 != verified_sha1:
        res.status = PAGE_CHANGED
        res.reasons.append(f"hash {verified_sha1[:8]} -> {current_sha1[:8]}")
    return res


# --- record-level diff (for the run report on PAGE_CHANGED) -----------------

def _key_fn(page_type: str):
    if page_type == "topics":
        return lambda r: r.get("topic_id")
    if page_type == "people":
        return lambda r: (r.get("email") or r.get("name"))
    return lambda r: r.get("source_id")  # process: one record per source


def _norm(r: dict) -> dict:
    return {k: v for k, v in r.items() if not k.startswith("scraped") and not k.startswith("_")}


def diff_records(page_type: str, old: list, new: list) -> dict:
    key = _key_fn(page_type)
    old_by = {key(r): r for r in old}
    new_by = {key(r): r for r in new}
    added = [k for k in new_by if k not in old_by]
    removed = [k for k in old_by if k not in new_by]
    modified = [k for k in new_by if k in old_by and _norm(new_by[k]) != _norm(old_by[k])]
    return {"added": len(added), "removed": len(removed), "modified": len(modified),
            "added_keys": [str(k) for k in added][:20],
            "removed_keys": [str(k) for k in removed][:20],
            "modified_keys": [str(k) for k in modified][:20]}
