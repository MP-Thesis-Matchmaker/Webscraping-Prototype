"""Store — the populated target data model + a SQLite mirror.

`output/theses.json` holds the plan's nesting:

    faculties[faculty_code].units[unit_id].{people, process, concrete_topics}

Every record carries its source_id, so updating one source is a clean
per-source replace inside its unit bucket (other sources' data is untouched).
The SQLite mirror is rebuilt from the JSON at the end of a run — simple and
idempotent, easy to query.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone

from . import registry
from .spec_engine import PEOPLE_FIELDS

THESES_PATH = registry.OUTPUT_DIR / "theses.json"
SQLITE_PATH = registry.OUTPUT_DIR / "theses.sqlite"
# Raw per-source people (pre-merge) live in a sidecar so theses.json stays clean;
# they are only needed to re-merge correctly when re-running a single source.
RAW_PEOPLE_PATH = registry.OUTPUT_DIR / "people_raw.json"

_BUCKET = {"people": "people", "process": "process", "topics": "concrete_topics"}
_RAW_KEY = "_people_by_source"


def _write_json(path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    tmp.replace(path)


def load() -> dict:
    if THESES_PATH.exists():
        with THESES_PATH.open(encoding="utf-8") as fh:
            data = json.load(fh)
    else:
        data = {"generated_at": None, "faculties": {}}
    # rehydrate raw per-source people from the sidecar into each unit
    raw = {}
    if RAW_PEOPLE_PATH.exists():
        with RAW_PEOPLE_PATH.open(encoding="utf-8") as fh:
            raw = json.load(fh)
    for fac in data.get("faculties", {}).values():
        for uid, unit in fac.get("units", {}).items():
            if uid in raw:
                unit[_RAW_KEY] = raw[uid]
    return data


def save(data: dict) -> None:
    data["generated_at"] = datetime.now(timezone.utc).isoformat()
    # Pull the internal raw-people maps out into the sidecar, then write
    # theses.json without them (restoring in-memory afterwards).
    sidecar, stashed = {}, []
    for fac in data.get("faculties", {}).values():
        for uid, unit in fac.get("units", {}).items():
            if _RAW_KEY in unit:
                sidecar[uid] = unit[_RAW_KEY]
                stashed.append((unit, unit[_RAW_KEY]))
    _write_json(RAW_PEOPLE_PATH, sidecar)
    for unit, _ in stashed:
        del unit[_RAW_KEY]
    _write_json(THESES_PATH, data)
    for unit, value in stashed:
        unit[_RAW_KEY] = value


def _unit_bucket(data: dict, src: registry.Source):
    fac = data.setdefault("faculties", {}).setdefault(
        src.faculty_code, {"faculty": src.faculty, "units": {}})
    unit = fac["units"].setdefault(
        src.unit_id, {"unit": src.unit, "unit_homepage": None,
                      "people": [], "process": [], "concrete_topics": []})
    return unit


def _group_bucket(unit: dict, group: dict) -> dict:
    """The unit.groups.<id> record for a chair, created/updated as needed."""
    g = unit.setdefault("groups", {}).setdefault(group["id"], {
        "name": group.get("name"), "full_name": group.get("full_name"),
        "homepage": group.get("homepage"), "source_ids": [],
        "process": [], "concrete_topics": []})
    g["name"] = group.get("name", g.get("name"))
    if group.get("full_name"):
        g["full_name"] = group["full_name"]
    if group.get("homepage"):
        g["homepage"] = group["homepage"]
    return g


def records_for_source(data: dict, src: registry.Source, page_type: str) -> list:
    """Existing stored records for one source (used to diff on PAGE_CHANGED)."""
    bucket = _BUCKET.get(page_type)
    fac = data.get("faculties", {}).get(src.faculty_code)
    if not bucket or not fac:
        return []
    unit = fac.get("units", {}).get(src.unit_id)
    if not unit:
        return []
    if page_type == "people":  # people are stored merged; use the raw per-source
        return unit.get("_people_by_source", {}).get(src.source_id, [])
    pool = list(unit.get(bucket, []))
    for g in unit.get("groups", {}).values():
        pool += g.get(bucket, [])
    return [r for r in pool if r.get("source_id") == src.source_id]


def upsert_source(data: dict, src: registry.Source, page_type: str, records: list,
                  group: dict | None = None) -> None:
    """Replace this source's records within its unit bucket. People are MERGED
    across sources (one record per professor). Process/topics from a chair
    source are nested under unit.groups.<chair>; unit-level otherwise."""
    bucket = _BUCKET.get(page_type)
    if not bucket:
        return
    unit = _unit_bucket(data, src)
    if page_type == "people":
        if group:  # a chair whose thesis offering is its people (e.g. SEAL)
            for r in records:
                r["group_id"] = group["id"]
                r["group_name"] = group.get("name")
            g = _group_bucket(unit, group)
            g["people"] = [r for r in g.get("people", [])
                           if r.get("source_id") != src.source_id] + records
            if src.source_id not in g["source_ids"]:
                g["source_ids"].append(src.source_id)
        else:  # central directory: merge across the unit's people sources
            raw = unit.setdefault(_RAW_KEY, {})
            raw[src.source_id] = records
            unit["people"] = merge_people(raw)
        return

    # Drop this source's records from the unit-level bucket (also migrates a
    # source that has just moved into a group).
    unit[bucket] = [r for r in unit[bucket] if r.get("source_id") != src.source_id]
    if group:
        g = _group_bucket(unit, group)
        for r in records:
            r["group_id"] = group["id"]
            r["group_name"] = group.get("name")
        g[bucket] = [r for r in g.get(bucket, []) if r.get("source_id") != src.source_id] + records
        if src.source_id not in g["source_ids"]:
            g["source_ids"].append(src.source_id)
    else:
        unit[bucket] = unit[bucket] + records


# --- People merging (dedup within a unit) ----------------------------------

def _identity(rec: dict) -> str:
    return re.sub(r"\s+", " ", (rec.get("name") or "")).strip().lower()


def _pick(field: str, values: list):
    """Choose the best value for a field among a person's per-source values."""
    if field == "personal_website":  # prefer an external homepage over a uzh page
        ext = [v for v in values if isinstance(v, str) and "uzh.ch" not in v]
        return ext[0] if ext else values[0]
    if all(isinstance(v, str) for v in values):
        return max(values, key=len)  # longest = most informative (role, bio, ...)
    return values[0]


def merge_people(raw_by_source: dict) -> list:
    """Merge people across a unit's sources by name. Non-null fields are
    combined (see _pick); the record records every contributing source_id."""
    groups: dict[str, list] = {}
    order: list[str] = []
    for sid, recs in raw_by_source.items():
        for rec in recs:
            key = _identity(rec) or f"__anon::{sid}::{len(order)}"
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append((sid, rec))

    merged = []
    for key in order:
        items = groups[key]
        names = []  # union of field names, known fields first
        for f in list(PEOPLE_FIELDS) + [k for _, r in items for k in r]:
            if f not in names and not f.startswith("scraped") and f != "source_id":
                names.append(f)
        rec = {}
        for f in names:
            vals = [r.get(f) for _, r in items if r.get(f) not in (None, "", [])]
            rec[f] = _pick(f, vals) if vals else None
        rec["source_ids"] = list(dict.fromkeys(sid for sid, _ in items))
        rec["source_id"] = ",".join(rec["source_ids"])
        rec["scraped_at"] = max((r.get("scraped_at") for _, r in items
                                 if r.get("scraped_at")), default=None)
        merged.append(rec)

    # Uniform shape: every merged person carries the same key set (the union of
    # all fields seen in this unit, null-filled), known fields first.
    field_order = list(PEOPLE_FIELDS)
    for m in merged:
        for k in m:
            if k not in field_order and k not in ("source_ids", "source_id", "scraped_at"):
                field_order.append(k)
    uniform = []
    for m in merged:
        rec = {f: m.get(f) for f in field_order}
        rec["source_ids"] = m["source_ids"]
        rec["source_id"] = m["source_id"]
        rec["scraped_at"] = m["scraped_at"]
        uniform.append(rec)
    return uniform


# --- SQLite mirror ----------------------------------------------------------

_SCHEMA = """
CREATE TABLE people (source_id TEXT, faculty_code TEXT, unit_id TEXT,
    group_id TEXT, group_name TEXT, role TEXT, name TEXT, email TEXT,
    research_interest TEXT, research_field TEXT, bio TEXT, personal_website TEXT,
    scraped_at TEXT);
CREATE TABLE process (source_id TEXT, faculty_code TEXT, unit_id TEXT,
    group_id TEXT, group_name TEXT, degree_level TEXT, process_description TEXT,
    relevant_links TEXT, source_url TEXT, scraped_at TEXT);
CREATE TABLE concrete_topics (topic_id TEXT, source_id TEXT, faculty_code TEXT,
    unit_id TEXT, group_id TEXT, group_name TEXT, title TEXT, status TEXT,
    degree_level TEXT, date_of_listing TEXT, research_area TEXT,
    supervisors TEXT, topic_description TEXT, source_link TEXT, scraped_at TEXT);
"""

_COLS = {
    "people": ["source_id", "faculty_code", "unit_id", "group_id", "group_name",
               "role", "name", "email", "research_interest", "research_field",
               "bio", "personal_website", "scraped_at"],
    "process": ["source_id", "faculty_code", "unit_id", "group_id", "group_name",
                "degree_level", "process_description", "relevant_links",
                "source_url", "scraped_at"],
    "concrete_topics": ["topic_id", "source_id", "faculty_code", "unit_id",
                        "group_id", "group_name", "title", "status", "degree_level",
                        "date_of_listing", "research_area", "supervisors",
                        "topic_description", "source_link", "scraped_at"],
}


def rebuild_sqlite(data: dict) -> int:
    if SQLITE_PATH.exists():
        SQLITE_PATH.unlink()
    conn = sqlite3.connect(SQLITE_PATH)
    try:
        conn.executescript(_SCHEMA)
        n = 0
        for fcode, fac in data.get("faculties", {}).items():
            for uid, unit in fac.get("units", {}).items():
                # process/topics come from the unit level AND from each chair
                # group; people are unit-level only.
                pools = {t: list(unit.get(t, [])) for t in
                         ("people", "process", "concrete_topics")}
                for g in unit.get("groups", {}).values():
                    for t in ("process", "concrete_topics", "people"):
                        pools[t] += g.get(t, [])
                for table in ("people", "process", "concrete_topics"):
                    for rec in pools[table]:
                        row = []
                        for c in _COLS[table]:
                            if c == "faculty_code":
                                row.append(fcode)
                            elif c == "unit_id":
                                row.append(uid)
                            elif c in ("relevant_links", "supervisors"):
                                row.append(json.dumps(rec.get(c) or [],
                                                      ensure_ascii=False))
                            else:
                                row.append(rec.get(c))
                        ph = ",".join("?" * len(_COLS[table]))
                        conn.execute(f"INSERT INTO {table} VALUES ({ph})", row)
                        n += 1
        conn.commit()
        return n
    finally:
        conn.close()
