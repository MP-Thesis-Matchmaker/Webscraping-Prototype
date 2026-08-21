# UZH Thesis Scraper — Implementation Plan

> **Status: all seven build steps complete and verified.** All 37 units / 103
> sources in the current registry are onboarded (`verified`) and run (`done`),
> zero quarantined. Final dataset: **707 concrete topics · 565 people · 57
> process entries → 1,329 rows** in `output/extracted_data.json` + `output/extracted_data.sqlite`
> (35 output units; 3 sources carry `scope: faculty` and consolidate into one
> PhF-level process entry, and 2 of their units have no other source so carry no
> unit-level output). Counts include the records nested under
> `unit.groups.<chair>` — 160 topics, 20 people, 15 process entries — which a flat
> count of `unit.concrete_topics` misses. See [Build order](#build-order) for the
> per-step ledger.

## Context

I'm building a scraper that aggregates open Bachelor/Master thesis information
across the University of Zurich for a thesis-matching tool. The source registry
lives in `registry/scraping_sources.json`. It was regenerated during the build
from a "full structure crawl (visible rows only)" and now holds **37 units
across 7 faculties (WWF 4, PhF 19, RWF 1, TRF 2, MNF 9, MeF 1, VSF 1), 103
sources / 106 URLs** (a few sources bundle two), each with a stable `source_id`,
a classification, and a `notes` field describing what's on the page. (The
original draft targeted a wider 99-unit / 142-URL list, kept under `archive/`;
the visible-rows crawl is the authoritative set.) Every implementation step was
verified against the populated target data model before moving on.

Core philosophy: **humans decide where and what; deterministic templates make
extraction repeatable; cached HTML decouples fetching from scraping; alarms
report drift.** The LLM appears in three controlled places — summarizing process
pages, drafting extraction templates during onboarding, and a run-time fallback
that rescues a source whose deterministic template matched nothing (always
flagged for review) — and sits behind an exchangeable interface. (The original
plan scoped the LLM to the first two; the fallback was added later, see the
mechanisms note below.)

## Pipeline (the six requirements)

### 1. Fetch & cache
Every URL under `thesis_sources` is fetched (requests first, Playwright
chromium fallback when the static fetch looks empty/blocked) and stored:

```
cache/<source_id>/
  page.html          # latest fetched HTML
  meta.json          # fetched_at, http_status, content_sha1, fetch_method
  history/<ts>.html  # previous versions (keep `SCRAPER_CACHE_HISTORY_KEEP`, default 3)
```

All extraction runs read from cache, never from the network. Refetching is its
own command. Content hash changes are how page-change detection works.

### 2. Extraction, routed by page type
Each source gets a `page_type` — `process` | `topics` | `people` — derived from
the registry's classification + notes, confirmed by me during onboarding.

- **process** → LLM summary into the process layer (degree_level,
  process_description, relevant_links, source_url). The LLM sits behind
  `src/posting_scraper/llm.py` exposing one function
  `complete(system, prompt) -> str`, with an OpenAI implementation selected by
  `config.Settings` (`llm_provider` / `llm_model` / `llm_api_key`) — any other
  provider must be pluggable by adding one file, changing no call sites.
- **topics** → deterministic extraction template (`spec.yaml` per source):
  CSS selectors + field mappings, executed by a spec engine. Same input,
  same output, no LLM in routine runs.
- **people** → deterministic template for the people listing (role, name,
  email, links), PLUS: follow each person's outgoing profile link exactly one
  step deep (also cached under `cache/<source_id>/people/<slug>.html`), and
  extract bio / research_interest / research_field / personal_website from the
  profile page with a second template. Only links matching the spec's person
  URL pattern are followed; everything else is recorded or ignored.

### 3. Populate the target data model
```
faculty:
  institute/department:
    people:
      - role, name, email, research_interest, research_field, bio,
        personal_website
    process:
      - degree_level, process_description,
        relevant_links: [{url, description}], source_url
    concrete_topics:
      - degree_level, date_of_listing, research_area, supervisor_name,
        supervisor_email, topic_description, source_link
```

> **Post-build note.** The built topic record is
> `title, status, degree_level, date_of_listing, research_area, supervisors,
> topic_description, source_link` (`spec_engine.TOPIC_FIELDS`): `title` and
> `status` were added, and the two flat supervisor fields became a
> `supervisors: [{name, email}]` list so a topic can carry several. A spec may
> still *declare* a scalar `supervisor_email` (`contracts/ifi--3/spec.yaml`
> does) — `normalize_supervisors` collapses it into the list before storage.

Every record carries `source_id` and `scraped_at` (UTC ISO); concrete topics get
a stable `topic_id` = sha1(source_url + normalized seed), where the seed is the
spec's `id_from` fields joined — `[title]` on most sources, hence "title" in the
original plan — falling back to `topic_description` when a spec sets no
`id_from`. Output:
`output/extracted_data.json` (this nesting) + a SQLite mirror for querying. The
JSON is written as a cleaned public view: internal keys never reach disk (`_llm`
dropped, `_profile_url` exposed as `profile_url`).

### 4. Break detection & notification
`validate.py` classifies every source's result each run:
- `fetch_failed` — network/HTTP error (retry once first)
- `extract_failed` — template matched nothing, and the LLM fallback couldn't
  recover it
- `schema_invalid` — global checks only, no per-source config: required fields
  of the record type present, emails well-formed, links resolvable format. Like
  `extract_failed`, it triggers the LLM fallback (the match was there but bad)
- `llm_fallback` — the deterministic template failed (`extract_failed` or
  `schema_invalid`) but the LLM fallback produced schema-valid records: stored,
  yet flagged for review so the template gets fixed
- `needs_review` — a record's title is implausible (a date, a status word, a
  bare label) and no better candidate was found on the page: the record is
  stored, the source is flagged, but it keeps scraping. Added post-build with
  `title_check.py`; see below.
- `page_changed` — content hash differs from the hash the template was
  verified against. Topics/people: re-extract with the existing template; if
  schema-valid, update the data BUT list the source in the run report with a
  record-level diff (added/removed/modified) for my review. Process pages:
  flag for one LLM re-summarization + my review.

Flag handling splits by kind (a refinement over the original "any flag →
quarantined"): a `page_changed` whose re-extracted records are *identical* is a
cosmetic HTML change and is quieted to `ok` (not flagged); a `page_changed` with
a real record diff updates the data, is listed in the run report for review, but
**keeps the source verified and in the rotation** — its data is good, so a
weekly scraper should keep tracking it. `needs_review` joins it in
`validate.KEEPS_VERIFIED` for the same reason: one questionable title is no
reason to stop refreshing every good record on the page. Only hard failures
(`fetch_failed`,
`extract_failed`, `schema_invalid`) and an `llm_fallback` rescue **quarantine**
the source (excluded from future runs until re-onboarded); their previous good
data stays in the output (never overwrite good data with garbage). The run
report lists every flagged source with reason, `report.py` writes
`output/runs/<timestamp>.json`, and the run exits non-zero so any scheduler
(cron/launchd/GitHub Action) can email me. Notification hook is one function
(`notify(summary)`) — default prints, later swappable for email/webhook.

### 5. Pause & resume
State lives in `var/state.json`: per source — onboarding state
(`unverified | verified | quarantined`) and per-run progress
(`pending | fetched | extracted | done | failed`). Every command is
interruptible (Ctrl+C safe: finish the current source, write state, exit) and
resumable: `fetch --resume`, `run --resume` continue with pending sources only.
The cache layer makes this cheap — completed work is never redone.

### 6. Stepwise verification via the data model
No implementation step is complete until I've seen the populated target data
model it produces. After each step, write the relevant JSON and show me its
content (or excerpt if large):
- spec-engine proof: `output/preview/ifi--3.json` (the DDIS page is source
  `ifi--3`; `ddis` survives only as the `group.id` in its spec)
- each onboarded source: `output/preview/<source_id>.json` (in target nesting)
- each run: updated `output/extracted_data.json` + diff summary vs previous run
  (added / removed / modified records)
Stop after each build step and wait for my go-ahead.

## Onboarding workflow (per source, interactive)

`posting-scraper onboard <source_id>` (and `onboard --next`):
1. Fetch + cache the page.
2. Propose `page_type` from registry classification + notes; I confirm.
3. topics/people → LLM drafts `spec.yaml`, spec engine runs it immediately,
   show me spec + extracted records side by side. process → LLM summary shown.
   For people pages: also show which outgoing links WOULD be followed before
   following them; I approve the pattern first.
4. I approve / edit / retry with a hint / skip.
5. On approval: freeze `snapshot.html` (the cached HTML), `expected.json`,
   record the verified content hash, state → `verified`, write + show
   `output/preview/<source_id>.json`.

## Repository layout

```
pyproject.toml                   # deps, extras, console script, ruff + pytest config
.env.example                     # every setting, with defaults (copy to .env)
src/posting_scraper/{config,registry,fetch,cache,spec_engine,spec_generator,llm,
                     llm_extract,title_check,validate,store,report,main}.py
tests/{test_contracts.py, test_units.py, test_title_check.py, test_config.py,
       replay_util.py, regen_golden.py, golden_contracts.json}
registry/scraping_sources.json   # regenerated during the build (see archive/)
contracts/<source_id>/{spec.yaml, snapshot.html|snapshot.json, expected.json}
cache/<source_id>/...
var/state.json
output/{extracted_data.json, extracted_data.sqlite, *_raw.json, preview/, runs/}
docs/scraper_plan.md             # this file
archive/                         # provenance of the source list; nothing reads it
```

> **Layout note (post-build).** The tree above is the restructured layout: code
> is an installed package under `src/`, and the data dirs sit at the repo root
> with the read-only inputs (`registry/`, `contracts/`) separated from the
> machine-written state (`var/`, `cache/`, `output/`). During the build
> everything lived one level down in `uzh-thesis-scraper/` and `state.json` sat
> in `registry/`.
Stages communicate only via typed dicts/dataclasses; `main.py` reads as ~10
lines of stage calls. `test_contracts.py` replays every spec against its
snapshot with no network and must always pass.

## CLI

```
posting-scraper fetch [--only ID ...] [--resume] [--render]
posting-scraper onboard <source_id> | --next
        [--page-type process|topics|people|none] [--hint TEXT]
        [--refetch] [--redraft] [--no-follow] [--profile-limit N]
        [--llm-title-review] [--yes]
posting-scraper run [--only ID ...] [--resume] [--no-llm-fallback]
posting-scraper status                              # state + last-run table
posting-scraper check <source_id>                   # dry-run one source, diff vs stored
```

The five subcommands are as planned; the flags accumulated during the build.
README §CLI is the canonical reference.

## Build order

1. ✅ Skeleton: registry, state, cache, fetch (+ fetch command with resume)
2. ✅ spec_engine + one hand-written spec for
   https://www.ifi.uzh.ch/en/ddis/theses/topics.html (source `ifi--3`) → proven,
   `output/preview/ifi--3.json`
3. ✅ llm.py abstraction (OpenAI impl) + llm_extract for process pages
4. ✅ onboard command (interactive, as specified) incl. people-page link following
5. ✅ validate + store + report + notify; run command with resume
6. ✅ tests (`tests/test_contracts.py` golden-replay of every spec against its
   snapshot, offline; `tests/test_units.py` for topic_id stability,
   normalization/transforms, link-pattern matching, escalation decision)
7. ✅ status + check commands; README documenting the workflow

> **Note on the two baselines.** Each contract's `expected.json` is the
> *onboarding provenance snapshot* — the full approved records at verification
> time, including enrichment (followed profiles, PDF-parsed supervisors). It is
> human-facing documentation and is not read back at runtime. The contract test
> asserts against `tests/golden_contracts.json`, the engine's deterministic,
> offline-reproducible core extraction, because `expected.json` carries
> non-reproducible enrichment and drifts as specs are edited after freezing.
> Regenerate the golden deliberately with `python tests/regen_golden.py`.

Politeness everywhere: sequential fetching, a 2s delay
(`SCRAPER_POLITE_DELAY_SECONDS`, see the configuration note below), honest
User-Agent. Do not scrape sources beyond the ones I explicitly onboard.

## What the build added beyond the original plan

All 37 units were extracted by *configuration, not new code*: the spec engine
grew a set of reusable mechanisms so each new source needed only a `spec.yaml`.

- **Field transforms** (`spec_engine.py` `_TRANSFORMS`) — composable string
  cleaners: `name_lastfirst` (`"Backhaus, Norman, Prof."` → `"Norman Backhaus"`),
  `name_lastfirst_space`, `pi_surname` (leading `SURNAME_` prefix from a PDF
  link, title-cased), `titlecase`, `academic_role`, `strip_titles`,
  `absolute_url`, `normalize_ws`. **Ordering contract: transforms run BEFORE a
  field's `regex:`** — a match+destroy pattern (e.g. `pi_surname`) must do its
  own regex inside the transform, never rely on a later `regex:`.
- **`sectioned_people`** source_type — bins each person element under its
  nearest preceding heading, keeps only whitelisted sections (e.g. active
  Professors, excluding emeriti/visiting).
- **`pdf_enrich`** — for one-PDF-per-project topic lists: parse each PDF for a
  fuller `topic_description` and the supervisor's name/email
  (`supervisors_from_pdf` supersedes the surname stub; `supervisor_email_any`
  accepts external domains like usz.ch / kispi.uzh.ch). PDFs are cached so
  re-runs don't re-spend.
- **`include_pages`** (process) — stitches a thin hub page together with named
  subpages into one document before the LLM summary.
- **`scope: faculty`** — sources that share one central page (e.g. the PhF
  `module.html`) consolidate into a single faculty-level process entry crediting
  every contributing unit, instead of surfacing as empty units.
- **LLM fallback** (`llm_extract.extract_records_fallback`, validate status
  `llm_fallback`) — a third, opt-out LLM use: when a topics/people template
  fails — matching nothing (`extract_failed`) or only malformed records
  (`schema_invalid`) — a run reads the cached page's cleaned main HTML and asks
  the model for target-model records as JSON, cached by content hash. The result
  is *writable but flagged* (same "store but alert" contract as `page_changed`):
  the run is rescued, but the source is quarantined for review so the template
  gets fixed. A rescue only replaces the original result when it is itself
  schema-valid, so a worse fallback never clobbers the diagnosis. On by default;
  `run --no-llm-fallback` disables it, and it no-ops when no LLM is configured.
- **Title plausibility + repair** (`title_check.py`, validate status
  `needs_review`) — a spec says *where* a title sits, never what one should look
  like, so a selector that keeps matching the wrong element yields a
  wrong-but-well-formed value no structural check can catch (`ifi--5` stored a
  topic titled `"November 3, 2021"`). Each `topics` title is scored; an
  implausible one is *reserved, not discarded*, while the record's own container
  is scanned for a better candidate (`p > strong`, other headings, `strong`/`b`,
  link text, PDF filename stem, description lead). Found ⇒ it becomes the title
  and the rejected string is parked in `date_of_listing` or `_title_rejected`;
  not found ⇒ the original stays and the source is flagged `needs_review`.
  `topic_id` is computed after the repair. Deterministic, no LLM in runs, and
  calibrated against the committed golden corpus rather than intuition — the
  calibration test fails instead of letting a tightened heuristic quietly rewrite
  good titles. Per-spec escape hatches: `title_check: false`,
  `title_candidates:`.
- **Centralized configuration** (`config.py`) — a `pydantic_settings.BaseSettings`
  class replaced config scattered across import-time `os.environ` reads, module
  constants and class attributes. All variables are `SCRAPER_`-prefixed (with
  `OPENAI_API_KEY` still accepted), read through `get_settings()` at call time so
  `SCRAPER_DATA_ROOT` relocates the whole data tree — it previously moved the
  cache but not the output, because `store.py` froze its output paths at import.
  Same shape as `backend-core`'s `config.py`, so the port is a merge. The title
  thresholds are deliberately excluded: they would change stored data, not how it
  is obtained.
- **`_profile_url`** is the standard institutional link on every person;
  `personal_website` is reserved for genuine external homepages (followed via a
  `follow` block when the profile links one).

Accepted limits: supervisor emails behind contact forms or login-gated topic
tools (paleontology mailform, physics team pages, MeF VAM, VSF matool) are left
`null` — not scrapable, by design.

## State-file note

The state file (now `var/state.json`) once carried ~57 orphaned entries from the
earlier 99-unit registry version (e.g. `ivr--1`, `cale--1`, `zkr--1`). It has
since been rebuilt and now holds exactly the 103 entries of the current 37-unit
registry, with no orphans — `status` reports any that reappear.
