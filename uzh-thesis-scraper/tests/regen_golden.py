"""Regenerate the contract-replay golden baseline (tests/golden_contracts.json).

Run this AFTER an intentional change to the spec engine or a spec.yaml, once
you've confirmed the new extraction is correct:

    python tests/regen_golden.py

It captures the current offline extraction of every replayable contract. The
test suite (tests/test_contracts.py) then asserts nothing drifts from it.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import replay_util  # noqa: E402


def main() -> None:
    out = replay_util.regenerate()
    total = sum(len(v) for v in out.values())
    print(f"wrote {replay_util.GOLDEN_PATH}")
    print(f"  {len(out)} contracts, {total} records")


if __name__ == "__main__":
    main()
