"""Config tests — the contract of `config.Settings`.

Two jobs. First, pin every default to the literal the code used *before*
config.py existed, so the centralization provably changed no behaviour. Second,
guard the reason it was centralized: `SCRAPER_DATA_ROOT` must relocate the whole
data tree, including the output paths that used to be frozen at import time.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from pydantic import ValidationError

from posting_scraper import llm, store, title_check
from posting_scraper.config import Settings, get_settings


def _clean_env(**overrides):
    """os.environ with every scraper/OpenAI variable stripped, plus overrides.

    Real environment variables beat .env in pydantic-settings, so a developer's
    local .env cannot influence anything asserted under this context manager.
    """
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("SCRAPER_") and k != "OPENAI_API_KEY"}
    env.update(overrides)
    return mock.patch.dict(os.environ, env, clear=True)


class DefaultsTest(unittest.TestCase):
    """Every default equals the value hardcoded in the modules before the move.

    `_env_file=None` on top of the cleaned environment so a local .env can't
    make these flaky.
    """

    def setUp(self):
        with _clean_env():
            self.s = Settings(_env_file=None)

    def test_politeness_defaults(self):
        self.assertEqual(self.s.polite_delay_seconds, 2.0)      # was fetch.py
        self.assertEqual(self.s.http_timeout_seconds, 30)
        self.assertEqual(self.s.render_idle_ms, 6000)
        self.assertEqual(self.s.render_settle_ms, 700)

    def test_cache_and_onboarding_defaults(self):
        self.assertEqual(self.s.cache_history_keep, 3)          # was cache.py
        self.assertEqual(self.s.profile_limit, 3)               # was an argparse default

    def test_llm_defaults(self):
        self.assertEqual(self.s.llm_provider, "openai")
        self.assertEqual(self.s.llm_model, "gpt-5-mini")
        self.assertIsNone(self.s.llm_base_url)                  # plain OpenAI API
        self.assertEqual(self.s.llm_max_attempts, 3)            # was a class attribute
        self.assertEqual(self.s.llm_timeout_seconds, 120)

    def test_budget_defaults(self):
        self.assertEqual(self.s.llm_page_text_budget, 6000)     # was llm_extract.py
        self.assertEqual(self.s.llm_fallback_html_budget, 14000)
        self.assertEqual(self.s.spec_draft_html_budget, 16000)  # was spec_generator.py

    def test_user_agent_names_the_tool_and_a_reachable_contact(self):
        ua = self.s.user_agent
        self.assertIn("UZH-Thesis-Scraper", ua)
        self.assertIn("academic research", ua)
        self.assertIn(f"mailto:{self.s.contact}", ua)

    def test_data_root_defaults_to_the_repo_checkout(self):
        self.assertTrue((self.s.data_root / "pyproject.toml").exists())
        self.assertTrue((self.s.data_root / "src" / "posting_scraper").is_dir())


class DataRootTest(unittest.TestCase):
    """SCRAPER_DATA_ROOT relocates the entire tree.

    Before config.py, store.py bound its five output paths from
    registry.OUTPUT_DIR at *import* time while cache.py resolved its directory
    per call — so setting the data root moved the cache but not the output.
    """

    ROOT = Path("/tmp/posting-scraper-config-test")

    def test_every_derived_path_follows_the_data_root(self):
        with _clean_env(SCRAPER_DATA_ROOT=str(self.ROOT)):
            s = get_settings()
            self.assertEqual(s.registry_path,
                             self.ROOT / "registry" / "scraping_sources.json")
            self.assertEqual(s.contracts_dir, self.ROOT / "contracts")
            self.assertEqual(s.state_path, self.ROOT / "var" / "state.json")
            self.assertEqual(s.cache_dir, self.ROOT / "cache")
            self.assertEqual(s.output_dir, self.ROOT / "output")
            self.assertEqual(s.preview_dir, self.ROOT / "output" / "preview")
            self.assertEqual(s.runs_dir, self.ROOT / "output" / "runs")

    def test_store_output_paths_follow_the_data_root(self):
        """The regression the import-time binding used to hide."""
        with _clean_env(SCRAPER_DATA_ROOT=str(self.ROOT)):
            out = self.ROOT / "output"
            self.assertEqual(store.data_path(), out / "extracted_data.json")
            self.assertEqual(store.sqlite_path(), out / "extracted_data.sqlite")
            self.assertEqual(store.raw_people_path(), out / "people_raw.json")
            self.assertEqual(store.raw_process_path(), out / "process_raw.json")
            self.assertEqual(store.raw_faculty_process_path(),
                             out / "faculty_process_raw.json")

    def test_a_relocated_root_is_not_created_as_a_side_effect(self):
        """Reading settings must never touch the filesystem."""
        with _clean_env(SCRAPER_DATA_ROOT="/tmp/posting-scraper-should-not-exist"):
            s = get_settings()
            self.assertFalse(s.cache_dir.exists())
            self.assertFalse(s.data_root.exists())


class ApiKeyTest(unittest.TestCase):
    """OPENAI_API_KEY keeps working; the prefixed name takes precedence."""

    def test_openai_api_key_is_accepted(self):
        with _clean_env(OPENAI_API_KEY="sk-from-openai-var"):
            self.assertEqual(get_settings().llm_api_key, "sk-from-openai-var")

    def test_prefixed_key_wins_when_both_are_set(self):
        with _clean_env(OPENAI_API_KEY="sk-old",
                        SCRAPER_LLM_API_KEY="sk-explicit"):
            self.assertEqual(get_settings().llm_api_key, "sk-explicit")

    def test_no_key_means_the_llm_reports_itself_unavailable(self):
        """The graceful-degradation contract: callers keep deterministic output.

        An explicit empty value rather than an absent one, so a real .env on the
        machine running the tests cannot supply a key behind our back.
        """
        with _clean_env():
            self.assertIsNone(Settings(_env_file=None).llm_api_key)
        with _clean_env(SCRAPER_LLM_API_KEY=""):
            self.assertFalse(get_settings().llm_api_key)
            self.assertFalse(llm.is_available())


class ValidationTest(unittest.TestCase):
    def test_a_bad_number_fails_loudly(self):
        """Fail at startup, not silently on the default — a typo'd delay that
        fell back to 2.0s would look like it worked."""
        with _clean_env(SCRAPER_POLITE_DELAY_SECONDS="soon"):
            with self.assertRaises(ValidationError):
                get_settings()

    def test_a_valid_override_is_coerced_to_its_type(self):
        with _clean_env(SCRAPER_POLITE_DELAY_SECONDS="0.5",
                        SCRAPER_CACHE_HISTORY_KEEP="10"):
            s = get_settings()
            self.assertEqual(s.polite_delay_seconds, 0.5)
            self.assertEqual(s.cache_history_keep, 10)

    def test_an_unknown_scraper_variable_is_ignored(self):
        """extra="ignore": a stale variable must not break startup."""
        with _clean_env(SCRAPER_NOT_A_REAL_SETTING="x"):
            self.assertEqual(get_settings().polite_delay_seconds, 2.0)


class DeliberatelyNotConfigurableTest(unittest.TestCase):
    """Title plausibility is calibrated, not configured.

    The thresholds are tuned against the 247 titles in golden_contracts.json and
    guarded by CorpusCalibrationTest. An environment variable that moved them
    would change *stored data* and break the determinism invariant, so they must
    stay module constants.
    """

    def test_the_thresholds_are_not_settings_fields(self):
        fields = set(Settings.model_fields)
        for name in ("plausible_min", "max_title_chars", "short_title_chars",
                     "title_check", "title_plausible_min"):
            self.assertNotIn(name, fields)

    def test_the_thresholds_keep_their_calibrated_values(self):
        self.assertEqual(title_check.PLAUSIBLE_MIN, 0.5)
        self.assertEqual(title_check.MAX_TITLE_CHARS, 300)
        self.assertEqual(title_check.SHORT_TITLE_CHARS, 12)


if __name__ == "__main__":
    unittest.main()
