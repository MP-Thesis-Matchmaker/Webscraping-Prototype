# UZH Thesis Scraper

Aggregates open Bachelor/Master thesis information — supervisors, open topics,
and how-to-apply procedures — across all seven faculties of the University of
Zurich into one structured dataset for a thesis-matching tool.

**Core philosophy:** humans decide *where* and *what*; deterministic templates
make extraction *repeatable*; cached HTML *decouples* fetching from scraping;
alarms *report drift*. The LLM appears in three controlled places —
summarizing process pages, drafting extraction templates during onboarding, and
a run-time *fallback* that rescues a source whose template matched nothing — plus
an opt-in advisory during onboarding (`--llm-title-review`) whose output is
printed, never stored. All of it sits behind an exchangeable interface. Routine
re-runs are 100% deterministic: same cached page + same template ⇒ identical
records. The fallback only fires on failure and is always flagged for review, so
it never silently changes a working source.

## Status

All 37 units / 103 sources in the current registry are onboarded (`verified`)
and run (`done`), zero quarantined.

| | |
|---|---|
| Faculties | 7 (WWF, PhF, RWF, TRF, MNF, MeF, VSF) |
| Units | 37 registry / 35 in output* |
| Concrete topics | 707 |
| People | 565 |
| Process entries | 57 |
| Rows in `extracted_data.sqlite` | 1,329 |

\* Three sources carry `scope: faculty` and consolidate into one PhF-level
process entry crediting all three. Two of their units
(`musikwissenschaftliches-institut`, `philosophisches-seminar`) have no other
source, so they carry no unit-level output; the third does.

Counts include records nested under `unit.groups.<chair>` (160 topics, 20 people,
15 process entries) — a flat count of `unit.concrete_topics` alone reports 547
topics and misses the grouped ones.

## Setup

Needs Python 3.11+.

```bash
uv sync --extra dev                           # package (editable) + pytest/ruff
uv sync --extra dev --extra render            # add the Playwright JS-render fallback
uv run python -m playwright install chromium  # only with the render extra
```

`playwright` is an opt-in extra because it pulls a browser download; `fetch.py`
lazy-imports it and falls back to the static fetch when it is absent.

## Configuration

Every setting lives in one place: `Settings` in `src/posting_scraper/config.py`,
a `pydantic_settings.BaseSettings` class (the same shape as `config.py` in
`backend-core`). Values are read from environment variables first, then from a
`.env` at the repo root or the working directory. Copy `.env.example` — it lists
every variable with its default and what it does — and fill in what you need:

```bash
cp .env.example .env
```

Nothing is required. With no `.env` at all the scraper runs fully deterministic:
the only settings without a usable default are the LLM credentials, and every
LLM path degrades to its rule-based output when they are absent.

The variables you are most likely to touch:

| Variable | Default | Effect |
|---|---|---|
| `OPENAI_API_KEY` / `SCRAPER_LLM_API_KEY` | unset | Enables spec drafting at onboarding, process/PDF summaries, and the run-time extraction fallback. Unset ⇒ `llm.is_available()` is False and callers keep deterministic output. |
| `SCRAPER_LLM_MODEL` | `gpt-5-mini` | Model used for all three LLM paths. |
| `SCRAPER_LLM_BASE_URL` | unset | OpenAI-compatible gateway (LibreChat / AI Buddy) or a local model, e.g. Ollama at `http://localhost:11434/v1`. |
| `SCRAPER_DATA_ROOT` | the repo checkout | Root of everything read or written: `registry/`, `contracts/`, `cache/`, `output/`, `var/`. Relocates all of them together. |
| `SCRAPER_CONTACT` | maintainer address | Address advertised in the User-Agent so a site owner can reach a human. |
| `SCRAPER_POLITE_DELAY_SECONDS` | `2.0` | Delay between requests. Lower it only with a reason. |

Read them in code with `get_settings()`, never at import time:

```python
from .config import get_settings

def fetch_something(url):
    s = get_settings()
    return requests.get(url, timeout=s.http_timeout_seconds)
```

Calling it inside the function is what makes `SCRAPER_DATA_ROOT` work
everywhere — a module-level `PATH = settings.output_dir` would freeze the root
at import and silently ignore the variable.

Two things are deliberately **not** configurable, because they would change
stored data rather than how it is obtained: the title-plausibility thresholds in
`title_check.py` (calibrated against `tests/golden_contracts.json`, see below)
and the extraction field lists, regexes and prompts. Those are domain
constants — the determinism invariant depends on an environment variable being
unable to move them.

## The pipeline

```
registry ──▶ fetch ──▶ cache/ ──▶ extract (routed by page_type) ──▶ validate ──▶ store ──▶ output/
```

1. **Registry** (`registry/scraping_sources.json`) — the human-curated list of
   *where* to scrape. Each source has a stable `source_id`, a URL, and notes.
2. **Fetch & cache** — requests first, Playwright-chromium fallback when a page
   looks empty/blocked. Everything lands in `cache/<source_id>/`. All extraction
   reads from cache, never the network; refetching is a separate command.
3. **Extract, routed by `page_type`:**
   - `process` → LLM summary into the process layer (cached by prompt hash).
   - `topics` → deterministic `spec.yaml` (CSS selectors) → `concrete_topics`.
   - `people` → deterministic `spec.yaml` + follow each profile link one step
     deep (also cached) → `people`.
   - `none` → source carries no extractable thesis data.
4. **Validate** — classify each result. Hard failures (`fetch_failed`,
   `extract_failed`, `schema_invalid`) and an `llm_fallback` rescue *quarantine*
   the source, keeping its previous good data (never overwrite good data with
   garbage); `page_changed` and `needs_review` are flagged for review but keep
   the source verified and scraping. See
   [Break detection & quarantine](#break-detection--quarantine).
5. **Store** — `output/extracted_data.json` (nested by faculty → unit) + a
   SQLite mirror. The written JSON is a cleaned *public view*: internal keys
   never reach disk — the `_llm` debug blob is dropped and the profile link is
   exposed as `profile_url` (not the internal `_profile_url`).

## CLI

Run everything through the `posting-scraper` console script (or
`python -m posting_scraper`):

```bash
# Stage 1: fetch pages into the cache
posting-scraper fetch [--only ID ...] [--resume] [--render]

# Interactive verification of one source (drafts a spec, shows records, freezes
# the contract on approval). --next picks the first unverified source.
posting-scraper onboard <source_id> | --next
        [--page-type process|topics|people|none] [--hint TEXT]
        [--refetch] [--redraft] [--no-follow] [--profile-limit N]
        [--llm-title-review] [--yes]

# Extract verified sources from cache, validate, store. Resumable.
# When a source's template matches nothing, an LLM fallback tries to recover it
# (flagged for review); pass --no-llm-fallback to disable.
posting-scraper run [--only ID ...] [--resume] [--no-llm-fallback]

# No-network snapshot: lifecycle state of every source + last-run summary.
posting-scraper status

# Dry-run one source: re-extract from cache and diff against stored data,
# writing nothing. Use it to check a spec edit before a full run.
posting-scraper check <source_id>
```

`run` (and `check`) exit non-zero when any source is flagged, so a scheduler can
alert on the exit code.

## Data model

```
faculties[CODE]:
  faculty, faculty_code
  process[]                     # faculty-wide procedures (scope: faculty)
  units[unit_id]:
    unit
    people[]:                   # role, name, email, research_interest,
                                #   research_field, bio, personal_website,
                                #   profile_url
    process[]:                  # degree_level, process_description,
                                #   relevant_links[{url, description}], source_url
    concrete_topics[]:          # degree_level, date_of_listing, research_area,
                                #   supervisors[{name, email}], topic_description,
                                #   status, source_link, topic_id
    groups{}                    # optional: topics/people grouped by research group
```

Every record carries `source_id` and `scraped_at` (UTC ISO). Each concrete topic
gets a stable `topic_id = sha1(source_url + normalized seed)`, where the seed is
the spec's `id_from` fields (or the topic description).

## Writing a spec (`contracts/<source_id>/spec.yaml`)

A spec is a small declarative document: a `container` CSS selector that picks out
each record, plus a `fields` map saying how to pull each field from a container.

```yaml
source_id: example--1        # illustrative; contracts/ifi--3/spec.yaml is a real one
page_type: topics
record:
  container: ".teaser"
  fields:
    title: ".teaser__title"          # shorthand: a selector string
    supervisors:
      each: ".teaser__person"        # a list of sub-objects
      fields:
        name: { attr: text, transform: strip_titles }
    source_link: { selector: "a", attr: href, transform: absolute_url }
id_from: [title]
```

All page-specific knowledge lives in the spec; the engine (`src/posting_scraper/spec_engine.py`)
stays generic. Beyond plain selectors it supports:

- **Transforms** (`transform:`) — composable field cleaners: `name_lastfirst`
  (`"Backhaus, Norman, Prof. Dr."` → `"Norman Backhaus"`), `name_lastfirst_space`,
  `pi_surname` (a `SURNAME_` PDF-link prefix, title-cased), `titlecase`,
  `strip_titles`, `academic_role`, `deobfuscate_email`, `degree_from_type`,
  `norm_status`, `absolute_url`, `normalize_ws`, and more.
  **Ordering contract:** transforms run *before* a field's `regex:` — a
  match-and-destroy transform (like `pi_surname`) must do its own regex, never
  rely on a later `regex:`.
- **`follow`** — for `people` (and topic-detail) pages: follow a record's link
  one step deep, cached under `cache/<id>/people|topics/`, and merge fields from
  the followed page. Only links matching the follow `url_pattern` are followed.
- **`sectioned_people` / `grouped_people`** — people listed under function
  headings or grouped by research field, inverted to one record per person.
- **`pdf_enrich`** — one-PDF-per-project topic lists: parse each PDF for a fuller
  description and the supervisor's name/email.
- **`scope: faculty`** — sources that share one central page consolidate into a
  single faculty-level process entry crediting every contributing unit.
- **`also_process`** — a dual page that is both a topic/people listing *and* a
  description of the chair's application procedure: the records are extracted
  deterministically and the page is *also* LLM-summarized into `process`. The
  most-used optional key in the corpus (16 specs).
- **`group`** — attributes a source's records to a named research group or chair;
  they nest under `unit.groups.<id>` instead of at unit level (17 specs), which
  is where a large share of the stored records actually live.

Onboard interactively (`onboard`) rather than hand-writing specs: the LLM drafts
the spec, the engine runs it immediately, and you approve / edit / retry before
the contract is frozen.

## Contracts & tests

On approval, onboarding freezes a **contract** in `contracts/<source_id>/`:

- `spec.yaml` — the extraction template (topics/people only).
- `snapshot.html` / `snapshot.json` — the exact page the expectation was verified
  against.
- `expected.json` — the **onboarding provenance snapshot**: the full approved
  records at verification time, *including* enrichment (followed profiles,
  PDF-parsed supervisors). It is human-facing documentation of what was
  approved, **not** the test oracle, and nothing reads it back at runtime. The
  contract test instead asserts against `tests/golden_contracts.json` — the
  engine's deterministic, offline-reproducible core extraction — because
  `expected.json` carries enrichment that can't be reproduced without the
  network/LLM and can drift as specs are edited after freezing.

Run the test suite (no network; `unittest.TestCase` classes collected by pytest):

```bash
uv run pytest
```

- **`tests/test_contracts.py`** — replays every replayable topics/people spec
  against its frozen snapshot, offline, and asserts it reproduces a committed
  golden baseline of 1,002 records
  (`tests/golden_contracts.json`). Of the 103 contract directories, 66 carry a
  `spec.yaml`; the 16 whose `page_type` is `process` are LLM-summarized rather
  than selector-extracted and cannot be replayed, leaving exactly **50** (29
  topics + 21 people). This is the regression net for the spec engine: a change
  that alters what any source extracts fails the suite. The golden captures the
  engine's deterministic *core* extraction (before the network-dependent
  enrichment/roster steps that `run` applies).
- **`tests/test_units.py`** — unit tests for `topic_id` stability, the field
  transforms/normalization, profile-link matching, and the run's escalation
  (quarantine) decision.

After an *intended* change to the engine or a spec, regenerate the baseline and
commit it:

```bash
uv run python tests/regen_golden.py
```

## Break detection & quarantine

`src/posting_scraper/validate.py` classifies every source's result each run:

| status | meaning | quarantines? |
|---|---|---|
| `ok` | extracted, schema-valid; content unchanged (or the HTML changed but the records didn't — a cosmetic change is quieted to `ok`) | no |
| `page_changed` | schema-valid, and the re-extracted records actually differ — data is updated and the source is flagged with a record-level diff for review, but it **stays verified and keeps scraping** | no |
| `needs_review` | schema-valid, but a record's title is implausible and nothing better was found on the page — stored and flagged, source **keeps scraping** (see [Title plausibility](#title-plausibility)) | no |
| `llm_fallback` | the deterministic template failed, but the LLM fallback produced schema-valid records — stored, yet quarantined so the template gets fixed | yes |
| `fetch_failed` | no usable cached page / last fetch errored | yes |
| `extract_failed` | template matched nothing and the fallback couldn't recover it | yes |
| `schema_invalid` | required fields missing or emails/links malformed | yes |

`page_changed` and `llm_fallback` are the two "store but alert" statuses: the
data is written *and* the source is flagged for review. The LLM fallback
(`src/posting_scraper/llm_extract.py:extract_records_fallback`) fires when the deterministic
template fails — either it matched nothing (`extract_failed`) or what it matched
was malformed (`schema_invalid`). It reads the cached page's cleaned main HTML,
asks the model for target-model records as JSON, caches the reply by content
hash (no re-spend), and degrades to a no-op when no LLM is configured. A rescue
only replaces the original result if it is itself schema-valid, so a worse
fallback never clobbers the diagnosis. Disable it with `run --no-llm-fallback`.

Only hard failures and `llm_fallback` **quarantine** a source (drop it from
future runs until re-onboarded); their previous good data stays in the output. A
`page_changed` keeps the source verified and scraping — it's only a review note,
because for a weekly scraper you *want* pages whose content changes to keep being
tracked. Either way the run report (`output/runs/<timestamp>.json`) lists every
flagged source with a reason, and the run exits non-zero. `report.notify(summary)`
is the single, swappable notification hook (prints today; later email/webhook
without touching callers).

## Title plausibility

A spec says *where* a title sits; it cannot say what a title should look like. So
a selector that keeps matching the wrong element yields a wrong-but-well-formed
value that no structural check can catch — the page did not move, the template
still matched, the record is schema-valid. `ifi--5` stored a topic titled
`"November 3, 2021"` that way: 11 of the 12 blocks on that page put the title in
the `h3`, and one put a posting date there and the real title in a bold paragraph
below.

`src/posting_scraper/title_check.py` closes that gap for `topics` records, deterministically
and with no LLM, on a reserve-then-replace contract:

1. **Score** the extracted title. A string that is *entirely* something else
   scores 0 — a date (EN/DE, numeric, ISO), an availability word
   (`Taken`/`vergeben`/…), a section label (`Thesis`, `Masterarbeit`, `PDF`), a
   degree or ECTS marker, a semester code, an email, a URL, or a 300+ character
   paragraph. Softer penalties cover a single short token and a title that merely
   repeats the record's `status`.
2. **Reserve, don't discard.** An implausible title is kept in hand while the
   record's own container is scanned for a better candidate, in a fixed order:
   `p > strong`, other headings, `strong`/`b`, `dt`/`caption`/`[class*=title]`,
   link text, the PDF filename stem, then the first clause of the description.
3. **Case A — a plausible alternative exists.** It becomes the title. The
   rejected string is parked, not dropped: into `date_of_listing` when it is a
   date, otherwise into an internal `_title_rejected`. A description that merely
   repeated the promoted title loses that prefix.
4. **Case B — nothing better.** The original title stays, because a bad title
   carries more than no title, and the source is flagged `needs_review`.

`topic_id` is computed *after* the repair, so ids are seeded from the corrected
title. The bookkeeping keys (`_title_repair`, `_title_check`, `_title_rejected`)
are internal and never reach `extracted_data.json`. Repairs are recorded in the
run report but do not flag the run; only Case B does.

Per-spec escape hatches:

```yaml
title_check: false                  # opt out entirely for this source
title_candidates: [".topic-name"]   # selectors to try first, before the defaults
```

The heuristic is calibrated against the committed golden baseline, not intuition:
`tests/test_title_check.py::CorpusCalibrationTest` asserts every one of the 247
titles in `tests/golden_contracts.json` passes the check and that the repair
touched exactly one record. Tighten it too far and that test fails rather than
quietly rewriting good titles.

`onboard` prints every repair and flag before you approve a spec. With
`--llm-title-review` it also prints the model's opinion on the flagged ones —
advisory text only, never stored, so routine runs stay deterministic.

## Pause & resume

State lives in `var/state.json`: per source, an onboarding status
(`unverified | verified | quarantined`) and per-run progress
(`pending | fetched | extracted | done | failed`). Every command is
interruptible (Ctrl-C finishes the current source, writes state, exits) and
resumable — `fetch --resume` / `run --resume` continue with pending sources only.
The cache makes this cheap: completed work is never redone.

## Scheduling (weekly runs)

The scraper has **no built-in scheduler** — by design. A run exits non-zero when
anything is flagged, so an external cron job or CI workflow can run it weekly and
alert on the exit code, e.g.:

```bash
uv run posting-scraper fetch --resume && \
uv run posting-scraper run --resume
```

Wire that into launchd (macOS), cron, or a CI schedule as needed.

## Repository layout

```
pyproject.toml                     # deps, extras, console script, ruff + pytest config
.env.example                       # every setting, with defaults (copy to .env)
src/posting_scraper/               # code only
  config.py                               # pydantic Settings — the only config
  registry.py  fetch.py  cache.py         # skeleton: sources, fetch, cache
  spec_engine.py  spec_generator.py       # deterministic extraction + LLM spec draft
  llm.py  llm_extract.py                  # LLM abstraction + process summaries/PDFs
  title_check.py                          # title plausibility + repair
  validate.py  store.py  report.py        # break detection, storage, run report
  main.py                                 # the CLI
tests/{test_contracts.py, test_units.py, test_title_check.py, test_config.py,
       replay_util.py, regen_golden.py, golden_contracts.json}
registry/scraping_sources.json     # the curated source list (input, tracked)
contracts/<source_id>/             # spec.yaml, snapshot.*, expected.json (onboarding provenance)
var/state.json                     # per-source lifecycle + run progress (untracked)
cache/<source_id>/                 # page.html, meta.json, history/, followed subpages (untracked)
output/{extracted_data.json, extracted_data.sqlite, *_raw.json, preview/, runs/}
docs/scraper_plan.md               # the implementation plan and build ledger
archive/                           # provenance of the source list; nothing reads it
```

Code lives in `src/`; everything the scraper reads or writes sits beside it at
the repo root, so the read-only inputs (`registry/`, `contracts/`) are visibly
separate from the machine-written state (`var/`, `cache/`, `output/`).

Politeness everywhere: sequential fetching, a 2s delay (`SCRAPER_POLITE_DELAY_SECONDS`),
an honest User-Agent, and no scraping of sources beyond the ones explicitly onboarded.
