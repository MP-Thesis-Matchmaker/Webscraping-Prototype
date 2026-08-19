# Provenance of the source list

Nothing under `src/` reads this directory. It exists so the origin of the
scraped source list stays auditable.

- **`scraping_source_pages.xlsx`** — the original manual crawl of all seven UZH
  faculties. Sheets: `Full structure crawl` (the authoritative rows),
  `Summary`, `Method & Legend`, `Official source pages`. Both JSON files below
  were generated from the visible rows of `Full structure crawl`.
- **`scraping_sources.json`** — the first full conversion: **99 units / 142
  source URLs**. Superseded. It is the wider draft referenced in
  `docs/scraper_plan.md`; the units that were dropped are the ones where the
  crawl found no usable public page.

The list the scraper actually reads is **`registry/scraping_sources.json`** at
the repo root: **37 units / 103 sources (106 URLs** — a few sources bundle two).
It is not a re-crawl, just the 99-unit list narrowed to units with something
scrapable.
