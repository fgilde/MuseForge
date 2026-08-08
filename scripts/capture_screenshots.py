"""Capture the README/landing-page screenshots from a running MuseForge.

Screenshots rot faster than prose: a UI change silently leaves the README
showing a product that no longer exists. Keeping the capture in the repo
means retaking them is one command against a running instance, not an
afternoon of cropping.

    python scripts/capture_screenshots.py [--base http://localhost:7861]
                                          [--out docs/screenshots]
                                          [--only studio,voices]

Needs Playwright with Chromium:  python -m playwright install chromium
"""
from __future__ import annotations

import argparse
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

VIEWPORT = {"width": 1600, "height": 1000}

# name -> (what to click to get there, how long the view needs to settle)
# Selectors are text-based on purpose: they survive class churn, and when one
# stops matching the shot is skipped loudly instead of capturing the wrong view.
SHOTS: list[dict] = [
    {
        "name": "studio",
        "title": "Studio — generation workspace",
        "steps": [],
    },
    {
        "name": "blueprints",
        "title": "Blueprints — reusable looks",
        "steps": [{"click": "Blueprints", "global": True}],
    },
    {
        "name": "lora-browser",
        "title": "LoRA browser with CivitAI",
        "steps": [{"click": "LoRAs", "global": True}],
    },
    {
        "name": "chat",
        "title": "Chat — local LLM",
        "steps": [{"click": "Text"}],
    },
    {
        "name": "storywriter",
        "title": "Storywriter — long-form prose",
        "steps": [{"click": "Text"}, {"click": "Story"}],
    },
    # Audiobooks and voices live under Audio, not Text.
    {
        "name": "audiobook",
        "title": "Audiobook producer",
        "steps": [{"click": "Audio"}, {"click": "Book"}],
    },
    {
        "name": "voices",
        "title": "Voice library",
        "steps": [{"click": "Audio"}, {"click": "Voices"}],
    },
    {
        "name": "settings-api",
        "title": "Settings — API & MCP",
        # The gear carries no label — only a title attribute.
        "steps": [{"css": '[title="Settings"]'}, {"click": "API & MCP", "global": True}],
    },
]


def capture(base: str, out_dir: str, wanted: set[str] | None) -> int:
    from playwright.sync_api import sync_playwright

    import os
    os.makedirs(out_dir, exist_ok=True)
    taken, skipped = 0, []

    with sync_playwright() as play:
        browser = play.chromium.launch()
        for shot in SHOTS:
            if wanted and shot["name"] not in wanted:
                continue
            page = browser.new_page(viewport=VIEWPORT, device_scale_factor=2)
            try:
                page.goto(base, wait_until="networkidle", timeout=60_000)
                page.wait_for_timeout(2500)          # let the panels animate in
                # First run shows a welcome dialog that covers the whole UI —
                # it swallowed every click before this and left six timeouts.
                for label in ("Get started", "Got it", "Close"):
                    button = page.get_by_role("button", name=label)
                    if button.count():
                        button.first.click()
                        page.wait_for_timeout(800)
                        break
                # Thumbnails decode lazily; without this the gallery is a grid
                # of black rectangles, which is a poor advertisement.
                page.wait_for_timeout(3000)
                # Mode tabs live in the right-hand dock, and some of their
                # labels ("Audio", "Text") also name a gallery filter on the
                # left. Scoping to the dock is what stops a click landing on
                # the wrong one.
                dock = page.locator("aside").last
                for step in shot["steps"]:
                    if step.get("css"):
                        page.locator(step["css"]).first.click(timeout=15_000)
                        page.wait_for_timeout(2200)
                        continue
                    label = step["click"]
                    scope = page if step.get("global") else dock
                    button = scope.get_by_role("button", name=label, exact=False)
                    target = (button.first if button.count()
                              else scope.get_by_text(label, exact=False).first)
                    target.click(timeout=15_000)
                    page.wait_for_timeout(2200)
                path = f"{out_dir}/{shot['name']}.png"
                page.screenshot(path=path)
                size = os.path.getsize(path)
                print(f"  {shot['name']:<14} {size // 1024:>5} KB  {shot['title']}")
                if size < 20_000:
                    skipped.append(f"{shot['name']} (suspiciously small: {size} B)")
                taken += 1
            except Exception as error:                # noqa: BLE001 — reported
                skipped.append(f"{shot['name']}: {str(error).splitlines()[0][:90]}")
            finally:
                page.close()
        browser.close()

    print(f"\n{taken} Screenshots in {out_dir}")
    for one in skipped:
        print(f"  ÜBERSPRUNGEN/VERDÄCHTIG: {one}")
    return 0 if taken and not skipped else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:7861")
    parser.add_argument("--out", default="docs/screenshots")
    parser.add_argument("--only", default="")
    args = parser.parse_args()
    wanted = {one.strip() for one in args.only.split(",") if one.strip()} or None
    print(f"Capturing from {args.base}\n")
    return capture(args.base, args.out, wanted)


if __name__ == "__main__":
    raise SystemExit(main())
