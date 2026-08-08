"""Fail the site build on a broken internal link or a missing asset.

A dead link on a landing page is invisible to the person who wrote it and
obvious to everyone else. This walks the built site, resolves every local
href/src, and reports what does not exist — including the anchors, because a
renamed heading breaks a nav entry silently.

    python scripts/check_site_links.py site

External links (http, mailto) are not fetched; this stays offline and fast.
"""
from __future__ import annotations

import os
import re
import sys
from html.parser import HTMLParser

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_EXTERNAL = ("http://", "https://", "mailto:", "tel:", "data:", "//")


class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []
        self.assets: list[str] = []
        self.ids: set[str] = set()

    def handle_starttag(self, tag: str, attrs) -> None:
        got = dict(attrs)
        if got.get("id"):
            self.ids.add(got["id"])
        if tag == "a" and got.get("href"):
            self.links.append(got["href"])
        elif got.get("src"):
            self.assets.append(got["src"])
        elif tag == "link" and got.get("href"):
            self.assets.append(got["href"])


def _parse(path: str) -> _Page:
    page = _Page()
    with open(path, encoding="utf-8") as handle:
        page.feed(handle.read())
    return page


def check(root: str) -> list[str]:
    pages: dict[str, _Page] = {}
    for base, _dirs, files in os.walk(root):
        for name in files:
            if name.endswith(".html"):
                full = os.path.join(base, name)
                pages[os.path.normpath(full)] = _parse(full)

    problems: list[str] = []
    for path, page in sorted(pages.items()):
        here = os.path.dirname(path)
        rel = os.path.relpath(path, root)

        for target in page.assets:
            if target.startswith(_EXTERNAL):
                continue
            resolved = os.path.normpath(os.path.join(here, target.split("#")[0]))
            if not os.path.exists(resolved):
                problems.append(f"{rel}: missing asset {target}")

        for target in page.links:
            if target.startswith(_EXTERNAL):
                continue
            anchor = ""
            if "#" in target:
                target, anchor = target.split("#", 1)
            if not target:                       # same-page anchor
                if anchor and anchor not in page.ids:
                    problems.append(f"{rel}: no element with id '{anchor}'")
                continue
            resolved = os.path.normpath(os.path.join(here, target))
            if os.path.isdir(resolved):
                resolved = os.path.join(resolved, "index.html")
            if not os.path.exists(resolved):
                problems.append(f"{rel}: broken link {target}")
            elif anchor:
                other = pages.get(os.path.normpath(resolved))
                if other and anchor not in other.ids:
                    problems.append(f"{rel}: {target} has no id '{anchor}'")

    if not pages:
        problems.append(f"no HTML pages found under {root}")
    return problems


def demo() -> None:
    """A broken link is caught; a good one is not reported."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "sub"))
        with open(os.path.join(tmp, "index.html"), "w", encoding="utf-8") as handle:
            handle.write('<a href="sub/">ok</a><a href="nope.html">bad</a>'
                         '<a href="#here">anchor</a><h2 id="here">x</h2>'
                         '<img src="gone.png">'
                         '<a href="https://example.com">external</a>')
        with open(os.path.join(tmp, "sub", "index.html"), "w", encoding="utf-8") as handle:
            handle.write("<p>hi</p>")
        found = check(tmp)
        assert any("nope.html" in one for one in found), found
        assert any("gone.png" in one for one in found), found
        assert not any("sub/" in one for one in found), found
        assert not any("example.com" in one for one in found), found
        assert len(found) == 2, found
    print("check_site_links self-check: OK")


def main() -> int:
    if "--self-check" in sys.argv:
        demo()
        return 0
    root = sys.argv[1] if len(sys.argv) > 1 else "site"
    problems = check(root)
    for one in problems:
        print(f"  {one}")
    print(f"\n{len(problems)} problem(s) in {root}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
