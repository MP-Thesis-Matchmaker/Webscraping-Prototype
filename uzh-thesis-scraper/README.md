# UZH Thesis Scraper

Aggregates open Bachelor/Master thesis information — supervisors, open topics,
and how-to-apply procedures — across all seven faculties of the University of
Zurich into one structured dataset for a thesis-matching tool.

**Core philosophy:** humans decide *where* and *what*; deterministic templates
make extraction *repeatable*; cached HTML *decouples* fetching from scraping;
alarms *report drift*. The LLM appears in three controlled places —
summarizing process pages, drafting extraction templates during onboarding, and
a run-time *fallback* that rescues a source whose template matched nothing — and
sits behind an exchangeable interface. Routine re-runs are 100% deterministic:
same cached page + same template ⇒ identical records. The fallback only fires on
failure and is always flagged for review, so it never silently changes a
working source.

## Status

All 37 units / 103 sources in the current registry are onboarded (`verified`)
and run (`done`), zero quarantined.

| | |
|---|---|
| Faculties | 7 (WWF, PhF, RWF, TRF, MNF, MeF, VSF) |
| Units | 37 registry / 35 in output* |
| Concrete topics | 711 |
| People | 569 |
| Process entries | 58 |
| Rows in `extracted_data.sqlite` | 1,338 |

\* Two units expose only a faculty-shared page and consolidate into a
faculty-level process entry via `scope: faculty`, so they carry no unit-level
output.

## Setup

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium   # JS-render fallback
```

Create a `.env` for the LLM (only needed for onboarding new sources and running
process pages):

```
OPENAI_API_KEY=sk-...
```

> Always invoke the tool with the venv interpreter (`.venv/bin/python`); the
> system Python does not have PyYAML / BeautifulSoup / OpenAI installed.

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
4. **Validate** — classify each result; any flag quarantines the source and
   keeps its previous good data (never overwrite good data with garbage).
5. **Store** — `output/extracted_data.json` (nested by faculty → unit) + a
   SQLite mirror. The written JSON is a cleaned *public view*: internal keys
   never reach disk — the `_llm` debug blob is dropped and the profile link is
   exposed as `profile_url` (not the internal `_profile_url`).

## CLI

Run everything through `python -m scraper <command>`:

```bash
# Stage 1: fetch pages into the cache
python -m scraper fetch [--only ID ...] [--resume] [--render]

# Interactive verification of one source (drafts a spec, shows records, freezes
# the contract on approval). --next picks the first unverified source.
python -m scraper onboard <source_id> | --next
        [--page-type process|topics|people|none] [--hint TEXT]
        [--refetch] [--redraft] [--no-follow] [--profile-limit N] [--yes]

# Extract verified sources from cache, validate, store. Resumable.
# When a source's template matches nothing, an LLM fallback tries to recover it
# (flagged for review); pass --no-llm-fallback to disable.
python -m scraper run [--only ID ...] [--resume] [--no-llm-fallback]

# No-network snapshot: lifecycle state of every source + last-run summary.
python -m scraper status

# Dry-run one source: re-extract from cache and diff against stored data,
# writing nothing. Use it to check a spec edit before a full run.
python -m scraper check <source_id>
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
source_id: ddis--1
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

All page-specific knowledge lives in the spec; the engine (`scraper/spec_engine.py`)
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

Run the test suite (no network, stdlib `unittest`):

```bash
.venv/bin/python -m unittest discover -s tests -p "test_*.py"
```

- **`tests/test_contracts.py`** — replays every topics/people spec against its
  frozen snapshot, offline, and asserts it reproduces a committed golden baseline
  (`tests/golden_contracts.json`). This is the regression net for the spec
  engine: a change that alters what any source extracts fails the suite. The
  golden captures the engine's deterministic *core* extraction (before the
  network-dependent enrichment/roster steps that `run` applies).
- **`tests/test_units.py`** — unit tests for `topic_id` stability, the field
  transforms/normalization, profile-link matching, and the run's escalation
  (quarantine) decision.

After an *intended* change to the engine or a spec, regenerate the baseline and
commit it:

```bash
.venv/bin/python tests/regen_golden.py
```

## Break detection & quarantine

`scraper/validate.py` classifies every source's result each run:

| status | meaning | quarantines? |
|---|---|---|
| `ok` | extracted, schema-valid; content unchanged (or the HTML changed but the records didn't — a cosmetic change is quieted to `ok`) | no |
| `page_changed` | schema-valid, and the re-extracted records actually differ — data is updated and the source is flagged with a record-level diff for review, but it **stays verified and keeps scraping** | no |
| `llm_fallback` | the deterministic template failed, but the LLM fallback produced schema-valid records — stored, yet quarantined so the template gets fixed | yes |
| `fetch_failed` | no usable cached page / last fetch errored | yes |
| `extract_failed` | template matched nothing and the fallback couldn't recover it | yes |
| `schema_invalid` | required fields missing or emails/links malformed | yes |

`page_changed` and `llm_fallback` are the two "store but alert" statuses: the
data is written *and* the source is flagged for review. The LLM fallback
(`scraper/llm_extract.py:extract_records_fallback`) fires when the deterministic
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

## Pause & resume

State lives in `registry/state.json`: per source, an onboarding status
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
.venv/bin/python -m scraper fetch --resume && \
.venv/bin/python -m scraper run --resume
```

Wire that into launchd (macOS), cron, or a CI schedule as needed.

## Repository layout

```
uzh-thesis-scraper/
  registry/scraping_sources.json   # the curated source list
  registry/state.json              # per-source lifecycle + run progress
  contracts/<source_id>/           # spec.yaml, snapshot.*, expected.json
  cache/<source_id>/               # page.html, meta.json, history/, followed subpages
  scraper/
    registry.py  fetch.py  cache.py         # skeleton: sources, fetch, cache
    spec_engine.py  spec_generator.py       # deterministic extraction + LLM spec draft
    llm.py  llm_extract.py                  # LLM abstraction + process summaries/PDFs
    validate.py  store.py  report.py        # break detection, storage, run report
    main.py                                 # the CLI
  output/{extracted_data.json, extracted_data.sqlite, preview/, runs/}
  tests/{test_contracts.py, test_units.py, replay_util.py, regen_golden.py,
         golden_contracts.json}
```

Politeness everywhere: sequential fetching, a 2s delay, an honest User-Agent, and
no scraping of sources beyond the ones explicitly onboarded.
