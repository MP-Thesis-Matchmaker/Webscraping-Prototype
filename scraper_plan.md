# UZH Thesis Scraper — Implementation Plan

## Context

I'm building a scraper that aggregates open Bachelor/Master thesis information
across the University of Zurich for a thesis-matching tool. The source registry
already exists: `scraping_sources.json` (99 units across 7 faculties, 141 source
URLs, each with a stable `source_id`, a classification, and a `notes` field
describing what's on the page). I will verify every implementation step against
the populated target data model before you move on.

Core philosophy: **humans decide where and what; deterministic templates make
extraction repeatable; cached HTML decouples fetching from scraping; alarms
report drift.** The LLM appears in exactly two controlled places — summarizing
process pages and drafting extraction templates during onboarding — and sits
behind an exchangeable interface.

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
`output/theses.json` (this nesting) + a SQLite mirror for querying.

### 4. Break detection & notification
`validate.py` classifies every source's result each run:
- `fetch_failed` — network/HTTP error (retry once first)
- `extract_failed` — template matched nothing / LLM returned unusable output
- `schema_invalid` — global checks only, no per-source config: required fields
  of the record type present, emails well-formed, links resolvable format
- `page_changed` — content hash differs from the hash the template was
  verified against. Topics/people: re-extract with the existing template; if
  schema-valid, update the data BUT list the source in the run report with a
  record-level diff (added/removed/modified) for my review. Process pages:
  flag for one LLM re-summarization + my review.

Any flag → the source's state becomes `quarantined`, its previous good data
stays in the output (never overwrite good data with garbage), and the run
report lists every quarantined source with reason. `report.py` prints that
table, writes `output/runs/<timestamp>.json`, and exits non-zero so any
scheduler (cron/GitHub Action) can email me. Notification hook is one function
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
- each run: updated `output/theses.json` + diff summary vs previous run
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
  output/{theses.json, theses.sqlite, preview/, runs/}
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

1. Skeleton: registry, state, cache, fetch (+ fetch command with resume)
2. spec_engine + one hand-written spec for
   https://www.ifi.uzh.ch/en/ddis/theses/topics.html → prove it, show
   `output/preview/ddis--1.json`
3. llm.py abstraction (OpenAI impl) + llm_extract for process pages
4. onboard command (interactive, as specified) incl. people-page link following
5. validate + store + report + notify; run command with resume
6. tests (contracts replay + unit tests for topic_id stability,
   normalization, link-pattern matching, escalation decision — mocked network)
7. status/check commands, README documenting the workflow

Politeness everywhere: sequential fetching, 2s delay, honest User-Agent. Do not
scrape sources beyond the ones I explicitly onboard.
