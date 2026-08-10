"""Keep the Umbrel and Unraid store templates honest.

A store template is the one file nobody looks at again after it is written,
and it is the only thing standing between a user and a broken install. The
failures it protects against are all silent: a version left behind after a
release, an image reference that no longer matches what CI publishes, a
gallery entry naming a screenshot that was renamed, an Umbrel app id that
does not carry the store prefix.

    python scripts/check_packaging.py

Exits non-zero and names each problem. Self-contained; no network.
"""
from __future__ import annotations

import os
import sys
import xml.etree.ElementTree as ET

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMAGE = "ghcr.io/fgilde/museforge:latest"
UNRAID = os.path.join(ROOT, "packaging", "unraid", "museforge.xml")
UMBREL = os.path.join(ROOT, "packaging", "umbrel")
APP_DIR = os.path.join(UMBREL, "gilde-museforge")


def _yaml(path: str) -> dict:
    """Parse with PyYAML when present, else a small subset good enough here.

    CI installs no YAML library for this check, and pulling one in for four
    files would be the expensive way to be sure.
    """
    try:
        import yaml
        with open(path, encoding="utf-8") as handle:
            return yaml.safe_load(handle)
    except ImportError:
        pass
    data: dict = {}
    pending = ""                       # last top-level key, for list items
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            stripped = line.strip()
            # A list item belongs to the key above it. Missing this is what
            # made the check pass locally (PyYAML present) and fail in CI.
            if stripped.startswith("- "):
                if pending:
                    # "key:" was already stored as "" by the branch below;
                    # setdefault would have kept that and swallowed the list.
                    if not isinstance(data.get(pending), list):
                        data[pending] = []
                    data[pending].append(stripped[2:].strip().strip('"').strip("'"))
                continue
            if line[:1].isspace():     # nested mapping — not needed here
                continue
            key, sep, value = line.partition(":")
            if not sep:
                continue
            pending = key.strip()
            value = value.strip().strip('"').strip("'")
            data[pending] = value if value else ""
    return data


def check() -> list[str]:
    problems: list[str] = []

    with open(os.path.join(ROOT, "VERSION"), encoding="utf-8") as handle:
        version = handle.read().strip()

    # ── Unraid ──────────────────────────────────────────────────────────
    if not os.path.exists(UNRAID):
        return [f"missing {UNRAID}"]
    try:
        container = ET.parse(UNRAID).getroot()
    except ET.ParseError as error:
        return [f"unraid template is not valid XML: {error}"]

    for field in ("Name", "Repository", "Overview", "Category", "WebUI",
                  "Icon", "Support", "TemplateURL"):
        if container.find(field) is None or not (container.findtext(field) or "").strip():
            problems.append(f"unraid: <{field}> is missing or empty")

    if container.findtext("Repository", "").strip() != IMAGE:
        problems.append(
            f"unraid: Repository is {container.findtext('Repository')!r}, expected {IMAGE!r}")

    # A CUDA-only app that forgets --runtime=nvidia installs and then fails
    # at the first generation, which reads as a broken app rather than a
    # missing flag.
    if "--runtime=nvidia" not in (container.findtext("ExtraParams") or ""):
        problems.append("unraid: ExtraParams must pass --runtime=nvidia")

    configs = container.findall("Config")
    targets = {one.get("Target") for one in configs}
    for needed in ("NVIDIA_VISIBLE_DEVICES", "NVIDIA_DRIVER_CAPABILITIES",
                   "/workspace/app/ckpts", "/workspace/app/outputs"):
        if needed not in targets:
            problems.append(f"unraid: no Config targets {needed}")
    for one in configs:
        if not one.get("Name") or not one.get("Type"):
            problems.append(f"unraid: a Config is missing Name or Type: {one.attrib}")
        if one.get("Type") == "Path" and one.get("Mode") != "rw":
            problems.append(f"unraid: path {one.get('Target')} is not rw")

    web = container.findtext("WebUI", "")
    port_config = next((one for one in configs if one.get("Type") == "Port"), None)
    if port_config is not None and port_config.get("Target") not in web:
        problems.append(
            f"unraid: WebUI {web!r} does not use the mapped port "
            f"{port_config.get('Target')!r}")

    # ── Umbrel ──────────────────────────────────────────────────────────
    store = _yaml(os.path.join(UMBREL, "umbrel-app-store.yml"))
    app = _yaml(os.path.join(APP_DIR, "umbrel-app.yml"))

    if not str(app.get("id", "")).startswith(f"{store.get('id')}-"):
        problems.append(
            f"umbrel: app id {app.get('id')!r} must start with the store id "
            f"{store.get('id')!r} plus a dash")

    if str(app.get("version", "")).strip('"') != version:
        problems.append(
            f"umbrel: version {app.get('version')!r} does not match VERSION ({version})")

    for field in ("manifestVersion", "category", "name", "tagline",
                  "description", "developer", "website", "repo", "support", "port"):
        if not app.get(field):
            problems.append(f"umbrel: {field} is missing")

    gallery = app.get("gallery") or []
    if isinstance(gallery, list):
        for image in gallery:
            if not os.path.exists(os.path.join(APP_DIR, str(image))):
                problems.append(f"umbrel: gallery names {image}, which is not in the app folder")

    with open(os.path.join(APP_DIR, "docker-compose.yml"), encoding="utf-8") as handle:
        compose = handle.read()
    expected_host = f"APP_HOST: {app.get('id')}_server_1"
    if expected_host not in compose:
        problems.append(f"umbrel: compose must set {expected_host!r}")
    if IMAGE not in compose:
        problems.append(f"umbrel: compose does not use {IMAGE}")
    if "${APP_DATA_DIR}" not in compose:
        problems.append("umbrel: volumes must live under ${APP_DATA_DIR}")
    if "driver: nvidia" not in compose:
        problems.append("umbrel: compose does not reserve an nvidia device")
    if "GPU" not in (app.get("permissions") or []):
        problems.append("umbrel: permissions should declare GPU")

    return problems


def main() -> int:
    problems = check()
    for one in problems:
        print(f"  {one}")
    if problems:
        print(f"\n{len(problems)} problem(s) in packaging/")
        return 1
    print("packaging: Umbrel and Unraid templates are consistent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
