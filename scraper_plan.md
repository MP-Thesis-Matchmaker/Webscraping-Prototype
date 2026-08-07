# UZH Thesis Scraper — Implementation Plan

> **Status: all seven build steps complete and verified.** All 37 units / 103
> sources in the current registry are onboarded (`verified`) and run (`done`),
> zero quarantined. Final dataset: **711 concrete topics · 569 people · 58
> process entries → 1,338 rows** in `output/extracted_data.json` + `output/extracted_data.sqlite`
> (35 output units; 2 registry units consolidate into faculty-level process via
> `scope: faculty`). See [Build order](#build-order) for the per-step ledger.

## Context

I'm building a scraper that aggregates open Bachelor/Master thesis information
across the University of Zurich for a thesis-matching tool. The source registry
lives in `registry/scraping_sources.json`. It was regenerated during the build
from a "full structure crawl (visible rows only)" and now holds **37 units
across 7 faculties (WWF 4, PhF 19, RWF 1, TRF 2, MNF 9, MeF 1, VSF 1), 103
source URLs**, each with a stable `source_id`, a classification, and a `notes`
field describing what's on the page. (The original draft targeted a wider
99-unit / 141-URL list; the visible-rows crawl is the authoritative set.) Every
implementation step was verified against the populated target data model before
moving on.

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
  history/<ts>.html  # previous versions (keep last 3)
```

All extraction runs read from cache, never from the network. Refetching is its
own command. Content hash changes are how page-change detection works.

### 2. Extraction, routed by page type
Each source gets a `page_type` — `process` | `topics` | `people` — derived from
the registry's classification + notes, confirmed by me during onboarding.

- **process** → LLM summary into the process layer (degree_level,
  process_description, relevant_links, source_url). The LLM sits behind
  `scraper/llm.py` exposing one function `complete(system, prompt) -> str`,
  with an OpenAI implementation selected via config/env — any other provider
  must be pluggable by adding one file, changing no call sites.
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
Every record carries `source_id` and `scraped_at` (UTC ISO); concrete topics get
a stable `topic_id` = sha1(source_url + normalized title). Output:
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
weekly scraper should keep tracking it. Only hard failures (`fetch_failed`,
`extract_failed`, `schema_invalid`) and an `llm_fallback` rescue **quarantine**
the source (excluded from future runs until re-onboarded); their previous good
data stays in the output (never overwrite good data with garbage). The run
report lists every flagged source with reason, `report.py` writes
`output/runs/<timestamp>.json`, and the run exits non-zero so any scheduler
(cron/launchd/GitHub Action) can email me. Notification hook is one function
(`notify(summary)`) — default prints, later swappable for email/webhook.

### 5. Pause & resume
State lives in `registry/state.json`: per source — onboarding state
(`unverified | verified | quarantined`) and per-run progress
(`pending | fetched | extracted | done | failed`). Every command is
interruptible (Ctrl+C safe: finish the current source, write state, exit) and
resumable: `fetch --resume`, `run --resume` continue with pending sources only.
The cache layer makes this cheap — completed work is never redone.

### 6. Stepwise verification via the data model
No implementation step is complete until I've seen the populated target data
model it produces. After each step, write the relevant JSON and show me its
content (or excerpt if large):
- spec-engine proof: `output/preview/ddis--1.json`
- each onboarded source: `output/preview/<source_id>.json` (in target nesting)
- each run: updated `output/extracted_data.json` + diff summary vs previous run
  (added / removed / modified records)
Stop after each build step and wait for my go-ahead.

## Onboarding workflow (per source, interactive)

`python -m scraper onboard <source_id>` (and `onboard --next`):
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
uzh-thesis-scraper/
  registry/scraping_sources.json   # provided
  registry/state.json
  contracts/<source_id>/{spec.yaml, snapshot.html, expected.json}
  cache/<source_id>/...
  scraper/{registry,fetch,cache,spec_engine,spec_generator,llm,llm_extract,
           validate,store,report,main}.py
  output/{extracted_data.json, extracted_data.sqlite, preview/, runs/}
  tests/{test_contracts.py, test_units.py}
```
Stages communicate only via typed dicts/dataclasses; `main.py` reads as ~10
lines of stage calls. `test_contracts.py` replays every spec against its
snapshot with no network and must always pass.

## CLI

```
python -m scraper fetch [--only ID ...] [--resume]    # stage 1 only
python -m scraper onboard <source_id> | --next        # interactive verification
python -m scraper run [--only ID ...] [--resume]      # extract verified sources from cache
python -m scraper status                              # state + last-run table
python -m scraper check <source_id>                   # dry-run one source, diff vs stored
```

## Build order

1. ✅ Skeleton: registry, state, cache, fetch (+ fetch command with resume)
2. ✅ spec_engine + one hand-written spec for
   https://www.ifi.uzh.ch/en/ddis/theses/topics.html → proven, `output/preview/ddis--1.json`
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

Politeness everywhere: sequential fetching, 2s delay, honest User-Agent. Do not
scrape sources beyond the ones I explicitly onboard.

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
- **`_profile_url`** is the standard institutional link on every person;
  `personal_website` is reserved for genuine external homepages (followed via a
  `follow` block when the profile links one).

Accepted limits: supervisor emails behind contact forms or login-gated topic
tools (paleontology mailform, physics team pages, MeF VAM, VSF matool) are left
`null` — not scrapable, by design.

## State-file note

`registry/state.json` still carries ~57 orphaned entries from the earlier
99-unit registry version (e.g. `ivr--1`, `cale--1`, `zkr--1`). They are not in
the current 37-unit registry and are correctly ignored; they can be pruned for
tidiness without affecting output.
