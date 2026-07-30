"""LLM assistance for audiobook projects: chapter splitting and casting.

Two passes, both schema-constrained so the model cannot return anything but
valid JSON on a local llama-server:

- `propose_chapter_split` — where a long text should break into chapters.
- `analyze_chapter` — who speaks which line, with what emotion, and which
  sound effects would suit the scene ("AI Magic" in the reference tool).

Everything the model returns is validated against the project's real ids
before it reaches a caller. That is not defensive politeness: an LLM
inventing a run id would otherwise write a suggestion into a run that does
not exist, silently corrupting the project. Unknown ids are dropped and
counted, never guessed at.
"""

import json
import re

from . import model as ab_model

# ── Schemas ───────────────────────────────────────────────────────────
# Kept small and flat. Deep nesting makes grammar-constrained decoding
# markedly worse at staying on task, and everything here fits in one level.

SPLIT_SCHEMA = {
    "type": "object",
    "properties": {
        "splits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "after_block_id": {"type": "string"},
                    "new_title": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["after_block_id", "new_title"],
            },
        },
    },
    "required": ["splits"],
}

MAGIC_SCHEMA = {
    "type": "object",
    "properties": {
        "characters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "gender": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["name"],
            },
        },
        "assignments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "speaker": {"type": "string"},
                    "emotion": {"type": "string"},
                },
                "required": ["run_id", "speaker"],
            },
        },
        "effects": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "block_id": {"type": "string"},
                    "label": {"type": "string"},
                    "prompt": {"type": "string"},
                    "mode": {"type": "string"},
                    "duration": {"type": "number"},
                },
                "required": ["block_id", "label", "prompt"],
            },
        },
    },
    "required": ["assignments"],
}

ALLOWED_EMOTIONS = {
    "neutral", "happy", "sad", "angry", "fearful", "whispering",
    "excited", "tender", "cheerful", "surprised", "disgusted",
}

_SPLIT_SYSTEM = """You are an audiobook editor deciding where a text breaks into chapters.

You receive numbered paragraphs, each with an id. Propose break points so
each chapter is roughly the target length and every break falls at a real
narrative transition — a scene change, a time jump, a shift of viewpoint.

Rules:
- Break AFTER the paragraph whose id you give, never inside one.
- Never propose a break that would leave a chapter under a third of the target.
- Titles are short and concrete, drawn from what actually happens. No
  numbering ("Chapter 3"), no summaries, no spoilers of a later twist.
- Prefer fewer, well-placed breaks over many mechanical ones. If the text
  genuinely has no good break points, return an empty list."""

_MAGIC_SYSTEM = """You are casting an audio drama from a piece of prose.

You receive paragraphs split into runs. Each run has an id and its text.
Decide for every run who speaks it and, where the writing clearly calls for
it, with what emotion. Optionally suggest sound effects that suit the scene.

Rules:
- Narration is spoken by "narrator". Dialogue is spoken by the character
  saying it — use a consistent name for the same character throughout.
- List every non-narrator character you use in `characters`, once.
- Assign an emotion only when the text supports it. Neutral narration needs
  none, and an emotion on every line reads as melodrama. Use only these:
  {emotions}
- Effects: `mode` is "ambience" for a continuous background (rain, a crowd,
  a machine) or "oneshot" for a single event (a door, a gunshot, glass).
  Write effect prompts in English regardless of the prose language — the
  audio model is trained on English descriptions. Suggest at most one effect
  per paragraph, and only where it adds something.
- Use ONLY the ids you were given. Never invent an id."""


def _strip_thinking(text: str) -> str:
    return re.sub(r"<think(?:ing)?>.*?</think(?:ing)?>", "", text or "",
                  flags=re.DOTALL).strip()


def _parse_json(raw: str) -> dict:
    """Parse a model response, tolerating fences and stray prose.

    Grammar-constrained decoding makes this near-redundant on a local
    server, but remote providers ignore the schema, so the fallback stays.
    """
    text = _strip_thinking(raw)
    if not text:
        return {}
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        candidate = text[start:end + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            try:
                from json_repair import repair_json
                return json.loads(repair_json(candidate))
            except Exception:  # noqa: BLE001
                return {}
    return {}


def _paragraph_digest(chapter, max_chars: int = 24000) -> tuple[str, set]:
    """Numbered paragraph listing for the split pass, plus the valid ids.

    Truncated by character budget rather than paragraph count: what matters
    is fitting the context, and paragraph lengths vary wildly.
    """
    lines, ids, used = [], set(), 0
    for block in chapter.blocks:
        if getattr(block, "type", None) != "paragraph":
            continue
        text = ab_model.chapter_plain_text(
            ab_model.Chapter(id="x", title="", blocks=[block])
        ).strip()
        if not text:
            continue
        # A break decision needs the shape of a paragraph, not all of it.
        preview = text if len(text) <= 400 else text[:400] + "…"
        entry = f"[{block.id}] {preview}"
        if used + len(entry) > max_chars:
            break
        lines.append(entry)
        ids.add(block.id)
        used += len(entry)
    return "\n\n".join(lines), ids


def _run_digest(chapter, max_chars: int = 20000) -> tuple[str, set, set]:
    """Run-level listing for the casting pass, plus valid run and block ids."""
    lines, run_ids, block_ids, used = [], set(), set(), 0
    for block in chapter.blocks:
        if getattr(block, "type", None) != "paragraph" or not block.runs:
            continue
        parts = []
        for run in block.runs:
            text = (run.text or "").strip()
            if not text:
                continue
            parts.append(f"  ({run.id}) {text}")
            run_ids.add(run.id)
        if not parts:
            continue
        entry = f"[{block.id}]\n" + "\n".join(parts)
        if used + len(entry) > max_chars:
            break
        lines.append(entry)
        block_ids.add(block.id)
        used += len(entry)
    return "\n\n".join(lines), run_ids, block_ids


def propose_chapter_split(chapter, target_words: int = 2500, *,
                          generate=None, stream_id: str = "ab-split") -> dict:
    """Ask the LLM where this chapter should break.

    Returns {"splits": [{after_block_id, new_title, reason}], "dropped": n}.
    `generate` is injected so this module never imports llm_service directly
    and stays unit-testable.
    """
    digest, valid_ids = _paragraph_digest(chapter)
    if not digest:
        return {"splits": [], "dropped": 0}

    prompt = (
        f"Target chapter length: about {target_words} words.\n\n"
        f"Paragraphs:\n\n{digest}"
    )
    raw = generate(
        prompt=prompt,
        system_prompt=_SPLIT_SYSTEM,
        json_schema=SPLIT_SCHEMA,
        temperature=0.3,
        max_new_tokens=2048,
        stream_id=stream_id,
    )
    data = _parse_json(raw)

    splits, dropped, seen = [], 0, set()
    for item in (data.get("splits") or []):
        bid = (item or {}).get("after_block_id")
        if bid not in valid_ids or bid in seen:
            dropped += 1
            continue
        seen.add(bid)
        splits.append({
            "after_block_id": bid,
            "new_title": (item.get("new_title") or "Untitled").strip()[:120],
            "reason": (item.get("reason") or "").strip()[:240],
        })
    # Preserve document order regardless of what order the model answered in.
    order = {b.id: i for i, b in enumerate(chapter.blocks)}
    splits.sort(key=lambda s: order.get(s["after_block_id"], 0))
    return {"splits": splits, "dropped": dropped}


def analyze_chapter(project, chapter, *, generate=None,
                    stream_id: str = "ab-magic") -> dict:
    """Suggest speakers, emotions and effects for a chapter.

    Every id is checked against the chapter; unknown ones are dropped and
    reported in `dropped` rather than applied to whatever happens to match.
    """
    digest, valid_runs, valid_blocks = _run_digest(chapter)
    if not digest:
        return {"characters": [], "assignments": [], "effects": [], "dropped": 0}

    existing = [v.name for v in (project.voice_profiles or [])]
    prompt = (
        (f"Voices already cast: {', '.join(existing)}.\n\n" if existing else "")
        + f"Chapter: {chapter.title or 'Untitled'}\n\n{digest}"
    )
    raw = generate(
        prompt=prompt,
        system_prompt=_MAGIC_SYSTEM.format(emotions=", ".join(sorted(ALLOWED_EMOTIONS))),
        json_schema=MAGIC_SCHEMA,
        temperature=0.4,
        max_new_tokens=4096,
        stream_id=stream_id,
    )
    data = _parse_json(raw)
    dropped = 0

    characters, seen_names = [], set()
    for item in (data.get("characters") or []):
        name = ((item or {}).get("name") or "").strip()
        key = name.lower()
        if not name or key in seen_names or key == "narrator":
            continue
        seen_names.add(key)
        characters.append({
            "name": name[:80],
            "gender": ((item.get("gender") or "").strip().lower() or None),
            "description": (item.get("description") or "").strip()[:240],
        })

    assignments, seen_runs = [], set()
    for item in (data.get("assignments") or []):
        rid = (item or {}).get("run_id")
        if rid not in valid_runs or rid in seen_runs:
            dropped += 1
            continue
        seen_runs.add(rid)
        emotion = ((item.get("emotion") or "").strip().lower() or None)
        if emotion in ("neutral", "none", ""):
            emotion = None
        if emotion and emotion not in ALLOWED_EMOTIONS:
            emotion = None  # keep the speaker, drop an invented emotion
        assignments.append({
            "run_id": rid,
            "speaker": ((item.get("speaker") or "narrator").strip() or "narrator")[:80],
            "emotion": emotion,
        })

    effects, seen_blocks = [], set()
    for item in (data.get("effects") or []):
        bid = (item or {}).get("block_id")
        if bid not in valid_blocks or bid in seen_blocks:
            dropped += 1
            continue
        seen_blocks.add(bid)
        mode = ((item.get("mode") or "ambience").strip().lower())
        ambience = mode not in ("oneshot", "one-shot", "single")
        try:
            duration = float(item.get("duration") or (8.0 if ambience else 3.0))
        except (TypeError, ValueError):
            duration = 8.0 if ambience else 3.0
        effects.append({
            "block_id": bid,
            "label": (item.get("label") or "Effect").strip()[:80],
            "prompt": (item.get("prompt") or "").strip()[:400],
            # Ambience loops quietly under the speech; a one-shot interrupts.
            "playback_mode": "parallel" if ambience else "sequential",
            "loop": ambience,
            "volume": 0.3 if ambience else 0.8,
            "duration": max(1.0, min(30.0, duration)),
        })

    return {"characters": characters, "assignments": assignments,
            "effects": effects, "dropped": dropped}


if __name__ == "__main__":
    # Self-check: validation and ordering, with a stubbed LLM.
    ch = ab_model.Chapter(id="c1", title="One", blocks=[
        ab_model.new_paragraph("The rain started at dusk."),
        ab_model.new_paragraph('"We should go," she said.'),
        ab_model.new_paragraph("Nobody moved."),
    ])
    b0, b1, b2 = [b.id for b in ch.blocks]
    r0 = ch.blocks[0].runs[0].id
    project = ab_model.Project(id="p1", title="T", chapters=[ch])

    # -- split: unknown and duplicate ids are dropped, order is restored
    def fake_split(**kw):
        return json.dumps({"splits": [
            {"after_block_id": b2, "new_title": "Later"},
            {"after_block_id": b0, "new_title": "Dusk", "reason": "scene ends"},
            {"after_block_id": "ghost", "new_title": "Nope"},
            {"after_block_id": b0, "new_title": "Duplicate"},
        ]})
    out = propose_chapter_split(ch, generate=fake_split)
    assert [s["after_block_id"] for s in out["splits"]] == [b0, b2], out
    assert out["dropped"] == 2, out
    assert out["splits"][0]["new_title"] == "Dusk"

    # -- fenced output and stray prose still parse
    def fenced(**kw):
        return 'Sure!\n```json\n{"splits": []}\n```'
    assert propose_chapter_split(ch, generate=fenced) == {"splits": [], "dropped": 0}

    # -- thinking blocks are stripped before parsing
    def thinking(**kw):
        return '<think>hmm</think>{"splits": [{"after_block_id": "%s", "new_title": "X"}]}' % b1
    assert len(propose_chapter_split(ch, generate=thinking)["splits"]) == 1

    # -- magic: invented ids dropped, invented emotion dropped but speaker kept
    def fake_magic(**kw):
        return json.dumps({
            "characters": [{"name": "She"}, {"name": "she"}, {"name": "Narrator"}],
            "assignments": [
                {"run_id": r0, "speaker": "narrator", "emotion": "neutral"},
                {"run_id": "ghost", "speaker": "She"},
                {"run_id": ch.blocks[1].runs[0].id, "speaker": "She", "emotion": "furious"},
            ],
            "effects": [
                {"block_id": b0, "label": "Rain", "prompt": "heavy rain", "mode": "ambience"},
                {"block_id": "ghost", "label": "X", "prompt": "y"},
            ],
        })
    magic = analyze_chapter(project, ch, generate=fake_magic)
    assert [c["name"] for c in magic["characters"]] == ["She"], magic["characters"]
    assert magic["dropped"] == 2, magic
    assert magic["assignments"][0]["emotion"] is None, "neutral must not become an override"
    furious = [a for a in magic["assignments"] if a["run_id"] == ch.blocks[1].runs[0].id][0]
    assert furious["emotion"] is None and furious["speaker"] == "She", furious
    rain = magic["effects"][0]
    assert rain["playback_mode"] == "parallel" and rain["loop"] is True, rain

    # -- a one-shot becomes sequential and does not loop
    def oneshot(**kw):
        return json.dumps({"assignments": [], "effects": [
            {"block_id": b1, "label": "Door", "prompt": "door slam", "mode": "oneshot"},
        ]})
    eff = analyze_chapter(project, ch, generate=oneshot)["effects"][0]
    assert eff["playback_mode"] == "sequential" and eff["loop"] is False, eff

    # -- unparseable output degrades to nothing rather than raising
    assert analyze_chapter(project, ch, generate=lambda **kw: "no json here")["assignments"] == []

    print("audiobook.assist self-check: OK")
