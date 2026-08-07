"""Store — the populated target data model + a SQLite mirror.

`output/extracted_data.json` holds the plan's nesting:

    faculties[faculty_code].units[unit_id].{people, process, concrete_topics}

Every record carries its source_id, so updating one source is a clean
per-source replace inside its unit bucket (other sources' data is untouched).
The SQLite mirror is rebuilt from the JSON at the end of a run — simple and
idempotent, easy to query.

The written JSON is a cleaned *public view* (see `_public_view`): internal-only
keys never reach the file — the LLM debug blob (`_llm`) is dropped and the
profile link is exposed as `profile_url` rather than the internal `_profile_url`.
The in-memory structure keeps the internal names so re-merge and the SQLite
build (which read the live data) are unaffected.
"""

from __future__ import annotations

import copy
import json
import re
import sqlite3
from datetime import datetime, timezone

from . import registry
from .spec_engine import PEOPLE_FIELDS

DATA_PATH = registry.OUTPUT_DIR / "extracted_data.json"
SQLITE_PATH = registry.OUTPUT_DIR / "extracted_data.sqlite"
# Raw per-source people (pre-merge) live in a sidecar so the main file stays
# clean; they are only needed to re-merge correctly when re-running a source.
RAW_PEOPLE_PATH = registry.OUTPUT_DIR / "people_raw.json"
RAW_PROCESS_PATH = registry.OUTPUT_DIR / "process_raw.json"
RAW_FACULTY_PROCESS_PATH = registry.OUTPUT_DIR / "faculty_process_raw.json"

_BUCKET = {"people": "people", "process": "process", "topics": "concrete_topics"}
_RAW_KEY = "_people_by_source"
_PROCESS_RAW_KEY = "_process_by_source"


def consolidate_process(raw_by_source: dict) -> list:
    """Collapse a unit's per-source process records into ONE entry per degree
    level: same-degree sources are merged (descriptions concatenated, links
    unioned, all contributing sources recorded). So a unit with a BA page + BA
    PDF + MA page + MA PDF yields one Bachelor and one Master entry."""
    groups: dict[str, list] = {}
    order: list[str] = []
    for sid, recs in raw_by_source.items():
        for r in recs:
            deg = r.get("degree_level") or "Unspecified"
            if deg not in groups:
                groups[deg] = []
                order.append(deg)
            groups[deg].append((sid, r))

    out = []
    for deg in order:
        items = groups[deg]
        descs, links, seen_links, urls, sids, scraped = [], [], set(), [], [], []
        for sid, r in items:
            if sid not in sids:
                sids.append(sid)
            url = r.get("source_url")
            if url and url in urls:
                continue  # same page (e.g. a shared faculty URL) — credit the
                          # source_id but don't duplicate its description/links
            d = (r.get("process_description") or "").strip()
            if d and d not in descs:
                descs.append(d)
            for l in r.get("relevant_links") or []:
                if l.get("url") and l["url"] not in seen_links:
                    seen_links.add(l["url"])
                    links.append(l)
            if url:
                urls.append(url)
            if r.get("scraped_at"):
                scraped.append(r["scraped_at"])
        out.append({
            "degree_level": deg,
            "process_description": "\n\n".join(descs) or None,
            "relevant_links": links,
            "source_url": urls[0] if urls else None,
            "source_urls": urls,
            "source_id": ",".join(sids),
            "source_ids": sids,
            "scraped_at": max(scraped) if scraped else None,
        })
    return out


def _write_json(path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    tmp.replace(path)


def load() -> dict:
    if DATA_PATH.exists():
        with DATA_PATH.open(encoding="utf-8") as fh:
            data = json.load(fh)
    else:
        data = {"generated_at": None, "faculties": {}}
    # rehydrate raw per-source people/process from the sidecars into each unit
    for path, key in ((RAW_PEOPLE_PATH, _RAW_KEY), (RAW_PROCESS_PATH, _PROCESS_RAW_KEY)):
        if not path.exists():
            continue
        with path.open(encoding="utf-8") as fh:
            raw = json.load(fh)
        for fac in data.get("faculties", {}).values():
            for uid, unit in fac.get("units", {}).items():
                if uid in raw:
                    unit[key] = raw[uid]
    # rehydrate faculty-level raw process (keyed by faculty code)
    if RAW_FACULTY_PROCESS_PATH.exists():
        with RAW_FACULTY_PROCESS_PATH.open(encoding="utf-8") as fh:
            fraw = json.load(fh)
        for fcode, fac in data.get("faculties", {}).items():
            if fcode in fraw:
                fac[_PROCESS_RAW_KEY] = fraw[fcode]
    return data


def save(data: dict) -> None:
    data["generated_at"] = datetime.now(timezone.utc).isoformat()
    # Pull the internal raw-per-source maps (people, process) out into their
    # sidecars, then write the main file without them (restoring in-memory after).
    stashed = []  # (unit, key, value)
    for path, key in ((RAW_PEOPLE_PATH, _RAW_KEY), (RAW_PROCESS_PATH, _PROCESS_RAW_KEY)):
        sidecar = {}
        for fac in data.get("faculties", {}).values():
            for uid, unit in fac.get("units", {}).items():
                if key in unit:
                    sidecar[uid] = unit[key]
                    stashed.append((unit, key, unit[key]))
        _write_json(path, sidecar)
    # faculty-level raw process (keyed by faculty code)
    fsidecar = {}
    for fcode, fac in data.get("faculties", {}).items():
        if _PROCESS_RAW_KEY in fac:
            fsidecar[fcode] = fac[_PROCESS_RAW_KEY]
            stashed.append((fac, _PROCESS_RAW_KEY, fac[_PROCESS_RAW_KEY]))
    _write_json(RAW_FACULTY_PROCESS_PATH, fsidecar)
    for obj, key, _ in stashed:
        del obj[key]
    # Write the cleaned public view (never the internal-keyed live structure).
    _write_json(DATA_PATH, _public_view(data))
    for obj, key, value in stashed:
        obj[key] = value


# --- Public view (the cleaned schema written to disk) -----------------------

def _clean_record(rec: dict) -> dict:
    """One output record with internal-only keys removed: drop the `_llm` debug
    blob (and any other `_`-prefixed internal), and expose `_profile_url` as the
    public `profile_url`. Key order is otherwise preserved."""
    out = {}
    for k, v in rec.items():
        if k == "_profile_url":
            out["profile_url"] = v
        elif k.startswith("_"):
            continue
        else:
            out[k] = v
    return out


def _clean_list(records) -> None:
    if isinstance(records, list):
        records[:] = [_clean_record(r) if isinstance(r, dict) else r for r in records]


def _public_view(data: dict) -> dict:
    """A deep copy of the data with every record run through `_clean_record`, so
    the file on disk carries the public schema while the in-memory structure
    keeps its internal keys (needed by re-merge and the SQLite build)."""
    view = copy.deepcopy(data)
    for fac in view.get("faculties", {}).values():
        _clean_list(fac.get("process"))
        for unit in fac.get("units", {}).values():
            for bucket in ("people", "process", "concrete_topics"):
                _clean_list(unit.get(bucket))
            for g in unit.get("groups", {}).values():
                for bucket in ("people", "process", "concrete_topics"):
                    _clean_list(g.get(bucket))
    return view


def _faculty_bucket(data: dict, src: registry.Source) -> dict:
    """The faculty-level record, for sources that describe a whole faculty (e.g.
    the shared Faculty-of-Philosophy thesis process page). Holds a `process`
    list alongside its `units`."""
    fac = data.setdefault("faculties", {}).setdefault(
        src.faculty_code, {"faculty": src.faculty, "units": {}})
    fac.setdefault("process", [])
    return fac


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


def records_for_source(data: dict, src: registry.Source, page_type: str,
                       scope: str | None = None) -> list:
    """Existing stored records for one source (used to diff on PAGE_CHANGED). A
    `scope='faculty'` source is consolidated at the FACULTY level, not under its
    unit, so it must be looked up there — otherwise the diff compares against an
    empty unit bucket and every run shows a spurious `+1 added`."""
    bucket = _BUCKET.get(page_type)
    fac = data.get("faculties", {}).get(src.faculty_code)
    if not bucket or not fac:
        return []
    if scope == "faculty":
        if page_type == "process":  # consolidated → raw per-source map on the faculty
            return fac.get(_PROCESS_RAW_KEY, {}).get(src.source_id, [])
        return [r for r in fac.get(bucket, []) if r.get("source_id") == src.source_id]
    unit = fac.get("units", {}).get(src.unit_id)
    if not unit:
        return []
    if page_type == "people":  # people are stored merged; use the raw per-source
        return unit.get(_RAW_KEY, {}).get(src.source_id, [])
    if page_type == "process":  # process is consolidated; use the raw per-source
        return unit.get(_PROCESS_RAW_KEY, {}).get(src.source_id, [])
    pool = list(unit.get(bucket, []))
    for g in unit.get("groups", {}).values():
        pool += g.get(bucket, [])
    return [r for r in pool if r.get("source_id") == src.source_id]


def upsert_source(data: dict, src: registry.Source, page_type: str, records: list,
                  group: dict | None = None, scope: str | None = None) -> None:
    """Replace this source's records within its unit bucket. People are MERGED
    across sources (one record per professor). Process/topics from a chair
    source are nested under unit.groups.<chair>; unit-level otherwise. With
    scope='faculty', the records are stored at the faculty level instead of a
    unit (e.g. a faculty-wide thesis-process page shared by several units)."""
    bucket = _BUCKET.get(page_type)
    if not bucket:
        return
    if scope == "faculty":
        fac = _faculty_bucket(data, src)
        if page_type == "process":
            # Consolidate per degree (dedups identical descriptions/URLs), so a
            # faculty-wide page referenced by several units is one entry crediting
            # all of them. Kept re-run safe via a raw-per-source map.
            raw = fac.get(_PROCESS_RAW_KEY)
            if raw is None:
                raw = {}
                for r in fac.get("process", []):
                    raw.setdefault(r.get("source_id"), []).append(r)
                fac[_PROCESS_RAW_KEY] = raw
            raw[src.source_id] = records
            fac["process"] = consolidate_process(raw)
        else:
            fac[bucket] = [r for r in fac.get(bucket, [])
                           if r.get("source_id") != src.source_id] + records
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
    elif page_type == "process":
        # Unit-level process: keep raw per-source (sidecar) and derive ONE entry
        # per degree. Seed the raw map from any pre-consolidation process on
        # first touch so other sources' process isn't lost.
        raw = unit.get(_PROCESS_RAW_KEY)
        if raw is None:
            raw = {}
            for r in unit.get("process", []):
                raw.setdefault(r.get("source_id"), []).append(r)
            unit[_PROCESS_RAW_KEY] = raw
        raw[src.source_id] = records
        unit["process"] = consolidate_process(raw)
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
    profile_url TEXT, scraped_at TEXT);
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
               "bio", "personal_website", "profile_url", "scraped_at"],
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
            # faculty-level process (unit_id NULL) — e.g. a shared faculty page
            for rec in fac.get("process", []):
                row = [rec.get(c) if c not in ("faculty_code", "unit_id", "relevant_links")
                       else (fcode if c == "faculty_code"
                             else None if c == "unit_id"
                             else json.dumps(rec.get(c) or [], ensure_ascii=False))
                       for c in _COLS["process"]]
                conn.execute(f"INSERT INTO process VALUES ({','.join('?' * len(_COLS['process']))})", row)
                n += 1
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
                            elif c == "profile_url":       # live: _profile_url;
                                # reloaded from the cleaned file: profile_url
                                row.append(rec.get("_profile_url") or rec.get("profile_url"))
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
