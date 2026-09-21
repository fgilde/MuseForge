"""Helpers for upstream tests that assume things this fork does not ship.

MuseForge is Docker-first and carries no Pinokio launcher, so upstream
contracts about `pinokio.js`, `start.js`, `update.js` and friends have
nothing to assert against here. Skipping states that in the test output,
where deleting the tests would quietly drop coverage upstream still wants
— and a skip keeps those files mergeable when upstream edits them.
"""
from __future__ import annotations

import os
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def missing_launcher_files(*names: str) -> list[str]:
    """Which of these launcher scripts are absent from the repository root."""
    return [name for name in names if not os.path.isfile(os.path.join(ROOT, name))]


def needs_launcher(*names: str):
    """Skip the decorated test when the named launcher scripts are absent."""
    missing = missing_launcher_files(*names)
    return unittest.skipIf(
        missing,
        f"no Pinokio launcher in this fork (missing {', '.join(missing)})",
    )


def replaced_by_media_grid(what: str):
    """Skip an upstream contract about the virtualized media feed.

    This fork replaced that feed with a uniform card grid (MediaGrid.tsx)
    after its measured-height and scroll-sync machinery proved to be the
    source of the reported scrolling trouble. The behaviour these tests
    describe therefore has no counterpart here.
    """
    return unittest.skip(
        f"this fork renders the gallery with MediaGrid, not the virtualized feed ({what})"
    )


def _self_check() -> None:
    """A present file does not skip; an absent one does, and says which."""
    assert missing_launcher_files("README.md") == []
    assert missing_launcher_files("pinokio.js") == ["pinokio.js"]
    assert missing_launcher_files("README.md", "no_such_launcher.js") == [
        "no_such_launcher.js"
    ]

    class Probe(unittest.TestCase):
        @needs_launcher("no_such_launcher.js")
        def test_skipped(self):
            raise AssertionError("should have been skipped")

        @needs_launcher("README.md")
        def test_runs(self):
            pass

    result = unittest.TextTestRunner(verbosity=0).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(Probe)
    )
    assert len(result.skipped) == 1, result.skipped
    assert result.wasSuccessful(), result
    print("_fork self-check: OK")


if __name__ == "__main__":
    _self_check()
