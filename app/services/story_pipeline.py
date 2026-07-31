"""Server-side Storywriter pipeline (docs/PLAN-text-audiobook.md §2.3).

Long-form prose generation in a background thread, structured as three LLM
passes per story:

  1. outline    — premise + target length -> structured plan (json_schema)
  2. chapter i  — prose, one call per chapter, high temperature
  3. continuity — re-reads the chapter that was ACTUALLY written and
                  refreshes the running synopsis + character state

The rolling context (outline + running synopsis + character state + the
tail of the previous prose) is the whole point: the full story text is
never sent back to the model, so a 40-chapter book costs the same context
as a 3-chapter one.

Four post-production passes reuse the same plumbing, all additive — a
story written before they existed loads and behaves unchanged:

  - translate  — chapter-by-chapter literary translation into any number
                 of target languages, stored beside the original prose.
  - rewrite    — one marked passage, one instruction, one replacement.
  - write_at   — insert a chapter and have it written to fit the seam.
  - analyze    — one audit pass per chapter, merged server-side into
                 characters / dialogue map / issues / timeline.

Structure deliberately mirrors ``services/director_pipeline.py`` — same
atomic state file, same file-lock-outside-memory-lock save order, same
"cancellation is an absorbing terminal state" update wrapper, same
non-daemon worker so an overnight run survives a browser disconnect, same
``{current,total,message,step,total_steps}`` progress shape so the
existing PipelinePlaceholder renders it unchanged.

Wiring (done by launch.py, not here — this module imports no launch code):
  start_story(params, out_dir, ensure_model=_ensure_llm_loaded)
``ensure_model`` and ``out_dir`` are injected rather than imported so the
worker never reaches back into launch.py.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import traceback
import uuid
from functools import wraps
from typing import Callable, Optional

from services.guide_loader import load_guide

# Optional: the Director's hard safety scan. Applied to the premise and to
# every chapter text exactly as the Director applies it to screenplays.
try:
    from services.director.safety_scan import (
        SafetyViolationError,
        assert_no_minor_content,
    )
    _HAVE_SAFETY_SCAN = True
except Exception:  # pragma: no cover - only when the director package moves
    _HAVE_SAFETY_SCAN = False

    class SafetyViolationError(RuntimeError):  # type: ignore[no-redef]
        pass

    def assert_no_minor_content(text: str, source: str) -> None:  # type: ignore[misc]
        return None

# Optional: json_repair is already an (optional) dependency of the Director
# planners — reused here, not added.
try:
    import json_repair  # type: ignore
except Exception:  # pragma: no cover
    json_repair = None  # type: ignore


# ── Module state ───────────────────────────────────────────────────────────

_stories: dict = {}
_story_lock = threading.Lock()
_story_file_lock = threading.RLock()
_story_threads: dict[str, threading.Thread] = {}
_story_starting: set[str] = set()
_story_operations: set[str] = set()
# Pids whose running synchronous pass has been asked to stop.
_operation_cancels: set[str] = set()
_story_deleting: set[str] = set()

STORY_STATE_VERSION = 1
_STORY_FILE_PREFIX = "_story_"

STATUSES = ("queued", "planning", "writing", "paused", "completed", "failed", "cancelled")
_ACTIVE_STORY_STATUSES = ("queued", "planning", "writing", "paused")

# After a user Stop, only artifacts a late-returning pass legitimately
# produced may still be written. Status, progress, error and completion are
# frozen — otherwise a chapter finishing 30 s after Stop flips the story
# back to "writing" or "completed" and the Stop is lost.
_CANCELLED_ARTIFACT_FIELDS = {
    "chapters",
    "output_files",
    "llm_passes",
    "synopsis_running",
    "character_state",
    "outline",
    "title",
    # A finished analysis of a cancelled story is a legitimate artifact, and
    # analysing one is allowed (it only reads the prose).
    "analysis",
}

# Fields written to _story_{pid}.json. Everything else in the in-memory dict
# (out_dir, the ensure_model callback, the active stream id) is runtime-only.
_STORY_PERSISTED_FIELDS = (
    "version",
    "story_id",
    "created_at",
    "completed_at",
    "status",
    "phase",
    "title",
    "premise",
    "params",
    "outline",
    "chapters",
    "synopsis_running",
    "synopsis_stale",
    "character_state",
    "llm_passes",
    "output_files",
    "progress",
    "error",
    "total_time_sec",
    "_params_snapshot",
    # Derived on every save from the original language + the translations
    # actually present (see _persisted_snapshot).
    "languages",
    # Result of the last analyze_story(), with its own analyzed_at.
    "analysis",
)

# ── Length maths ───────────────────────────────────────────────────────────
# The UI slider is in pages; everything downstream is in words. 275
# words/page is the plan's figure (§2.3) and matches a mass-market paperback.
WORDS_PER_PAGE = 275
# Auto chapter count aims for this; long enough to be a real chapter, short
# enough that one LLM call can hold the whole thing.
AUTO_WORDS_PER_CHAPTER = 1500
MIN_CHAPTER_WORDS = 500
MAX_CHAPTER_WORDS = 4000
# Prose token budget = words * 2 (a conservative English words->tokens
# factor) plus slack for the model's opening throat-clearing. Capped so a
# runaway target can't ask llama-server for a 100k-token completion.
MAX_CHAPTER_TOKENS = 12000
OUTLINE_MAX_TOKENS = 6144
CONTINUITY_MAX_TOKENS = 2048
# The tail of already-written prose handed to the next chapter. Big enough
# to continue a scene seamlessly, small enough that context stays flat.
CONTEXT_TAIL_WORDS = 1500

DEFAULT_PROSE_TEMPERATURE = 0.9
OUTLINE_TEMPERATURE = 0.25
CONTINUITY_TEMPERATURE = 0.2
# A translation and an audit must not invent — near-greedy sampling.
TRANSLATE_TEMPERATURE = 0.3
ANALYZE_TEMPERATURE = 0.2
# Characters of surrounding prose handed to the rewrite pass on each side.
REWRITE_CONTEXT_CHARS = 600
# The head of the FOLLOWING chapter handed to an inserted chapter, so it
# can lead into prose that already exists.
CONTEXT_HEAD_WORDS = 400
# One analysis pass per chapter; a 4000-word chapter is ~24k characters, so
# this truncates only the outliers.
ANALYZE_MAX_CHARS = 24000
ANALYZE_CHAPTER_MAX_TOKENS = 4096
ANALYZE_SUMMARY_MAX_TOKENS = 1024
# The dialogue map is a UI table, not a transcript: bounded per chapter and
# overall, and the result says when it was cut.
ANALYZE_DIALOGUE_PER_CHAPTER = 40
MAX_DIALOGUE_ENTRIES = 200

# Original language of a story when params carry none (every pre-language
# story on disk).
DEFAULT_LANGUAGE = "en"
# Enough to name the common cases in the prompt; anything else is passed
# through as its code, which models handle fine ("write in nl").
_LANGUAGE_NAMES = {
    "en": "English", "de": "German", "fr": "French", "es": "Spanish",
    "it": "Italian", "pt": "Portuguese", "nl": "Dutch", "pl": "Polish",
    "ru": "Russian", "uk": "Ukrainian", "cs": "Czech", "sv": "Swedish",
    "da": "Danish", "no": "Norwegian", "fi": "Finnish", "tr": "Turkish",
    "el": "Greek", "hu": "Hungarian", "ro": "Romanian", "ja": "Japanese",
    "ko": "Korean", "zh": "Chinese", "ar": "Arabic", "he": "Hebrew",
    "hi": "Hindi", "id": "Indonesian", "vi": "Vietnamese", "th": "Thai",
}
# Long writing sessions must not unload the model between chapters —
# reloading 16 GB of weights costs minutes per chapter.
STORY_IDLE_TIMEOUT_S = 1800


class StoryBusyError(RuntimeError):
    """Raised when a mutation conflicts with active story work."""


# ── JSON schemas ───────────────────────────────────────────────────────────
# These are grammar-enforced by llama-server for provider "local"
# (llm_service.generate_streaming(json_schema=...)): the sampler masks every
# token that would break the schema, so the model physically cannot emit
# prose, markdown fences or a repeat loop. On a remote provider the schema
# degrades to a hint, which is why the parser below is still forgiving.
#
# additionalProperties=False matters: a closed object emits each key at most
# once, which makes field-level repetition loops unrepresentable.

# One planned chapter. Referenced by both the outline pass and the
# extend pass so the two can never drift apart.
_CHAPTER_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        # Ordered, concrete, dramatisable events. minItems keeps the model
        # from emitting a single mood line the prose pass can't use.
        "beats": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 2,
            "maxItems": 8,
        },
    },
    "required": ["title", "beats"],
    "additionalProperties": False,
}

OUTLINE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "logline": {"type": "string"},
        "setting": {"type": "string"},
        "characters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "role": {"type": "string"},
                    # Renderable on the page: voice, body, want, secret.
                    "description": {"type": "string"},
                },
                "required": ["name", "description"],
                "additionalProperties": False,
            },
            "minItems": 1,
        },
        "chapters": {
            "type": "array",
            "items": _CHAPTER_PLAN_SCHEMA,
            "minItems": 1,
        },
    },
    "required": ["title", "logline", "characters", "chapters"],
    "additionalProperties": False,
}

# extend_story(): only new chapters, appended after the existing ones.
EXTEND_OUTLINE_SCHEMA = {
    "type": "object",
    "properties": {
        "chapters": {
            "type": "array",
            "items": _CHAPTER_PLAN_SCHEMA,
            "minItems": 1,
        },
    },
    "required": ["chapters"],
    "additionalProperties": False,
}

CONTINUITY_SCHEMA = {
    "type": "object",
    "properties": {
        # Full replacement synopsis covering the whole story so far — the
        # next chapter sees this and the prose tail, nothing else.
        "synopsis": {"type": "string"},
        "characters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "state": {"type": "string"},
                },
                "required": ["name", "state"],
                "additionalProperties": False,
            },
        },
        "open_threads": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["synopsis", "characters"],
    "additionalProperties": False,
}

# analyze_story(): ONE pass per chapter. A whole novel never fits in one
# context, so the model only ever audits the chapter in front of it and
# _merge_chapter_analyses folds the passes together.
_ISSUE_KINDS = ("plot_hole", "continuity", "timeline", "character", "pacing")
_ISSUE_SEVERITIES = ("low", "medium", "high")

ANALYZE_CHAPTER_SCHEMA = {
    "type": "object",
    "properties": {
        "characters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "role": {"type": "string"},
                    "description": {"type": "string"},
                    "traits": {"type": "array", "items": {"type": "string"},
                               "maxItems": 8},
                },
                "required": ["name"],
                "additionalProperties": False,
            },
        },
        "dialogue": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "speaker": {"type": "string"},
                    "line_excerpt": {"type": "string"},
                    "context": {"type": "string"},
                },
                "required": ["speaker", "line_excerpt"],
                "additionalProperties": False,
            },
        },
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": list(_ISSUE_KINDS)},
                    "severity": {"type": "string", "enum": list(_ISSUE_SEVERITIES)},
                    # 1-based in the reply; validated and converted to the
                    # state's 0-based index server-side.
                    "chapter": {"type": "integer"},
                    "description": {"type": "string"},
                    "suggestion": {"type": "string"},
                },
                "required": ["kind", "severity", "description"],
                "additionalProperties": False,
            },
        },
        "when": {"type": "string"},
        "where": {"type": "string"},
        "summary": {"type": "string"},
    },
    "required": ["characters", "dialogue", "issues", "summary"],
    "additionalProperties": False,
}

ANALYZE_SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}


# ── Language ───────────────────────────────────────────────────────────────
# The original language lives in params["language"]; translations live per
# chapter under `translations[code]`. Everything reads through these helpers
# so a story saved before languages existed answers "en" instead of None.

def _lang_code(value) -> str:
    """Normalize a language tag ("DE", "de_DE" -> "de-de"). "" if unusable."""
    code = str(value or "").strip().lower().replace("_", "-")
    return code if re.fullmatch(r"[a-z]{2,3}(-[a-z0-9]{2,8})*", code) else ""


def language_name(code: str) -> str:
    """Human name for a language tag, for the prompt. Falls back to the code."""
    code = _lang_code(code) or DEFAULT_LANGUAGE
    return _LANGUAGE_NAMES.get(code.split("-")[0], code)


def _params_language(params: dict) -> str:
    return _lang_code((params or {}).get("language")) or DEFAULT_LANGUAGE


def _story_language(state: dict) -> str:
    """The language the story itself is written in."""
    return _params_language((state or {}).get("params") or {})


def story_languages(state: dict) -> list[str]:
    """Original language first, then every language a translation exists in."""
    original = _story_language(state)
    extra = set()
    for chapter in (state or {}).get("chapters") or []:
        for code, entry in (chapter.get("translations") or {}).items():
            code = _lang_code(code)
            if code and code != original and isinstance(entry, dict):
                extra.add(code)
    return [original] + sorted(extra)


def _language_directive(code: str) -> str:
    """The block that pins the output language of a pass."""
    code = _lang_code(code) or DEFAULT_LANGUAGE
    name = language_name(code)
    return (
        f"OUTPUT LANGUAGE: {name} ({code}).\n"
        f"Write every word you output in {name} — prose, dialogue, titles, "
        f"summaries and any other text. Never switch language mid-way, never "
        f"add a translation or a note about the language. Proper names that "
        f"the story has already established keep their established spelling."
    )


# ── Guides ─────────────────────────────────────────────────────────────────

def _guide(name: str) -> str:
    """Load a story guide (Markdown under llm_guides/story/)."""
    return load_guide("story", name)


def _content_guidance(nsfw: bool, explicit_guide: str) -> str:
    """Pick the mature or the tame content block for a pass.

    Selection is by the master flag, not by a prompt suffix bolted on at
    call time (§2.5). The tame block reuses the Director's version-
    controlled safety guardrails so both features stay in sync.
    """
    if nsfw:
        return _guide(explicit_guide)
    try:
        from services.director.nsfw_guidance import get_safe_content_guidance
        return get_safe_content_guidance()
    except Exception:
        return "Keep all content PG-13."


def _is_nsfw(params: dict) -> bool:
    """Explicitness is only in play when the master NSFW switch is on."""
    return bool(params.get("nsfw"))


def _system_prompt(base_guide: str, explicit_guide: Optional[str], params: dict,
                   language: Optional[str] = None) -> str:
    """Guide + content guidance + the output language of this pass.

    `explicit_guide=None` skips the content block for passes that generate
    no prose (the analysis pass). `language` overrides the story's own
    language — the translation pass writes in the TARGET language.
    """
    blocks = [_guide(base_guide)]
    if explicit_guide:
        blocks.append(_content_guidance(_is_nsfw(params), explicit_guide))
    blocks.append(_language_directive(language or _params_language(params)))
    return "\n\n".join(b for b in blocks if b).strip()


# ── Length / context helpers (pure — covered by the self-check) ────────────

def total_target_words(min_pages) -> int:
    try:
        pages = max(1, int(min_pages or 1))
    except (TypeError, ValueError):
        pages = 1
    return pages * WORDS_PER_PAGE


def auto_chapter_count(min_pages) -> int:
    """Chapter count when the user let the model decide."""
    return max(3, round(total_target_words(min_pages) / AUTO_WORDS_PER_CHAPTER))


def chapter_target_words(min_pages, chapter_count) -> int:
    """Per-chapter word target, clamped to what one LLM call can hold."""
    try:
        count = max(1, int(chapter_count or 1))
    except (TypeError, ValueError):
        count = 1
    words = total_target_words(min_pages) / count
    return int(max(MIN_CHAPTER_WORDS, min(MAX_CHAPTER_WORDS, round(words))))


def chapter_token_budget(target_words: int) -> int:
    """max_new_tokens for a prose pass — generous, but bounded."""
    return int(min(MAX_CHAPTER_TOKENS, target_words * 2 + 512))


def _word_count(text: str) -> int:
    return len((text or "").split())


def _tail_words(text: str, limit: int = CONTEXT_TAIL_WORDS) -> str:
    words = (text or "").split()
    if len(words) <= limit:
        return (text or "").strip()
    return " ".join(words[-limit:]).strip()


def _head_words(text: str, limit: int = CONTEXT_HEAD_WORDS) -> str:
    words = (text or "").split()
    if len(words) <= limit:
        return (text or "").strip()
    return " ".join(words[:limit]).strip()


def _outline_block(outline: dict) -> str:
    """The story bible, flattened for the prompt."""
    lines = [
        f"Title: {outline.get('title', '')}",
        f"Logline: {outline.get('logline', '')}",
    ]
    if outline.get("setting"):
        lines.append(f"Setting: {outline['setting']}")
    characters = outline.get("characters") or []
    if characters:
        lines.append("Characters:")
        for char in characters:
            role = f" ({char.get('role')})" if char.get("role") else ""
            lines.append(f"- {char.get('name', '?')}{role}: {char.get('description', '')}")
    chapters = outline.get("chapters") or []
    if chapters:
        lines.append("Chapter plan:")
        for i, chapter in enumerate(chapters):
            lines.append(f"  {i + 1}. {chapter.get('title', '')}")
    return "\n".join(lines)


def _character_state_block(character_state: dict) -> str:
    if not character_state:
        return ""
    lines = []
    for name, state in (character_state.get("characters") or {}).items():
        lines.append(f"- {name}: {state}")
    threads = character_state.get("open_threads") or []
    if threads:
        lines.append("Open threads:")
        lines.extend(f"- {t}" for t in threads)
    return "\n".join(lines)


def build_chapter_context(state: dict, index: int, instruction: Optional[str] = None,
                          *, bridge: bool = False) -> str:
    """The user message for one chapter pass.

    Contains the outline, the running synopsis, the character state and the
    TAIL of the prose written so far — never the whole story. Everything
    that makes long stories survivable lives in this function.

    `bridge=True` is the inserted-chapter case (write_chapter_at): the
    chapter after this one already exists, so the head of it goes into the
    context too and the synopsis used is the one recorded when that
    following chapter was written (i.e. the state of the story at this
    point, not after the ending).
    """
    params = state.get("params") or {}
    outline = state.get("outline") or {}
    chapters = state.get("chapters") or []
    chapter = chapters[index] if 0 <= index < len(chapters) else {}
    following = chapters[index + 1] if 0 <= index + 1 < len(chapters) else {}
    total = len(chapters)

    previous_text = "\n\n".join(
        (chapters[i].get("text") or "") for i in range(index)
    )
    tail = _tail_words(previous_text)
    head = _head_words(following.get("text") or "") if bridge else ""

    target = chapter_target_words(params.get("min_pages"), total)

    parts = [
        "=== STORY BIBLE ===",
        _outline_block(outline),
    ]
    synopsis = (state.get("synopsis_running") or "").strip()
    if bridge:
        # The story as it stood BEFORE the following chapter was written.
        synopsis = (following.get("synopsis_at_start") or synopsis or "").strip()
    if synopsis:
        parts += ["", "=== WHAT HAS HAPPENED SO FAR (running synopsis) ===", synopsis]
    char_block = _character_state_block(state.get("character_state") or {})
    if char_block:
        parts += ["", "=== CURRENT CHARACTER STATE ===", char_block]
    if tail:
        parts += [
            "",
            "=== END OF THE PREVIOUS CHAPTER (verbatim — continue seamlessly, "
            "do not repeat it) ===",
            tail,
        ]
    if head:
        parts += [
            "",
            "=== BEGINNING OF THE FOLLOWING CHAPTER (verbatim — lead into it, "
            "do not repeat it, do not resolve what it still treats as open) ===",
            head,
        ]

    beats = chapter.get("beats") or []
    parts += ["", "=== YOUR TASK ==="]
    if bridge:
        title = chapter.get("title") or ""
        parts.append(
            f"Write a NEW chapter {index + 1} of {total} that belongs between the "
            f"previous chapter and the one that follows"
            + (f': "{title}"' if title else ".")
        )
        parts.append(
            "It has to fit the seam: continue from the verbatim end of the "
            "previous chapter and land where the verbatim beginning of the "
            "following chapter starts, so that a reader notices no join."
        )
    else:
        parts.append(f'Write chapter {index + 1} of {total}: "{chapter.get("title", "")}"')
    if beats:
        parts.append("Cover these beats, in this order, as dramatised scenes:")
        parts.extend(f"- {b}" for b in beats)
    elif bridge:
        parts.append(
            "No beats were planned for this chapter — invent the scenes it "
            "needs, but only material that fits between the two chapters you "
            "were given."
        )
    else:
        parts.append(
            "No beats were planned for this chapter — continue the story from "
            "the synopsis and drive it toward the outline's ending."
        )
    style = [
        f"Language: {language_name(_params_language(params))} "
        f"({_params_language(params)})",
        f"Genre: {params.get('genre') or 'unspecified'}",
        f"Tone: {params.get('tone') or 'unspecified'}",
        f"Point of view: {params.get('pov') or 'third person limited'}",
        f"Tense: {params.get('tense') or 'past'}",
        f"Target audience: {params.get('audience') or 'adult general readership'}",
    ]
    parts += ["", *style]
    parts += [
        "",
        f"Target length: about {target} words. Write the full chapter — do not "
        "stop short and do not summarise scenes to save space.",
    ]
    extra = (instruction or chapter.get("instruction") or "").strip()
    if extra:
        parts += ["", f"Additional instruction for this chapter: {extra}"]
    parts += [
        "",
        "Output ONLY the chapter's prose. No title, no heading, no chapter "
        "number, no commentary, no summary at the end.",
    ]
    return "\n".join(parts)


def _continuity_context(state: dict, index: int) -> str:
    chapters = state.get("chapters") or []
    chapter = chapters[index] if 0 <= index < len(chapters) else {}
    parts = []
    synopsis = (state.get("synopsis_running") or "").strip()
    if synopsis:
        parts += ["=== PREVIOUS SYNOPSIS (revise and extend it) ===", synopsis, ""]
    char_block = _character_state_block(state.get("character_state") or {})
    if char_block:
        parts += ["=== PREVIOUS CHARACTER STATE ===", char_block, ""]
    parts += [
        f"=== CHAPTER {index + 1} AS WRITTEN "
        f"({chapter.get('title', '')}) ===",
        chapter.get("text") or "",
        "",
        "Update the record from what this chapter actually says.",
    ]
    return "\n".join(parts)


# ── Export formatting (pure) ───────────────────────────────────────────────

def format_story(state: dict, fmt: str = "md", lang: Optional[str] = None) -> str:
    """Render a story state as Markdown or plain text.

    `lang` other than the story's own language renders the translated
    chapters; chapters with no translation yet are skipped exactly like
    unwritten ones.
    """
    title = state.get("title") or (state.get("outline") or {}).get("title") or "Untitled"
    outline = state.get("outline") or {}
    original = _story_language(state)
    views = [
        (chapter.get("index", position),) + _chapter_view(chapter, lang, original)
        for position, chapter in enumerate(state.get("chapters") or [])
    ]
    views = [v for v in views if (v[2] or "").strip()]
    if fmt == "txt":
        blocks = [title.upper(), ""]
        if outline.get("logline"):
            blocks += [outline["logline"], ""]
        for number, chapter_title, text in views:
            heading = f"CHAPTER {number + 1}"
            if chapter_title:
                heading += f": {chapter_title.upper()}"
            blocks += [heading, "", text.strip(), ""]
        return "\n".join(blocks).rstrip() + "\n"
    blocks = [f"# {title}", ""]
    if outline.get("logline"):
        blocks += [f"*{outline['logline']}*", ""]
    for number, chapter_title, text in views:
        blocks += [f"## {number + 1}. {chapter_title or ''}".rstrip(), ""]
        blocks += [text.strip(), ""]
    return "\n".join(blocks).rstrip() + "\n"


def _safe_slug(title: str) -> str:
    slug = re.sub(r"[^\w\-]+", "_", (title or "").strip()).strip("_")
    return (slug[:48] or "story").lower()


# ── Persistence ────────────────────────────────────────────────────────────

def _write_story_json_unlocked(filepath: str, state: dict) -> None:
    """Atomically replace one story JSON file while its file lock is held."""
    temp_filepath = f"{filepath}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(temp_filepath, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, ensure_ascii=False, default=str)
        os.replace(temp_filepath, filepath)
    finally:
        if os.path.isfile(temp_filepath):
            try:
                os.remove(temp_filepath)
            except OSError:
                pass


def _persisted_snapshot(state: dict) -> dict:
    """Copy the serializable fields out of a live state.

    Lists of dicts are copied one level deep so json.dump can't trip over
    the worker appending a chapter mid-serialization.
    """
    snapshot = {k: state[k] for k in _STORY_PERSISTED_FIELDS if k in state}
    snapshot["chapters"] = [dict(c) for c in (state.get("chapters") or [])]
    snapshot["llm_passes"] = list(state.get("llm_passes") or [])
    snapshot["output_files"] = list(state.get("output_files") or [])
    # Derived, never authored: original language + the translations present.
    snapshot["languages"] = story_languages(state)
    created = state.get("created_at")
    snapshot["total_time_sec"] = (
        (state.get("completed_at") or time.time()) - created if created else None
    )
    return snapshot


def _save_story_state(pid: str) -> bool:
    """Serialize one live story without racing other writers."""
    with _story_file_lock:
        return _save_story_state_locked(pid)


def _save_story_state_locked(pid: str) -> bool:
    # File lock outside, memory lock inside — and the snapshot is built
    # while the memory lock is held, then written outside it (the Director's
    # ordering; the reverse deadlocks against list/load).
    with _story_lock:
        state = _stories.get(pid)
        if not state:
            return False
        out_dir = state.get("out_dir") or "outputs"
        snapshot = _persisted_snapshot(state)
    try:
        os.makedirs(out_dir, exist_ok=True)
        _write_story_json_unlocked(
            os.path.join(out_dir, f"{_STORY_FILE_PREFIX}{pid}.json"), snapshot,
        )
        return True
    except Exception as exc:
        print(f"[Story] Failed to save state for {pid}: {exc}")
        return False


def _scan_dirs(out_dir: str) -> list[str]:
    """out_dir plus its workspace subdirectories."""
    dirs = [out_dir]
    try:
        for name in os.listdir(out_dir):
            sub = os.path.join(out_dir, name)
            if os.path.isdir(sub):
                dirs.append(sub)
    except OSError:
        pass
    return dirs


def list_stories(out_dir: str) -> list[dict]:
    """Summaries of every saved story under out_dir, newest first."""
    results: list[dict] = []
    if not os.path.isdir(out_dir):
        return results
    for scan_dir in _scan_dirs(out_dir):
        try:
            names = os.listdir(scan_dir)
        except OSError:
            continue
        for fname in names:
            if not (fname.startswith(_STORY_FILE_PREFIX) and fname.endswith(".json")):
                continue
            filepath = os.path.join(scan_dir, fname)
            try:
                with _story_file_lock:
                    with open(filepath, "r", encoding="utf-8") as handle:
                        data = json.load(handle)
                    pid = data.get("story_id", "")
                    status = data.get("status", "unknown")
                    # A story whose status says active but which has no live
                    # worker died with the process. Same normalization the
                    # Director does for "running" pipelines.
                    with _story_lock:
                        live = pid in _stories
                    if status in _ACTIVE_STORY_STATUSES and not live:
                        # Say why, or the reader sees "crashed" and no reason.
                        # This death is always the same one: the process went
                        # away mid-run (restart, OOM kill), so the phase it
                        # never came back from is the whole explanation.
                        if not data.get("error"):
                            data["error"] = (
                                "The server stopped while this story was "
                                f"'{data.get('phase') or status}', so the run was "
                                "lost. Nothing is broken — extend it to carry on "
                                "from the last finished chapter, or start over."
                            )
                        data["status"] = status = "crashed"
                        _write_story_json_unlocked(filepath, data)
                chapters = data.get("chapters") or []
                results.append({
                    "id": pid,
                    "status": status,
                    "error": data.get("error") or "",
                    "title": data.get("title") or "",
                    "premise": (data.get("premise") or "")[:200],
                    "created_at": data.get("created_at"),
                    "chapter_count": len(chapters),
                    "chapters_done": sum(
                        1 for c in chapters if c.get("status") == "done"
                    ),
                    "word_count": sum(int(c.get("word_count") or 0) for c in chapters),
                    "output_count": len(data.get("output_files") or []),
                    "workspace": (
                        os.path.basename(scan_dir) if scan_dir != out_dir else "default"
                    ),
                    "_filepath": filepath,
                })
            except Exception:
                pass
    results.sort(key=lambda r: r.get("created_at") or 0, reverse=True)
    return results


def _find_story_file(out_dir: str, pid: str) -> Optional[str]:
    target = f"{_STORY_FILE_PREFIX}{pid}.json"
    for scan_dir in _scan_dirs(out_dir):
        filepath = os.path.join(scan_dir, target)
        if os.path.isfile(filepath):
            return filepath
    return None


def load_story(out_dir: str, pid: str) -> Optional[dict]:
    """Read a saved story state from disk (out_dir or a workspace subdir)."""
    with _story_file_lock:
        filepath = _find_story_file(out_dir, pid)
        if not filepath:
            return None
        try:
            with open(filepath, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except Exception as exc:
            print(f"[Story] Failed to load {pid}: {exc}")
            return None


def _update_saved_story(out_dir: str, pid: str, updater) -> Optional[dict]:
    """Load / mutate / atomically write back one saved story."""
    with _story_file_lock:
        filepath = _find_story_file(out_dir, pid)
        if not filepath:
            return None
        with open(filepath, "r", encoding="utf-8") as handle:
            state = json.load(handle)
        updater(state)
        _write_story_json_unlocked(filepath, state)
        return state


# ── Exclusive-operation claims ─────────────────────────────────────────────

def _claim_story_operation(pid: str) -> bool:
    """Reserve a terminal story for one mutation (edit / export / regen)."""
    with _story_lock:
        if (
            pid in _story_threads
            or pid in _story_starting
            or pid in _story_operations
            or pid in _story_deleting
            or _stories.get(pid, {}).get("status") in _ACTIVE_STORY_STATUSES
        ):
            return False
        _story_operations.add(pid)
        # A stop request from a previous operation must never carry over and
        # abort the next one before it has done anything.
        _operation_cancels.discard(pid)
        return True


def _release_story_operation(pid: str) -> None:
    with _story_lock:
        _story_operations.discard(pid)
        _operation_cancels.discard(pid)


def active_operations() -> list[dict]:
    """Synchronous passes running right now (analysis, translation, rewrite).

    These hold no worker thread, so they are invisible to the status-based
    listing in list_stories — without this the Activity panel could not show
    a running analysis, let alone stop it.
    """
    with _story_lock:
        items = []
        for pid in _story_operations:
            state = _stories.get(pid) or {}
            progress = state.get("progress") or {}
            items.append({
                "id": pid,
                "title": state.get("title") or "Story",
                "message": progress.get("message") or "Working...",
                "step": progress.get("step") or 0,
                "total_steps": progress.get("total_steps") or 0,
                "started_at": state.get("created_at"),
                "cancelling": pid in _operation_cancels,
            })
        return items


def cancel_story_operation(pid: str) -> bool:
    """Ask a running synchronous pass to stop.

    Deliberately not stop_story: the story itself is finished and must keep
    its status. Cancelling the in-flight LLM stream ends the current chapter
    pass within a token, and the pass loop checks operation_cancelled()
    before starting the next one.
    """
    with _story_lock:
        if pid not in _story_operations:
            return False
        _operation_cancels.add(pid)
        stream_id = (_stories.get(pid) or {}).get("_active_stream_id")
    if stream_id:
        try:
            from services import llm_service
            llm_service.cancel_stream(stream_id)
        except Exception:
            pass
    return True


def operation_cancelled(pid: str) -> bool:
    with _story_lock:
        return pid in _operation_cancels


def _exclusive_story_operation(function):
    """Keep a saved-state mutation away from any other story work."""
    @wraps(function)
    def wrapped(out_dir: str, pid: str, *args, **kwargs):
        if not _claim_story_operation(pid):
            raise StoryBusyError("Story is still active; try again shortly.")
        try:
            return function(out_dir, pid, *args, **kwargs)
        finally:
            _release_story_operation(pid)
    return wrapped


def any_story_active() -> bool:
    """True while any story holds a worker, a claim, or an active status."""
    with _story_lock:
        return bool(
            _story_threads or _story_starting or _story_operations or _story_deleting
        ) or any(
            s.get("status") in _ACTIVE_STORY_STATUSES for s in _stories.values()
        )


# ── State updates ──────────────────────────────────────────────────────────

def _update_story(pid: str, **kwargs) -> bool:
    """Thread-safe update; cancellation is an absorbing terminal state.

    Copied semantics from director_pipeline._update_pipeline: once a story
    is cancelled, only artifact fields (the chapter a pass was already
    writing, output files) may still land. No later phase, completion or
    failure may replace the user's Stop.
    """
    with _story_lock:
        state = _stories.get(pid)
        if not state:
            return False
        if state.get("status") == "cancelled":
            if set(kwargs) - _CANCELLED_ARTIFACT_FIELDS:
                return False
        state.update(kwargs)
        return True


def _is_cancelled(pid: str) -> bool:
    with _story_lock:
        return _stories.get(pid, {}).get("status") == "cancelled"


def _snapshot(pid: str) -> Optional[dict]:
    with _story_lock:
        state = _stories.get(pid)
        return dict(state) if state else None


def _progress(current: int, total: int, message: str, step: int = 0,
              total_steps: int = 0) -> dict:
    """EXACTLY the Director's progress shape — PipelinePlaceholder reads it."""
    return {
        "current": current,
        "total": total,
        "message": message,
        "step": step,
        "total_steps": total_steps,
    }


def _writing_progress(chapters: list, message: str, sub: int = 0) -> dict:
    """Chapters in current/total, overall pass count in step/total_steps."""
    total = len(chapters)
    done = sum(1 for c in chapters if c.get("status") == "done")
    return _progress(done, total, message, step=1 + 2 * done + sub,
                     total_steps=1 + 2 * total)


# ── LLM plumbing ───────────────────────────────────────────────────────────

def _model_downloaded(model_id: str) -> bool:
    """Is this registry model's GGUF already on disk?

    llm_service exposes no is_downloaded() helper, so this mirrors the
    cache-path derivation in llm_service.load_model (cache dir +
    cache_dir_override or the repo basename without "-GGUF"). Wrong only in
    the harmless direction: a miss just means the first candidate wins.
    """
    try:
        from services import llm_service
        entry = llm_service.MODEL_REGISTRY.get(model_id)
        if not entry:
            return False
        basename = model_id.split("/")[-1]
        subdir = entry.get("cache_dir_override") or basename.replace("-GGUF", "")
        return os.path.isfile(
            os.path.join(llm_service.get_model_dir(), subdir, entry["gguf_file"]),
        )
    except Exception:
        return False


def _pick_model(use_case: str, override: Optional[str] = None) -> Optional[str]:
    """params override, else the first downloaded candidate, else the first."""
    if override:
        return override
    try:
        from services import llm_service
        candidates = llm_service.models_for_use_case(use_case)
    except Exception:
        return None
    if not candidates:
        return None
    for model_id in candidates:
        if _model_downloaded(model_id):
            return model_id
    return candidates[0]


def _capture_story_pass(pid: str, pass_name: str, model_id: Optional[str]) -> None:
    """Append one pass to the story's LLM log (same fields as the Director).

    Reads llm_service's last-call globals exactly like
    director_pipeline._capture_llm_pass, so the reader can show what the
    model was asked, per pass.
    """
    try:
        from services import llm_service
        entry = {
            "pass": pass_name,
            "model_id": model_id,
            "system_prompt": getattr(llm_service, "_last_system_prompt", "") or "",
            "user_prompt": getattr(llm_service, "_last_user_prompt", "") or "",
            "response_text": getattr(llm_service, "_stream_buffer", "") or "",
            "thinking_text": getattr(llm_service, "_last_thinking_text", None),
            "at": time.time(),
        }
        with _story_lock:
            state = _stories.get(pid)
            if state is not None:
                state.setdefault("llm_passes", []).append(entry)
    except Exception:
        pass


def _ensure_model(pid: str, model_id: Optional[str]) -> None:
    """Invoke the injected loader callback (launch._ensure_llm_loaded)."""
    if not model_id:
        return
    with _story_lock:
        callback: Optional[Callable] = (_stories.get(pid) or {}).get("_ensure_model")
    if callback is None:
        print(f"[Story {pid}] No ensure_model callback — assuming {model_id} is loaded")
        return
    callback(model_id)


def _run_llm(pid: str, pass_name: str, *, model_id: Optional[str],
             system_prompt: str, user_prompt: str, stream_id: str,
             max_new_tokens: int, temperature: float,
             json_schema: Optional[dict] = None) -> str:
    """One LLM pass: load the model, stream, log, return the text."""
    from services import llm_service

    _ensure_model(pid, model_id)
    _update_story(pid, _active_stream_id=stream_id)
    params = (_snapshot(pid) or {}).get("params") or {}
    kwargs = {
        "prompt": user_prompt,
        "system_prompt": system_prompt,
        "max_new_tokens": max_new_tokens,
        "temperature": temperature,
        "top_p": float(params.get("top_p") or 0.95),
        "stream_id": stream_id,
        # Thinking off for every pass: grammar-constrained JSON requires it,
        # and a chapter must not be prefixed with the model's reasoning.
        "thinking_budget": 0,
        "enable_thinking": False,
    }
    if json_schema is not None:
        kwargs["json_schema"] = json_schema
    try:
        text = llm_service.generate_streaming(**kwargs)
    except Exception as exc:
        if json_schema is None:
            raise
        # Never let the grammar make a pass WORSE than it would be without
        # it — a rejecting server (old binary, remote provider) drops the
        # constraint and the forgiving parser takes over.
        print(f"[Story {pid}] Grammar-constrained {pass_name} failed ({exc}); retrying free")
        kwargs.pop("json_schema", None)
        text = llm_service.generate_streaming(**kwargs)
    _capture_story_pass(pid, pass_name, model_id)
    return text or ""


_THINK_TAG_RE = re.compile(
    r"<(think|thinking|seed:think|reasoning|reflection)>.*?(</\1>|$)",
    re.DOTALL | re.IGNORECASE,
)


def _strip_model_noise(text: str) -> str:
    """Remove thinking tags and markdown fences from a raw completion."""
    text = _THINK_TAG_RE.sub("", text or "")
    text = re.sub(r"<\|channel>thought\n.*?(<channel\|>|$)", "", text, flags=re.DOTALL)
    text = re.sub(r"```[a-zA-Z]*\s*", "", text)
    return text.strip()


def _parse_json_object(text: str) -> Optional[dict]:
    """Best-effort dict out of a completion (grammar makes this the fast path)."""
    cleaned = _strip_model_noise(text)
    if not cleaned:
        return None
    candidates = [cleaned]
    braces = re.search(r"\{[\s\S]*\}", cleaned)
    if braces:
        candidates.append(braces.group())
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    if json_repair is not None:
        try:
            parsed = json_repair.loads(cleaned)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return None


def _clean_prose(text: str) -> str:
    """Strip the wrappers models add around a chapter despite instructions."""
    prose = _strip_model_noise(text)
    lines = prose.splitlines()
    # A leading "Chapter 4: The Harbour" / "## The Harbour" heading.
    while lines and (
        re.match(r"^\s*#{1,6}\s", lines[0])
        or re.match(r"^\s*(chapter|kapitel)\s+\w+\s*[:.\-–]?\s*$", lines[0], re.IGNORECASE)
        or re.match(r"^\s*(chapter|kapitel)\s+\w+\s*[:.\-–]\s*.{0,80}$", lines[0], re.IGNORECASE)
    ):
        lines.pop(0)
        while lines and not lines[0].strip():
            lines.pop(0)
    return "\n".join(lines).strip()


def _wait_for_gpu(pid: str, poll_interval: float = 2.0) -> bool:
    """Block until the generation queue is drained. False if cancelled.

    Deliberately NOT director_pipeline._wait_for_gpu: that one reports
    progress into the Director's _pipelines dict and checks the Director's
    cancel flag, so called with a story id it would publish nothing and
    would never notice a story Stop. Only the job registry is reused, and
    read-only — launch.py assigns it on startup, so a None means "no jobs
    can be running yet".
    """
    _update_story(pid, progress=_progress(
        0, 1, "Waiting for GPU (generation queue)...",
    ))
    while True:
        if _is_cancelled(pid):
            return False
        try:
            from services import director_pipeline
            jobs = director_pipeline._jobs
        except Exception:
            jobs = None
        if not jobs:
            return True
        if not [j for j in jobs.values() if j.get("status") in ("queued", "running")]:
            return True
        time.sleep(poll_interval)


# ── Passes ─────────────────────────────────────────────────────────────────

def _pass_outline(pid: str) -> dict:
    """Pass 1 — premise + target length -> structured plan (low temperature)."""
    state = _snapshot(pid) or {}
    params = state.get("params") or {}
    premise = state.get("premise") or ""
    if _HAVE_SAFETY_SCAN:
        assert_no_minor_content(premise, "story premise")

    requested = params.get("chapter_count")
    count = int(requested) if requested else auto_chapter_count(params.get("min_pages"))
    total_words = total_target_words(params.get("min_pages"))
    per_chapter = chapter_target_words(params.get("min_pages"), count)

    user_prompt = "\n".join([
        "=== PREMISE ===",
        premise,
        "",
        "=== REQUIREMENTS ===",
        f"Language: {language_name(_params_language(params))} "
        f"({_params_language(params)}) — the title, the logline, the setting, "
        f"every character description and every chapter title and beat must be "
        f"written in this language",
        f"Genre: {params.get('genre') or 'unspecified'}",
        f"Tone: {params.get('tone') or 'unspecified'}",
        f"Point of view: {params.get('pov') or 'third person limited'}",
        f"Tense: {params.get('tense') or 'past'}",
        f"Target audience: {params.get('audience') or 'adult general readership'}",
        f"Minimum length: {params.get('min_pages') or 1} pages "
        f"(~{total_words} words, ~{WORDS_PER_PAGE} words per page)",
        (
            f"Chapter count: exactly {count}"
            if requested else
            f"Chapter count: you decide — {count} is the recommended number "
            f"for this length"
        ),
        f"Each chapter will be drafted to about {per_chapter} words, so give "
        "every chapter enough beats to fill that.",
        (
            f"Explicitness: {params.get('explicitness')}"
            if _is_nsfw(params) and params.get("explicitness") else ""
        ),
        "",
        "Produce the story blueprint as JSON.",
    ])

    model_id = _pick_model("story_outline", params.get("outline_model"))
    text = _run_llm(
        pid, "outline",
        model_id=model_id,
        system_prompt=_system_prompt("outline", "outline_explicit", params),
        user_prompt=user_prompt,
        stream_id=f"story-{pid}-outline",
        max_new_tokens=OUTLINE_MAX_TOKENS,
        temperature=OUTLINE_TEMPERATURE,
        json_schema=OUTLINE_SCHEMA,
    )
    outline = _parse_json_object(text)
    if not outline or not outline.get("chapters"):
        raise RuntimeError("The outline pass did not return a usable chapter plan.")
    if _HAVE_SAFETY_SCAN:
        assert_no_minor_content(json.dumps(outline, ensure_ascii=False), "story outline")

    chapters = [_new_chapter(i, plan) for i, plan in enumerate(outline["chapters"])]
    _update_story(
        pid,
        outline=outline,
        title=outline.get("title") or state.get("title") or "Untitled",
        chapters=chapters,
    )
    return outline


def _new_chapter(index: int, plan: dict) -> dict:
    return {
        "index": index,
        "title": (plan or {}).get("title") or f"Chapter {index + 1}",
        "beats": list((plan or {}).get("beats") or []),
        "text": "",
        "text_pre_edit": None,
        "word_count": 0,
        "status": "pending",
        "generated_at": None,
        "model_id": None,
        "edited": False,
        "instruction": None,
        "synopsis_at_start": "",
        # {lang: {title, text, translated_at, stale}} — `text`/`title` above
        # always stay the original language.
        "translations": {},
    }


def _chapter_view(chapter: dict, lang: Optional[str], original: str) -> tuple[str, str]:
    """(title, text) of one chapter in `lang`.

    Anything but the original language reads the stored translation; a
    missing translation yields empty text, which callers report as an error
    rather than silently falling back to the original.
    """
    code = _lang_code(lang)
    if code and code != original:
        entry = (chapter.get("translations") or {}).get(code) or {}
        return (entry.get("title") or chapter.get("title") or "",
                entry.get("text") or "")
    return chapter.get("title") or "", chapter.get("text") or ""


def _mark_translations_stale(chapter: dict) -> None:
    """The original prose changed — every translation of it is now behind.

    Only a hint for the UI: the translation stays readable and is never
    thrown away.
    """
    for entry in (chapter.get("translations") or {}).values():
        if isinstance(entry, dict):
            entry["stale"] = True


def _renumber_chapters(chapters: list) -> list:
    """Make every chapter's `index` its list position again.

    `index` is load-bearing in several places — _pending_chapter_indices and
    _refresh_synopsis_if_stale feed it straight back into _pass_chapter /
    _pass_continuity, which index the list with it, and format_story prints
    `index + 1` as the chapter number. Insert and delete must therefore
    renumber, not just reorder.
    """
    for position, chapter in enumerate(chapters):
        chapter["index"] = position
    return chapters


def _insert_into_state(state: dict, at_index: int, title: str = "",
                       text: str = "") -> int:
    """Insert one chapter into a story state dict. Returns its position.

    Keeps `outline["chapters"]` (the plan list, read positionally by
    _outline_block) aligned with the chapter list, and marks the synopsis
    stale only when actual prose came in.
    """
    chapters = [dict(c) for c in (state.get("chapters") or [])]
    at = max(0, min(int(at_index), len(chapters)))
    chapter = _new_chapter(at, {"title": title, "beats": []})
    # _new_chapter's "Chapter N" fallback would be a lie after renumbering.
    chapter["title"] = title or ""
    if (text or "").strip():
        chapter.update({
            "text": text,
            "word_count": _word_count(text),
            "status": "done",
            "generated_at": time.time(),
        })
    chapters.insert(at, chapter)
    state["chapters"] = _renumber_chapters(chapters)

    outline = dict(state.get("outline") or {})
    plans = list(outline.get("chapters") or [])
    if plans:
        plans.insert(min(at, len(plans)), {"title": chapter["title"], "beats": []})
        outline["chapters"] = plans
        state["outline"] = outline
    if (text or "").strip():
        state["synopsis_stale"] = True
    return at


def _delete_from_state(state: dict, index: int) -> bool:
    """Remove one chapter from a story state dict and renumber the rest."""
    chapters = [dict(c) for c in (state.get("chapters") or [])]
    if not 0 <= index < len(chapters):
        return False
    had_text = bool((chapters[index].get("text") or "").strip())
    chapters.pop(index)
    state["chapters"] = _renumber_chapters(chapters)

    outline = dict(state.get("outline") or {})
    plans = list(outline.get("chapters") or [])
    if index < len(plans):
        plans.pop(index)
        outline["chapters"] = plans
        state["outline"] = outline
    if had_text:
        state["synopsis_stale"] = True
    return True


def _pass_extend_outline(pid: str, additional: int) -> None:
    """Plan N more chapters and append them (extend_story)."""
    state = _snapshot(pid) or {}
    params = state.get("params") or {}
    outline = dict(state.get("outline") or {})
    chapters = list(state.get("chapters") or [])

    user_prompt = "\n".join([
        "=== EXISTING STORY BIBLE ===",
        _outline_block(outline),
        "",
        "=== WHAT HAS HAPPENED SO FAR ===",
        (state.get("synopsis_running") or "").strip() or "(nothing written yet)",
        "",
        "=== CURRENT CHARACTER STATE ===",
        _character_state_block(state.get("character_state") or {}) or "(unchanged)",
        "",
        "=== YOUR TASK ===",
        f"Plan {additional} additional chapter(s) that continue this story "
        f"from chapter {len(chapters)} onward. They must follow from what has "
        "already been written, resolve the open threads, and bring the story "
        "to a satisfying end. Same beat rules as before. JSON only.",
    ])

    model_id = _pick_model("story_outline", params.get("outline_model"))
    text = _run_llm(
        pid, f"outline-extend+{additional}",
        model_id=model_id,
        system_prompt=_system_prompt("outline", "outline_explicit", params),
        user_prompt=user_prompt,
        stream_id=f"story-{pid}-outline",
        max_new_tokens=OUTLINE_MAX_TOKENS,
        temperature=OUTLINE_TEMPERATURE,
        json_schema=EXTEND_OUTLINE_SCHEMA,
    )
    parsed = _parse_json_object(text) or {}
    new_plans = (parsed.get("chapters") or [])[:max(1, additional)]
    if not new_plans:
        raise RuntimeError("The extend pass did not return any new chapters.")

    start = len(chapters)
    chapters += [_new_chapter(start + i, plan) for i, plan in enumerate(new_plans)]
    outline["chapters"] = (outline.get("chapters") or []) + list(new_plans)
    _update_story(pid, outline=outline, chapters=chapters)


def _pass_chapter(pid: str, index: int, instruction: Optional[str] = None,
                  *, bridge: bool = False) -> str:
    """Pass 2 — write chapter `index` as prose (high temperature, no JSON).

    `bridge` is the inserted-chapter case: the context then also carries the
    head of the chapter that follows (see build_chapter_context).
    """
    state = _snapshot(pid) or {}
    params = state.get("params") or {}
    chapters = [dict(c) for c in (state.get("chapters") or [])]
    if not 0 <= index < len(chapters):
        raise RuntimeError(f"Chapter {index} does not exist")

    target = chapter_target_words(params.get("min_pages"), len(chapters))
    chapters[index]["status"] = "writing"
    chapters[index]["synopsis_at_start"] = state.get("synopsis_running") or ""
    if instruction:
        chapters[index]["instruction"] = instruction
    _update_story(
        pid,
        chapters=chapters,
        progress=_writing_progress(
            chapters, f"Writing chapter {index + 1}/{len(chapters)}: "
                      f"{chapters[index].get('title', '')}",
        ),
    )

    model_id = _pick_model("story_prose", params.get("prose_model"))
    temperature = float(params.get("temperature") or DEFAULT_PROSE_TEMPERATURE)
    text = _run_llm(
        pid, f"chapter-{index + 1}",
        model_id=model_id,
        system_prompt=_system_prompt("chapter", "chapter_explicit", params),
        user_prompt=build_chapter_context(state, index, instruction, bridge=bridge),
        stream_id=f"story-{pid}-ch{index}",
        max_new_tokens=chapter_token_budget(target),
        temperature=temperature,
    )
    prose = _clean_prose(text)
    if not prose:
        raise RuntimeError(f"Chapter {index + 1} came back empty")
    if _HAVE_SAFETY_SCAN:
        assert_no_minor_content(prose, f"story chapter {index + 1}")

    # Re-read the live chapter list: a manual edit or an extend may have
    # changed it while this pass ran.
    live = [dict(c) for c in ((_snapshot(pid) or {}).get("chapters") or chapters)]
    if index < len(live):
        live[index].update({
            "text": prose,
            "word_count": _word_count(prose),
            # A Stop mid-chapter keeps the partial prose but does not claim
            # the chapter is finished.
            "status": "partial" if _is_cancelled(pid) else "done",
            "generated_at": time.time(),
            "model_id": model_id,
            "edited": False,
            "text_pre_edit": None,
        })
        # New prose for this chapter: any translation of it is now behind.
        _mark_translations_stale(live[index])
    # chapters is an artifact field, so this still lands after a Stop.
    _update_story(pid, chapters=live)
    return prose


def _pass_continuity(pid: str, index: int) -> None:
    """Pass 3 — refresh synopsis + character state from the written text."""
    state = _snapshot(pid) or {}
    params = state.get("params") or {}
    chapters = state.get("chapters") or []
    if not 0 <= index < len(chapters) or not (chapters[index].get("text") or "").strip():
        return

    _update_story(pid, progress=_writing_progress(
        chapters, f"Continuity check after chapter {index + 1}", sub=1,
    ))
    model_id = _pick_model("story_outline", params.get("outline_model"))
    text = _run_llm(
        pid, f"continuity-{index + 1}",
        model_id=model_id,
        system_prompt=_system_prompt("continuity", "continuity", params),
        user_prompt=_continuity_context(state, index),
        stream_id=f"story-{pid}-cont{index}",
        max_new_tokens=CONTINUITY_MAX_TOKENS,
        temperature=CONTINUITY_TEMPERATURE,
        json_schema=CONTINUITY_SCHEMA,
    )
    parsed = _parse_json_object(text)
    if not parsed:
        # Drift-correction is best-effort: a failed continuity pass leaves
        # the previous record in place rather than failing the story.
        print(f"[Story {pid}] Continuity pass {index + 1} returned no usable JSON")
        return
    character_state = {
        "characters": {
            c.get("name"): c.get("state")
            for c in (parsed.get("characters") or [])
            if c.get("name")
        },
        "open_threads": list(parsed.get("open_threads") or []),
    }
    _update_story(
        pid,
        synopsis_running=(parsed.get("synopsis") or "").strip()
        or state.get("synopsis_running") or "",
        character_state=character_state,
        synopsis_stale=False,
    )


def _split_translated(text: str, fallback_title: str) -> tuple[str, str]:
    """Split a "title\\n\\nprose" translation reply into (title, prose).

    The guide demands that shape, but a model that ignores it must not cost
    the user the first paragraph of the chapter — a first line that reads
    like prose (long, or ending in sentence punctuation) is treated as prose
    and the original title is kept.
    # ponytail: heuristic split. A JSON-schema pass would be exact but puts a
    # 2000-word literary translation through grammar-constrained decoding.
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return fallback_title, ""
    head, _, rest = cleaned.partition("\n\n")
    head = head.strip()
    looks_like_title = (
        rest.strip() and "\n" not in head and len(head) <= 120
        and not head.endswith((".", "!", "?", "…", "”", '"', "»"))
    )
    if looks_like_title:
        title = re.sub(r"^(title|titel)\s*[:\-]\s*", "", head, flags=re.IGNORECASE)
        return title.strip(" \"'*#„“»«") or fallback_title, _clean_prose(rest)
    return fallback_title, _clean_prose(cleaned)


def _pass_translate(pid: str, index: int, target_lang: str) -> bool:
    """Translate one chapter into `target_lang`; store it beside the original.

    Own pass, own stream id (``story-{pid}-tr-{lang}-ch{i}``), low
    temperature. The original `text`/`title` are never touched.
    """
    state = _snapshot(pid) or {}
    params = state.get("params") or {}
    chapters = state.get("chapters") or []
    if not 0 <= index < len(chapters):
        return False
    chapter = chapters[index]
    source = (chapter.get("text") or "").strip()
    if not source:
        return False
    source_name = language_name(_story_language(state))
    target_name = language_name(target_lang)

    user_prompt = "\n".join([
        f"Source language: {source_name}",
        f"Target language: {target_name}",
        "",
        f"=== CHAPTER TITLE ({source_name}) ===",
        chapter.get("title") or "(untitled)",
        "",
        f"=== CHAPTER TEXT ({source_name}) ===",
        source,
        "",
        f"Translate the title and the whole chapter into {target_name}. "
        "First line: the translated title only. Then one blank line. Then the "
        "complete translated chapter, and nothing after it.",
    ])
    model_id = _pick_model("story_prose", params.get("prose_model"))
    text = _run_llm(
        pid, f"translate-{target_lang}-{index + 1}",
        model_id=model_id,
        # The target language, not the story's — and the story's own content
        # rules, so an explicit book is not quietly bowdlerised.
        system_prompt=_system_prompt("translate", "chapter_explicit", params,
                                     language=target_lang),
        user_prompt=user_prompt,
        stream_id=f"story-{pid}-tr-{target_lang}-ch{index}",
        # Translations run longer than the original in most language pairs.
        max_new_tokens=chapter_token_budget(int(_word_count(source) * 1.5) + 200),
        temperature=TRANSLATE_TEMPERATURE,
    )
    title, prose = _split_translated(text, chapter.get("title") or "")
    if not prose:
        print(f"[Story {pid}] Translation of chapter {index + 1} came back empty")
        return False

    live = [dict(c) for c in ((_snapshot(pid) or {}).get("chapters") or [])]
    if not 0 <= index < len(live):
        return False
    translations = dict(live[index].get("translations") or {})
    translations[target_lang] = {
        "title": title,
        "text": prose,
        "translated_at": time.time(),
        "stale": False,
    }
    live[index]["translations"] = translations
    # chapters is an artifact field: a translation that lands after a Stop is
    # still a legitimate artifact.
    _update_story(pid, chapters=live)
    return True


def _pass_analyze_chapter(pid: str, index: int, lang: Optional[str]) -> dict:
    """One audit pass over one chapter. Returns the raw parsed reply."""
    state = _snapshot(pid) or {}
    params = state.get("params") or {}
    chapters = state.get("chapters") or []
    if not 0 <= index < len(chapters):
        return {}
    original = _story_language(state)
    title, text = _chapter_view(chapters[index], lang, original)
    if not text.strip():
        return {}
    excerpt = text if len(text) <= ANALYZE_MAX_CHARS else \
        text[:ANALYZE_MAX_CHARS] + "\n\n[... chapter truncated for this pass ...]"
    total = len(chapters)

    user_prompt = "\n".join([
        f"Story: {state.get('title') or '(untitled)'}",
        f"This story has {total} chapter(s).",
        "",
        "=== RUNNING SYNOPSIS OF THE WHOLE STORY (for cross-chapter checks) ===",
        (state.get("synopsis_running") or "").strip() or "(none recorded)",
        "",
        f"=== CHAPTER {index + 1} OF {total}: {title or '(untitled)'} ===",
        excerpt,
        "",
        f"Analyse chapter {index + 1}. Chapter numbers are 1-based and must "
        f"stay within 1..{total}. Return at most "
        f"{ANALYZE_DIALOGUE_PER_CHAPTER} dialogue entries, the most "
        f"significant ones. JSON only.",
    ])
    model_id = _pick_model("story_outline", params.get("outline_model"))
    raw = _run_llm(
        pid, f"analyze-{index + 1}",
        model_id=model_id,
        # No content block: this pass writes no prose, it only reports.
        system_prompt=_system_prompt("analyze", None, params,
                                     language=_lang_code(lang) or original),
        user_prompt=user_prompt,
        stream_id=f"story-{pid}-an-ch{index}",
        max_new_tokens=ANALYZE_CHAPTER_MAX_TOKENS,
        temperature=ANALYZE_TEMPERATURE,
        json_schema=ANALYZE_CHAPTER_SCHEMA,
    )
    return _parse_json_object(raw) or {}


def _merge_chapter_analyses(entries: list, total_chapters: int) -> dict:
    """Fold per-chapter audit passes into one story-level analysis.

    `entries` is [(chapter_index, parsed_reply)] in chapter order. Merge
    rules, all of them server-side because a model's cross-chapter claims
    cannot be trusted:

      characters   keyed by case-folded name; the first non-empty role and
                   description win, `traits` and `chapters` are unioned in
                   first-seen order, `first_chapter`/`last_chapter` are the
                   min/max of the chapters the character was seen in.
      dialogue_map concatenated in chapter order and capped at
                   MAX_DIALOGUE_ENTRIES; `truncated` reports the cut.
      issues       concatenated; `kind` and `severity` are snapped to the
                   known vocabulary (an unknown kind becomes "continuity",
                   an unknown severity "medium"), and `chapter` — the only
                   index the model is free to choose — is validated against
                   the real chapter count. An issue naming a chapter that
                   does not exist is dropped and counted in `dropped_refs`.
      timeline     one entry per analysed chapter; the chapter number comes
                   from the loop, never from the reply.

    Every `chapter` field in the result is a 0-based state index, the same
    numbering as `chapter["index"]`.
    """
    characters: dict = {}
    dialogue: list = []
    issues: list = []
    timeline: list = []
    dropped_refs = 0
    truncated = False

    for index, data in entries:
        data = data if isinstance(data, dict) else {}

        for item in (data.get("characters") or []):
            name = str((item or {}).get("name") or "").strip()
            if not name:
                continue
            key = name.casefold()
            entry = characters.setdefault(key, {
                "name": name[:80], "role": "", "description": "",
                "first_chapter": index, "last_chapter": index,
                "chapters": [], "traits": [],
            })
            entry["role"] = entry["role"] or str(item.get("role") or "").strip()[:60]
            entry["description"] = entry["description"] or \
                str(item.get("description") or "").strip()[:400]
            if index not in entry["chapters"]:
                entry["chapters"].append(index)
            entry["first_chapter"] = min(entry["first_chapter"], index)
            entry["last_chapter"] = max(entry["last_chapter"], index)
            for trait in (item.get("traits") or []):
                trait = str(trait or "").strip()[:60]
                if trait and trait.casefold() not in {
                    t.casefold() for t in entry["traits"]
                }:
                    entry["traits"].append(trait)

        for item in (data.get("dialogue") or [])[:ANALYZE_DIALOGUE_PER_CHAPTER]:
            excerpt = str((item or {}).get("line_excerpt") or "").strip()
            if not excerpt:
                continue
            if len(dialogue) >= MAX_DIALOGUE_ENTRIES:
                truncated = True
                break
            dialogue.append({
                "chapter": index,
                "speaker": (str(item.get("speaker") or "unknown").strip()
                            or "unknown")[:80],
                "line_excerpt": excerpt[:400],
                "context": str(item.get("context") or "").strip()[:240],
            })

        for item in (data.get("issues") or []):
            description = str((item or {}).get("description") or "").strip()
            if not description:
                continue
            raw_chapter = item.get("chapter")
            if raw_chapter is None or str(raw_chapter).strip() == "":
                chapter = index
            else:
                try:
                    # 1-based in the reply, 0-based in the state.
                    chapter = int(str(raw_chapter).strip()) - 1
                except (TypeError, ValueError):
                    dropped_refs += 1
                    continue
                if not 0 <= chapter < total_chapters:
                    dropped_refs += 1
                    continue
            kind = str(item.get("kind") or "").strip().lower()
            severity = str(item.get("severity") or "").strip().lower()
            issues.append({
                "kind": kind if kind in _ISSUE_KINDS else "continuity",
                "severity": severity if severity in _ISSUE_SEVERITIES else "medium",
                "chapter": chapter,
                "description": description[:600],
                "suggestion": str(item.get("suggestion") or "").strip()[:600],
            })

        timeline.append({
            "chapter": index,
            "when": str(data.get("when") or "").strip()[:160] or "unclear",
            "where": str(data.get("where") or "").strip()[:160] or "unclear",
            "summary": str(data.get("summary") or "").strip()[:800],
        })

    order = {"high": 0, "medium": 1, "low": 2}
    issues.sort(key=lambda i: (i["chapter"], order.get(i["severity"], 3)))
    return {
        "characters": sorted(
            characters.values(),
            key=lambda c: (c["first_chapter"], -len(c["chapters"]), c["name"]),
        ),
        "dialogue_map": dialogue,
        "truncated": truncated,
        "issues": issues,
        "timeline": timeline,
        "dropped_refs": dropped_refs,
    }


def _pass_analyze_summary(pid: str, timeline: list, issues: list,
                          lang: Optional[str]) -> str:
    """Final short pass: an overall verdict over the per-chapter notes."""
    state = _snapshot(pid) or {}
    params = state.get("params") or {}
    if not timeline:
        return ""
    notes = "\n".join(
        f"Chapter {entry['chapter'] + 1} ({entry['when']}, {entry['where']}): "
        f"{entry['summary']}"
        for entry in timeline
    )
    problems = "\n".join(
        f"- [{i['severity']}/{i['kind']}] chapter {i['chapter'] + 1}: "
        f"{i['description']}"
        for i in issues[:40]
    ) or "(none reported)"
    user_prompt = "\n".join([
        f"Story: {state.get('title') or '(untitled)'}",
        f"Genre: {params.get('genre') or 'unspecified'}",
        "",
        "=== PER-CHAPTER NOTES ===",
        notes,
        "",
        "=== REPORTED PROBLEMS ===",
        problems,
        "",
        "Write one overall editorial assessment of this manuscript: what it "
        "does well, what its biggest structural weaknesses are, and what to "
        "fix first. Six to twelve sentences. JSON only.",
    ])
    model_id = _pick_model("story_outline", params.get("outline_model"))
    raw = _run_llm(
        pid, "analyze-summary",
        model_id=model_id,
        system_prompt=_system_prompt("analyze", None, params,
                                     language=_lang_code(lang) or _story_language(state)),
        user_prompt=user_prompt,
        stream_id=f"story-{pid}-an-summary",
        max_new_tokens=ANALYZE_SUMMARY_MAX_TOKENS,
        temperature=ANALYZE_TEMPERATURE,
        json_schema=ANALYZE_SUMMARY_SCHEMA,
    )
    parsed = _parse_json_object(raw) or {}
    # A failed verdict pass must not lose the per-chapter work.
    return str(parsed.get("summary") or "").strip()


def _refresh_synopsis_if_stale(pid: str) -> None:
    """Rebuild the record from scratch after a manual edit.

    A user edit invalidates the synopsis (§2.4), and the honest rebuild is
    to re-read every written chapter in order from an empty record.
    # ponytail: O(chapters) low-temperature LLM calls, once per edit session.
    # If that ever hurts, store a per-chapter synopsis snapshot and replay
    # only from the edited chapter (synopsis_at_start already exists).
    """
    state = _snapshot(pid) or {}
    if not state.get("synopsis_stale"):
        return
    written = [
        c["index"] for c in (state.get("chapters") or [])
        if (c.get("text") or "").strip()
    ]
    if not written:
        _update_story(pid, synopsis_stale=False)
        return
    print(f"[Story {pid}] Synopsis is stale — rebuilding over {len(written)} chapters")
    _update_story(pid, synopsis_running="", character_state={})
    for index in written:
        if _is_cancelled(pid):
            return
        _pass_continuity(pid, index)
    _update_story(pid, synopsis_stale=False)


# ── Worker ─────────────────────────────────────────────────────────────────

def _pending_chapter_indices(pid: str) -> list[int]:
    chapters = (_snapshot(pid) or {}).get("chapters") or []
    return [c["index"] for c in chapters if c.get("status") not in ("done",)]


def _finish(pid: str, status: str, message: str, error: Optional[str] = None) -> None:
    chapters = (_snapshot(pid) or {}).get("chapters") or []
    _update_story(
        pid,
        status=status,
        phase=status,
        error=error,
        completed_at=time.time(),
        progress=_writing_progress(chapters, message),
    )


def _insert_chapter_live(pid: str, at_index: int, title: str = "",
                         text: str = "") -> int:
    """Insert a chapter into the LIVE state (worker side). Returns its index."""
    state = _snapshot(pid) or {}
    working = {
        "chapters": state.get("chapters") or [],
        "outline": state.get("outline") or {},
    }
    at = _insert_into_state(working, at_index, title, text)
    # chapters and outline are both artifact fields, so this also lands if
    # the user pressed Stop while the GPU queue was still draining.
    _update_story(pid, chapters=working["chapters"], outline=working["outline"])
    return at


def _run_translation(pid: str, job: dict) -> None:
    """The translate job: chapter by chapter, cancellable between chapters.

    Its own branch of the worker because a translation must not plan, must
    not write prose, and must not rebuild the continuity record — it only
    adds `translations[lang]` to chapters that already exist.
    """
    target = _lang_code(job.get("lang"))
    name = language_name(target)
    chapters = (_snapshot(pid) or {}).get("chapters") or []
    requested = job.get("indices")
    if requested:
        indices = [int(i) for i in requested]
    else:
        indices = [i for i, c in enumerate(chapters) if (c.get("text") or "").strip()]
    indices = [i for i in indices if 0 <= i < len(chapters)]
    if not target:
        _finish(pid, "failed", "No target language", error="No target language.")
        return
    if not indices:
        _finish(pid, "failed", f"Nothing to translate to {name}",
                error="No written chapters to translate.")
        return

    _update_story(pid, status="writing", phase="writing")
    done = 0
    for position, index in enumerate(indices):
        if _is_cancelled(pid):
            return
        _update_story(pid, progress=_progress(
            position, len(indices),
            f"Translating chapter {index + 1} of {len(chapters)} into {name}...",
            step=position, total_steps=len(indices),
        ))
        if _pass_translate(pid, index, target):
            done += 1
        _save_story_state(pid)
    if _is_cancelled(pid):
        return
    _finish(pid, "completed", f"Translated {done} chapter(s) into {name}")


def _run_story(pid: str, job: dict) -> None:
    """The story worker. One per story, non-daemon (survives a disconnect)."""
    llm_service = None
    try:
        from services import llm_service as _llm
        llm_service = _llm
        # Keep the model warm between chapters; restored in the finally.
        llm_service.set_idle_timeout(STORY_IDLE_TIMEOUT_S)
    except Exception as exc:
        print(f"[Story {pid}] Could not set the LLM idle timeout: {exc}")

    try:
        if not _wait_for_gpu(pid):
            return
        if _is_cancelled(pid):
            return

        kind = job.get("kind", "new")

        if kind == "translate":
            _run_translation(pid, job)
            return

        # Before anything reads the record: a manual edit since the last run
        # invalidated it. No-op on a fresh story (nothing written yet).
        _update_story(pid, status="writing", phase="writing")
        _refresh_synopsis_if_stale(pid)
        if _is_cancelled(pid):
            return

        if kind == "extend":
            _update_story(pid, status="planning", phase="planning",
                          progress=_progress(0, 1, "Planning new chapters..."))
            _pass_extend_outline(pid, int(job.get("count") or 1))
            _save_story_state(pid)
        elif kind == "write_at":
            # Make room first: the chapter has to exist before it can be
            # written, and the pass needs its neighbours in place.
            at = _insert_chapter_live(pid, int(job.get("at_index") or 0))
            job = dict(job, indices=[at],
                       instruction=(job.get("brief") or "").strip() or None)
            _save_story_state(pid)
        elif not ((_snapshot(pid) or {}).get("outline") or {}).get("chapters"):
            _update_story(pid, status="planning", phase="planning",
                          progress=_progress(0, 1, "Planning the story..."))
            _pass_outline(pid)
            _save_story_state(pid)

        if _is_cancelled(pid):
            return
        _update_story(pid, status="writing", phase="writing")

        indices = list(job.get("indices") or _pending_chapter_indices(pid))
        for index in indices:
            if _is_cancelled(pid):
                return
            _pass_chapter(pid, index, job.get("instruction"),
                          bridge=(kind == "write_at"))
            if _is_cancelled(pid):
                return
            _pass_continuity(pid, index)
            _save_story_state(pid)

        # A regenerated middle chapter — or a freshly inserted one —
        # invalidates the record built without it; replay continuity over the
        # chapters that follow it.
        if kind in ("regenerate", "write_at") and indices:
            for index in range(max(indices) + 1, len(
                (_snapshot(pid) or {}).get("chapters") or [],
            )):
                if _is_cancelled(pid):
                    return
                _pass_continuity(pid, index)
            _save_story_state(pid)

        if _is_cancelled(pid):
            return
        state = _snapshot(pid) or {}
        words = sum(int(c.get("word_count") or 0) for c in state.get("chapters") or [])
        _finish(pid, "completed", f"Story complete — {words} words")

    except SafetyViolationError as exc:
        print(f"[Story {pid}] Safety violation: {exc}")
        _finish(pid, "failed", "Blocked by the content safety scan", error=str(exc))
    except Exception as exc:
        print(f"[Story {pid}] Failed: {exc}")
        traceback.print_exc()
        _finish(pid, "failed", "Story generation failed", error=str(exc))
    finally:
        if llm_service is not None:
            try:
                llm_service.set_idle_timeout(None)
            except Exception:
                pass
        _update_story(pid, _active_stream_id=None)
        _save_story_state(pid)
        with _story_lock:
            if _story_threads.get(pid) is threading.current_thread():
                _story_threads.pop(pid, None)


def _start_worker(pid: str, job: dict) -> None:
    """Start and track a story worker until its ``finally`` completes."""
    thread = threading.Thread(
        target=_run_story, args=(pid, job), daemon=False,
    )
    with _story_lock:
        if pid in _story_threads:
            raise RuntimeError(f"Story {pid} already has a worker")
        _story_threads[pid] = thread
    try:
        thread.start()
    except BaseException as exc:
        with _story_lock:
            if _story_threads.get(pid) is thread:
                _story_threads.pop(pid, None)
            state = _stories.get(pid)
            if state and state.get("status") not in ("completed", "failed", "cancelled"):
                state.update({
                    "status": "failed",
                    "phase": "failed",
                    "error": f"Could not start story worker: {exc}",
                    "completed_at": time.time(),
                    "progress": _progress(0, 0, "Could not start story worker"),
                })
        _save_story_state(pid)
        raise


# ── Params ─────────────────────────────────────────────────────────────────

def _normalized_params(raw: dict) -> dict:
    """The knobs the passes read, defaulted. `_params_snapshot` keeps the rest."""
    chapter_count = raw.get("chapter_count")
    if isinstance(chapter_count, str):
        chapter_count = None if chapter_count.strip().lower() in ("", "auto") \
            else int(chapter_count)
    return {
        # Original language of the story; every pass writes in it unless it
        # is a translation pass. Never changes after the story is created.
        "language": _lang_code(raw.get("language")) or DEFAULT_LANGUAGE,
        "genre": raw.get("genre") or "",
        "tone": raw.get("tone") or "",
        "pov": raw.get("pov") or "third person limited",
        "tense": raw.get("tense") or "past",
        "audience": raw.get("audience") or "adult general readership",
        "min_pages": int(raw.get("min_pages") or 20),
        # None means "let the model decide" — auto_chapter_count() answers.
        "chapter_count": int(chapter_count) if chapter_count else None,
        "explicitness": raw.get("explicitness") or "none",
        "nsfw": bool(raw.get("nsfw")),
        "temperature": float(raw.get("temperature") or DEFAULT_PROSE_TEMPERATURE),
        "top_p": float(raw.get("top_p") or 0.95),
        "outline_model": raw.get("outline_model") or None,
        "prose_model": raw.get("prose_model") or None,
    }


# ── Public API ─────────────────────────────────────────────────────────────

def start_story(params: dict, out_dir: str,
                ensure_model: Optional[Callable[[str], None]] = None) -> str:
    """Start a new story run. Returns the story id.

    Args:
        params: the request dict — premise, min_pages, genre, tone, pov,
            tense, audience, chapter_count|None, explicitness, nsfw,
            temperature, top_p, outline_model, prose_model. Kept verbatim
            in `_params_snapshot` so "new story with these settings" and
            "show me the exact prompt used" are reads, not new plumbing.
        out_dir: workspace output dir; the state file and exports land here.
        ensure_model: callback that loads a model id (launch._ensure_llm_loaded).
            Injected so the worker never imports launch.py.
    """
    pid = uuid.uuid4().hex[:8]
    normalized = _normalized_params(params)
    state = {
        "version": STORY_STATE_VERSION,
        "story_id": pid,
        "created_at": time.time(),
        "completed_at": None,
        "status": "queued",
        "phase": "queued",
        "title": params.get("title") or "",
        "premise": params.get("premise") or "",
        "params": normalized,
        "outline": {},
        "chapters": [],
        "synopsis_running": "",
        "synopsis_stale": False,
        "character_state": {},
        "llm_passes": [],
        "output_files": [],
        "progress": _progress(0, 0, "Queued..."),
        "error": None,
        "total_time_sec": None,
        "_params_snapshot": dict(params),
        # Runtime-only (never persisted).
        "out_dir": out_dir,
        "_ensure_model": ensure_model,
        "_active_stream_id": None,
    }
    with _story_lock:
        _stories[pid] = state
    _save_story_state(pid)
    _start_worker(pid, {"kind": "new"})
    return pid


def get_story(pid: str) -> Optional[dict]:
    """Live in-memory state — same shape as the JSON on disk.

    Runtime-only keys (out_dir, the ensure_model callback, the active stream
    id) are dropped by going through the persisted-field projection, so the
    endpoint can return this verbatim.
    """
    with _story_lock:
        state = _stories.get(pid)
        return _persisted_snapshot(state) if state else None


def stop_story(pid: str) -> bool:
    """User Stop. Absorbing: nothing may flip the story back afterwards."""
    with _story_lock:
        state = _stories.get(pid)
        if not state or state.get("status") in ("completed", "failed", "cancelled"):
            return False
        state.update({
            "status": "cancelled",
            "phase": "cancelled",
            "completed_at": time.time(),
            "progress": _progress(0, 0, "Cancelled"),
        })
        stream_id = state.get("_active_stream_id")
    # Stop the in-flight pass within a token instead of at the next chapter.
    if stream_id:
        try:
            from services import llm_service
            llm_service.cancel_stream(stream_id)
        except Exception:
            pass
    _save_story_state(pid)
    return True


def delete_story(out_dir: str, pid: str) -> dict:
    """Delete a saved story and the exports it produced."""
    with _story_lock:
        if (
            pid in _story_threads
            or pid in _story_starting
            or pid in _story_operations
            or pid in _story_deleting
            or _stories.get(pid, {}).get("status") in _ACTIVE_STORY_STATUSES
        ):
            return {"ok": False, "error": "running"}
        _story_deleting.add(pid)
    try:
        with _story_file_lock:
            filepath = _find_story_file(out_dir, pid)
            if not filepath:
                return {"ok": False, "error": "not_found"}
            story_dir = os.path.dirname(filepath)
            removed = []
            try:
                with open(filepath, "r", encoding="utf-8") as handle:
                    state = json.load(handle)
            except Exception:
                state = {}
            for name in state.get("output_files") or []:
                # Basenames only — never follow a path out of the workspace.
                if not name or os.path.basename(name) != name:
                    continue
                target = os.path.join(story_dir, name)
                try:
                    if os.path.isfile(target):
                        os.remove(target)
                        removed.append(name)
                except OSError:
                    pass
            os.remove(filepath)
        with _story_lock:
            _stories.pop(pid, None)
        return {"ok": True, "deleted_files": removed}
    finally:
        with _story_lock:
            _story_deleting.discard(pid)


def _rehydrate(pid: str, out_dir: str,
               ensure_model: Optional[Callable[[str], None]],
               *, reset_run_state: bool = True) -> tuple[bool, str]:
    """Put a saved story back in memory so a worker can continue it.

    `reset_run_state=False` is for the synchronous operations (analyse,
    rewrite): they need the story in memory for the LLM helpers but must not
    erase the completion it already reported.
    """
    saved = load_story(out_dir, pid)
    if not saved:
        return False, "No saved state found for this story."
    filepath = _find_story_file(out_dir, pid)
    with _story_lock:
        existing = _stories.get(pid) or {}
        state = {k: saved.get(k) for k in _STORY_PERSISTED_FIELDS}
        state["story_id"] = pid
        state["version"] = saved.get("version") or STORY_STATE_VERSION
        state["created_at"] = saved.get("created_at") or time.time()
        state["chapters"] = [dict(c) for c in (saved.get("chapters") or [])]
        state["llm_passes"] = list(saved.get("llm_passes") or [])
        state["output_files"] = list(saved.get("output_files") or [])
        state["params"] = saved.get("params") or {}
        state["out_dir"] = os.path.dirname(filepath) if filepath else out_dir
        state["_ensure_model"] = ensure_model or existing.get("_ensure_model")
        state["_active_stream_id"] = None
        if reset_run_state:
            state["error"] = None
            state["completed_at"] = None
        _stories[pid] = state
    return True, "ok"


def _for_synchronous_pass(pid: str, out_dir: Optional[str],
                          ensure_model: Optional[Callable[[str], None]],
                          ) -> tuple[bool, str]:
    """Claim a terminal story and load it, for a pass that runs in-thread.

    Caller MUST call _release_story_operation(pid) when done. Used by
    analyze_story and rewrite_passage: both need the story in `_stories` so
    the shared LLM helpers can reach params and the ensure_model callback,
    but neither starts a worker.
    """
    with _story_lock:
        resolved = out_dir or (_stories.get(pid) or {}).get("out_dir")
    if not resolved:
        return False, "No output directory for this story."
    if not _claim_story_operation(pid):
        return False, "Story is still active; try again shortly."
    ok, why = _rehydrate(pid, resolved, ensure_model, reset_run_state=False)
    if not ok:
        _release_story_operation(pid)
        return False, why
    return True, resolved


def _start_continuation(pid: str, job: dict, out_dir: Optional[str],
                        ensure_model: Optional[Callable[[str], None]],
                        message: str) -> tuple[bool, str]:
    """Shared reservation + rehydrate + worker start for regen / extend."""
    with _story_lock:
        state = _stories.get(pid)
        if (
            pid in _story_threads
            or pid in _story_starting
            or pid in _story_operations
            or pid in _story_deleting
            or (state and state.get("status") in _ACTIVE_STORY_STATUSES)
        ):
            return False, "Story is still active; try again shortly."
        resolved_out_dir = out_dir or (state or {}).get("out_dir")
        _story_starting.add(pid)
    try:
        if not resolved_out_dir:
            return False, "No output directory for this story."
        ok, why = _rehydrate(pid, resolved_out_dir, ensure_model)
        if not ok:
            return False, why
        _update_story(pid, status="queued", phase="queued", error=None,
                      completed_at=None, progress=_progress(0, 1, message))
        _save_story_state(pid)
        _start_worker(pid, job)
        return True, "started"
    finally:
        with _story_lock:
            _story_starting.discard(pid)


def regenerate_chapter(pid: str, index: int, instruction: Optional[str] = None,
                       out_dir: Optional[str] = None,
                       ensure_model: Optional[Callable[[str], None]] = None,
                       ) -> tuple[bool, str]:
    """Rewrite one chapter, optionally with an instruction ("darker").

    The continuity record is replayed over the chapters that follow, so a
    regenerated middle chapter doesn't leave the synopsis describing the
    version it replaced.
    """
    index = int(index)
    # Validate against the saved state BEFORE a worker exists, so a bad
    # index is a plain error instead of a story that starts and stops.
    known = get_story(pid) or load_story(out_dir or "", pid) or {}
    if known and not 0 <= index < len(known.get("chapters") or []):
        return False, f"Chapter {index + 1} does not exist."
    return _start_continuation(
        pid,
        {"kind": "regenerate", "indices": [index], "instruction": instruction},
        out_dir, ensure_model, f"Rewriting chapter {index + 1}...",
    )


def extend_story(pid: str, additional_chapters: int,
                 out_dir: Optional[str] = None,
                 ensure_model: Optional[Callable[[str], None]] = None,
                 ) -> tuple[bool, str]:
    """Plan and write N more chapters, continuing from the current synopsis."""
    count = max(1, int(additional_chapters or 1))
    return _start_continuation(
        pid, {"kind": "extend", "count": count}, out_dir, ensure_model,
        f"Extending the story by {count} chapter(s)...",
    )


def _sync_live_from_saved(pid: str, saved: dict) -> None:
    """Push a saved-state mutation into the in-memory copy, if there is one."""
    with _story_lock:
        live = _stories.get(pid)
        if live is None:
            return
        live["chapters"] = [dict(c) for c in (saved.get("chapters") or [])]
        live["outline"] = saved.get("outline") or live.get("outline") or {}
        live["synopsis_stale"] = bool(saved.get("synopsis_stale"))


def _apply_chapter_text(out_dir: str, pid: str, index: int, text: str,
                        lang: Optional[str] = None,
                        title: Optional[str] = None) -> bool:
    """Write one chapter's text in one language. No operation claim of its own.

    Original language: replaces the prose, keeps the model's version in
    `text_pre_edit` (first edit only), marks the synopsis stale and marks
    every translation of the chapter stale.
    Any other language: replaces that translation only; the original prose
    and the synopsis are untouched.
    """
    def updater(state: dict) -> None:
        chapters = state.get("chapters") or []
        if not 0 <= index < len(chapters):
            raise IndexError(f"Chapter {index} does not exist")
        chapter = chapters[index]
        original = _story_language(state)
        code = _lang_code(lang)
        if code and code != original:
            translations = chapter.setdefault("translations", {})
            entry = translations.setdefault(code, {})
            entry.update({
                "title": title if title is not None
                else (entry.get("title") or chapter.get("title") or ""),
                "text": text or "",
                "translated_at": time.time(),
                # The user has taken ownership of this translation, so it is
                # no longer "behind the original" as far as the UI goes.
                "stale": False,
            })
            return
        if chapter.get("text_pre_edit") is None:
            chapter["text_pre_edit"] = chapter.get("text") or ""
        chapter["text"] = text or ""
        chapter["word_count"] = _word_count(text)
        chapter["edited"] = True
        chapter["status"] = "done" if (text or "").strip() else "pending"
        if title is not None:
            chapter["title"] = title
        state["synopsis_stale"] = True
        _mark_translations_stale(chapter)

    saved = _update_saved_story(out_dir, pid, updater)
    if saved is None:
        return False
    _sync_live_from_saved(pid, saved)
    return True


@_exclusive_story_operation
def update_chapter_text(out_dir: str, pid: str, index: int, text: str,
                        lang: Optional[str] = None) -> bool:
    """Save a manual edit of one chapter, in the original or a translation.

    Original language (`lang` None or the story's own language): the user's
    text replaces the generated prose, the model's version is kept in
    `text_pre_edit` (first edit only), the synopsis is marked stale so the
    next run rebuilds it from what is actually on the page
    (`_refresh_synopsis_if_stale`), and every translation of this chapter is
    marked `stale` — a hint for the UI, nothing is deleted.

    Any other language: only `translations[lang]` changes.
    """
    return _apply_chapter_text(out_dir, pid, index, text, lang)


@_exclusive_story_operation
def insert_chapter(out_dir: str, pid: str, at_index: int, title: str = "",
                   text: str = "") -> bool:
    """Insert an empty (or given) chapter at `at_index` and renumber the rest.

    `at_index` is clamped to 0..len(chapters), so len() appends. The outline's
    plan list is kept aligned and the new chapter gets no beats.
    """
    def updater(state: dict) -> None:
        _insert_into_state(state, at_index, title, text)

    saved = _update_saved_story(out_dir, pid, updater)
    if saved is None:
        return False
    _sync_live_from_saved(pid, saved)
    return True


@_exclusive_story_operation
def delete_chapter(out_dir: str, pid: str, index: int) -> bool:
    """Delete one chapter and renumber the rest. False if it does not exist."""
    removed = []

    def updater(state: dict) -> None:
        removed.append(_delete_from_state(state, index))

    saved = _update_saved_story(out_dir, pid, updater)
    if saved is None or not removed or not removed[0]:
        return False
    _sync_live_from_saved(pid, saved)
    return True


def write_chapter_at(pid: str, at_index: int, brief: str = "",
                     out_dir: Optional[str] = None,
                     ensure_model: Optional[Callable[[str], None]] = None,
                     ) -> tuple[bool, str]:
    """Insert a chapter at `at_index` and have the model write it.

    The pass sees the outline, the story as it stood at that point, the end
    of the previous chapter AND the beginning of the following one, so the
    new chapter fits the seam instead of restarting the story
    (`build_chapter_context(..., bridge=True)`). Afterwards the continuity
    record is replayed over the chapters that follow, reusing the same
    replay a mid-story regeneration triggers.
    """
    at_index = int(at_index)
    known = get_story(pid) or load_story(out_dir or "", pid) or {}
    if known and not 0 <= at_index <= len(known.get("chapters") or []):
        return False, f"Cannot insert a chapter at position {at_index + 1}."
    return _start_continuation(
        pid,
        {"kind": "write_at", "at_index": at_index, "brief": brief or ""},
        out_dir, ensure_model, f"Writing a new chapter {at_index + 1}...",
    )


def translate_story(pid: str, target_lang: str, out_dir: Optional[str] = None,
                    ensure_model: Optional[Callable[[str], None]] = None,
                    ) -> tuple[bool, str]:
    """Translate every written chapter into `target_lang` in a worker thread.

    One pass per chapter, stored under `chapter["translations"][lang]`; the
    original prose is never touched. Cancellable between chapters like any
    other story run.
    """
    code = _lang_code(target_lang)
    if not code:
        return False, f"'{target_lang}' is not a usable language code."
    known = get_story(pid) or load_story(out_dir or "", pid) or {}
    if known and code == _story_language(known):
        return False, f"The story is already written in {language_name(code)}."
    return _start_continuation(
        pid, {"kind": "translate", "lang": code}, out_dir, ensure_model,
        f"Translating the story into {language_name(code)}...",
    )


def retranslate_chapter(pid: str, index: int, target_lang: str,
                        out_dir: Optional[str] = None,
                        ensure_model: Optional[Callable[[str], None]] = None,
                        ) -> tuple[bool, str]:
    """Translate one chapter again (e.g. after the original was edited)."""
    index = int(index)
    code = _lang_code(target_lang)
    if not code:
        return False, f"'{target_lang}' is not a usable language code."
    known = get_story(pid) or load_story(out_dir or "", pid) or {}
    if known and not 0 <= index < len(known.get("chapters") or []):
        return False, f"Chapter {index + 1} does not exist."
    if known and code == _story_language(known):
        return False, f"The story is already written in {language_name(code)}."
    return _start_continuation(
        pid, {"kind": "translate", "lang": code, "indices": [index]},
        out_dir, ensure_model,
        f"Translating chapter {index + 1} into {language_name(code)}...",
    )


def rewrite_passage(pid: str, index: int, selected_text: str, instruction: str,
                    lang: Optional[str] = None, out_dir: Optional[str] = None,
                    ensure_model: Optional[Callable[[str], None]] = None,
                    ) -> dict:
    """Rewrite one marked passage of a chapter. Returns the replacement only.

    Returns {"ok", "replacement", "before", "after"} — `before`/`after` are
    the ~REWRITE_CONTEXT_CHARS of prose on either side that the model was
    given, so the caller can show the seam. Nothing is written: applying it
    is a separate call (`apply_passage_rewrite`).

    The passage is located by EXACT match in the chapter text of the given
    language. Not found, or found more than once, is an error with a clear
    reason — never a guess at which occurrence was meant.
    """
    def fail(reason: str, **extra) -> dict:
        return {"ok": False, "error": reason, "replacement": "",
                "before": "", "after": "", **extra}

    index = int(index)
    selected_text = selected_text or ""
    if not selected_text.strip():
        return fail("No passage was selected.")
    if not (instruction or "").strip():
        return fail("No rewrite instruction was given.")

    ok, resolved = _for_synchronous_pass(pid, out_dir, ensure_model)
    if not ok:
        return fail(resolved)
    try:
        state = _snapshot(pid) or {}
        chapters = state.get("chapters") or []
        if not 0 <= index < len(chapters):
            return fail(f"Chapter {index + 1} does not exist.")
        original = _story_language(state)
        code = _lang_code(lang) or original
        title, text = _chapter_view(chapters[index], code, original)
        if not text.strip():
            return fail(
                f"Chapter {index + 1} has no text in {language_name(code)}."
                if code != original else f"Chapter {index + 1} is empty."
            )
        occurrences = text.count(selected_text)
        if occurrences == 0:
            return fail(
                "The selected passage was not found in chapter "
                f"{index + 1} ({language_name(code)}). It may have been edited "
                "since — reload the chapter and select it again.",
                occurrences=0,
            )
        if occurrences > 1:
            return fail(
                f"The selected passage appears {occurrences} times in chapter "
                f"{index + 1}. Extend the selection so it is unique.",
                occurrences=occurrences,
            )

        start = text.index(selected_text)
        end = start + len(selected_text)
        before = text[max(0, start - REWRITE_CONTEXT_CHARS):start]
        after = text[end:end + REWRITE_CONTEXT_CHARS]

        params = state.get("params") or {}
        user_prompt = "\n".join([
            f'Story: {state.get("title") or "(untitled)"} — chapter '
            f'{index + 1} of {len(chapters)}: {title}',
            f"Language: {language_name(code)} ({code})",
            f"Genre: {params.get('genre') or 'unspecified'}",
            f"Tone: {params.get('tone') or 'unspecified'}",
            f"Point of view: {params.get('pov') or 'third person limited'}",
            f"Tense: {params.get('tense') or 'past'}",
            f"Target audience: {params.get('audience') or 'adult general readership'}",
            "",
            "=== CONTEXT BEFORE THE PASSAGE (do not rewrite, do not repeat) ===",
            before or "(the passage starts the chapter)",
            "",
            "=== THE MARKED PASSAGE (rewrite exactly this) ===",
            selected_text,
            "",
            "=== CONTEXT AFTER THE PASSAGE (do not rewrite, do not repeat) ===",
            after or "(the passage ends the chapter)",
            "",
            "=== THE AUTHOR'S INSTRUCTION ===",
            instruction.strip(),
            "",
            f"The marked passage is {_word_count(selected_text)} words long. "
            "Output only the replacement for it.",
        ])
        model_id = _pick_model("story_prose", params.get("prose_model"))
        raw = _run_llm(
            pid, f"rewrite-{index + 1}",
            model_id=model_id,
            system_prompt=_system_prompt("rewrite", "chapter_explicit", params,
                                         language=code),
            user_prompt=user_prompt,
            stream_id=f"story-{pid}-rw{index}",
            # "make it longer" needs real headroom over the selection.
            max_new_tokens=chapter_token_budget(
                max(MIN_CHAPTER_WORDS, _word_count(selected_text) * 3)),
            temperature=float(params.get("temperature") or DEFAULT_PROSE_TEMPERATURE),
        )
        replacement = _clean_prose(raw)
        if not replacement:
            return fail("The rewrite pass came back empty.")
        if _HAVE_SAFETY_SCAN:
            try:
                assert_no_minor_content(replacement, "story passage rewrite")
            except SafetyViolationError as exc:
                return fail(f"Blocked by the content safety scan: {exc}")
        return {"ok": True, "replacement": replacement,
                "before": before, "after": after}
    except Exception as exc:  # noqa: BLE001 - reported, never raised at the endpoint
        traceback.print_exc()
        return fail(f"Rewrite failed: {exc}")
    finally:
        _update_story(pid, _active_stream_id=None)
        _release_story_operation(pid)


@_exclusive_story_operation
def apply_passage_rewrite(out_dir: str, pid: str, index: int, selected_text: str,
                          replacement: str, lang: Optional[str] = None) -> bool:
    """Splice a rewrite into the chapter. False unless the match is unique.

    Re-checks the match against what is on disk NOW: the text may have been
    edited between rewrite_passage() and this call, and replacing the wrong
    occurrence would silently corrupt the chapter.
    """
    index = int(index)
    state = load_story(out_dir, pid)
    if not state:
        return False
    chapters = state.get("chapters") or []
    if not 0 <= index < len(chapters) or not (selected_text or ""):
        return False
    original = _story_language(state)
    code = _lang_code(lang) or original
    _, text = _chapter_view(chapters[index], code, original)
    if text.count(selected_text) != 1:
        return False
    return _apply_chapter_text(
        out_dir, pid, index,
        text.replace(selected_text, replacement or "", 1),
        None if code == original else code,
    )


def analyze_story(pid: str, out_dir: Optional[str] = None,
                  ensure_model: Optional[Callable[[str], None]] = None,
                  lang: Optional[str] = None) -> dict:
    """Audit the whole story: characters, dialogue map, issues, timeline.

    Runs ONE schema-constrained pass per written chapter — a full novel never
    fits in one context — plus one short verdict pass at the end, and merges
    the results server-side (`_merge_chapter_analyses`, which documents every
    merge rule). Characters are merged by case-folded name with their chapter
    lists unioned; every chapter number the model chose is validated against
    the real chapter count and dropped and counted (`dropped_refs`) when it
    names a chapter that does not exist.

    Every `chapter` field in the result is a 0-based state index. The result
    is also persisted in the story state under `analysis`, with `analyzed_at`.

    Runs in the calling thread (minutes for a long book) and holds the story's
    exclusive-operation claim while it does. cancel_story_operation() stops it
    between chapters; what was analysed so far is discarded, since a merged
    result covering half the book would read as a complete audit.
    """
    ok, resolved = _for_synchronous_pass(pid, out_dir, ensure_model)
    if not ok:
        return {"ok": False, "error": resolved}
    prior_progress = (_snapshot(pid) or {}).get("progress")
    try:
        state = _snapshot(pid) or {}
        original = _story_language(state)
        code = _lang_code(lang) or original
        chapters = state.get("chapters") or []
        indices = [
            i for i, chapter in enumerate(chapters)
            if (_chapter_view(chapter, code, original)[1] or "").strip()
        ]
        if not indices:
            return {"ok": False, "error": (
                f"Nothing to analyse in {language_name(code)}."
                if code != original else "This story has no written chapters."
            )}

        entries = []
        cancelled = {"ok": False, "error": "Analysis cancelled.", "cancelled": True}
        for position, index in enumerate(indices):
            if operation_cancelled(pid):
                return cancelled
            _update_story(pid, progress=_progress(
                position, len(indices),
                f"Analysing chapter {index + 1} of {len(chapters)}...",
                step=position, total_steps=len(indices) + 1,
            ))
            entries.append((index, _pass_analyze_chapter(pid, index, code)))

        if operation_cancelled(pid):
            return cancelled
        analysis = _merge_chapter_analyses(entries, len(chapters))
        _update_story(pid, progress=_progress(
            len(indices), len(indices), "Writing the overall assessment...",
            step=len(indices), total_steps=len(indices) + 1,
        ))
        analysis["summary"] = _pass_analyze_summary(
            pid, analysis["timeline"], analysis["issues"], code,
        ) or " ".join(e["summary"] for e in analysis["timeline"] if e["summary"])
        analysis.update({
            "ok": True,
            "language": code,
            "chapters_analyzed": len(indices),
            "analyzed_at": time.time(),
        })
        _update_story(pid, analysis=analysis)
        # Put the story's own progress back before it is persisted — the
        # analysis is not a run and must not leave "Analysing chapter 3" in
        # the state file.
        if prior_progress:
            _update_story(pid, progress=prior_progress)
        _save_story_state(pid)
        return analysis
    except Exception as exc:  # noqa: BLE001 - reported, never raised at the endpoint
        traceback.print_exc()
        return {"ok": False, "error": f"Analysis failed: {exc}"}
    finally:
        _update_story(pid, _active_stream_id=None)
        if prior_progress:
            _update_story(pid, progress=prior_progress)
        _release_story_operation(pid)


def export_story(out_dir: str, pid: str, fmt: str = "md",
                 lang: Optional[str] = None) -> str:
    """Write the story as .md or .txt into the workspace, return the path.

    Writing it into out_dir is what makes it show up as a text output
    (§1.3) and what "Create audiobook" later reads. A translation exports to
    its own file (`..._de.md`) so it never overwrites the original.
    """
    fmt = (fmt or "md").lower().lstrip(".")
    if fmt not in ("md", "txt"):
        raise ValueError(f"Unsupported export format: {fmt}")
    state = load_story(out_dir, pid) or get_story(pid)
    if not state:
        raise FileNotFoundError(f"No story {pid}")
    content = format_story(state, fmt, lang)
    code = _lang_code(lang)
    suffix = f"_{code}" if code and code != _story_language(state) else ""
    filename = f"story_{_safe_slug(state.get('title') or '')}_{pid}{suffix}.{fmt}"
    filepath = _find_story_file(out_dir, pid)
    story_dir = os.path.dirname(filepath) if filepath else out_dir
    os.makedirs(story_dir, exist_ok=True)
    target = os.path.join(story_dir, filename)
    with _story_file_lock:
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(content)

        def updater(saved: dict) -> None:
            files = saved.setdefault("output_files", [])
            if filename not in files:
                files.append(filename)

        _update_saved_story(out_dir, pid, updater)
    with _story_lock:
        live = _stories.get(pid)
        if live is not None and filename not in (live.get("output_files") or []):
            live.setdefault("output_files", []).append(filename)
    return target


# ── Self-check ─────────────────────────────────────────────────────────────
# Covers the pure logic and one full fake-LLM run: `python -m
# services.story_pipeline` from app/. No CUDA, no model, no network.

def _self_check() -> None:
    import shutil
    import sys
    import tempfile
    import types

    # 1. Length maths.
    assert total_target_words(20) == 20 * WORDS_PER_PAGE
    assert auto_chapter_count(100) == round(100 * WORDS_PER_PAGE / AUTO_WORDS_PER_CHAPTER)
    assert auto_chapter_count(1) == 3, "never fewer than 3 chapters"
    assert chapter_target_words(40, 10) == 40 * WORDS_PER_PAGE // 10 == 1100
    assert chapter_target_words(4, 20) == MIN_CHAPTER_WORDS, "clamped up"
    assert chapter_target_words(2000, 2) == MAX_CHAPTER_WORDS, "clamped down"
    assert chapter_token_budget(1100) == 1100 * 2 + 512
    assert chapter_token_budget(MAX_CHAPTER_WORDS) <= MAX_CHAPTER_TOKENS
    assert chapter_target_words(40, 0) > 0 and chapter_target_words(None, None) > 0

    # 2. Context window: bounded tail, bible + synopsis present, old prose gone.
    state = {
        # 12 pages / 3 chapters -> 1100 words per chapter.
        "params": {"min_pages": 12, "genre": "noir", "pov": "first person"},
        "outline": {
            "title": "The Salt Line",
            "logline": "A diver hunts her brother's killer.",
            "setting": "A drowned harbour town.",
            "characters": [{"name": "Mara", "role": "protagonist",
                            "description": "Diver, thirty, scar on her jaw."}],
            "chapters": [{"title": "Low Tide"}, {"title": "The Wreck"},
                         {"title": "Salt"}],
        },
        "synopsis_running": "Mara found the wreck and the missing canvas.",
        "character_state": {"characters": {"Mara": "aboard the Kestrel, armed"},
                            "open_threads": ["the canvas is still missing"]},
        "chapters": [
            {"index": 0, "title": "Low Tide", "beats": ["b"],
             "text": "ANCIENTMARKER " + "alpha " * 3000, "status": "done"},
            {"index": 1, "title": "The Wreck", "beats": ["b"],
             "text": "beta " * 3000 + "TAILMARKER", "status": "done"},
            {"index": 2, "title": "Salt", "beats": ["Mara confronts the buyer",
                                                    "She loses the canvas"],
             "text": "", "status": "pending"},
        ],
    }
    context = build_chapter_context(state, 2, instruction="colder, less dialogue")
    assert "The Salt Line" in context and "Mara" in context, "bible in context"
    assert "Mara found the wreck" in context, "running synopsis in context"
    assert "aboard the Kestrel" in context, "character state in context"
    assert "the canvas is still missing" in context, "open threads in context"
    assert "TAILMARKER" in context, "tail of the previous prose in context"
    assert "ANCIENTMARKER" not in context, "chapter 1 must NOT be in context"
    assert "colder, less dialogue" in context, "instruction in context"
    assert "about 1100 words" in context, "word target handed to the model"
    assert "Mara confronts the buyer" in context, "beats in context"
    tail_only = _tail_words("w " * 5000)
    assert _word_count(tail_only) == CONTEXT_TAIL_WORDS, "tail is bounded"
    assert _word_count(context) < 2600, f"context stays flat, got {_word_count(context)}"
    # Growing the story must not grow the context.
    grown = dict(state)
    grown["chapters"] = [dict(c) for c in state["chapters"]]
    grown["chapters"][0]["text"] = "gamma " * 40000
    assert abs(_word_count(build_chapter_context(grown, 2)) - _word_count(
        build_chapter_context(state, 2))) < 50, "context is length-independent"

    # 2b. Language helpers.
    assert _lang_code("DE") == "de" and _lang_code("de_DE") == "de-de"
    assert _lang_code(None) == "" and _lang_code("not a code") == ""
    assert language_name("de") == "German" and language_name("de-DE") == "German"
    assert language_name("xx") == "xx", "unknown codes pass through"
    assert _story_language({"params": {}}) == DEFAULT_LANGUAGE, "old stories are 'en'"
    assert _story_language({"params": {"language": "FR"}}) == "fr"
    assert story_languages({
        "params": {"language": "de"},
        "chapters": [{"translations": {"en": {"text": "x"}}},
                     {"translations": {"en": {"text": "y"}, "fr": {"text": "z"}}},
                     {"translations": {"de": {"text": "ignored"}}}],
    }) == ["de", "en", "fr"], "original first, translations sorted, self excluded"
    assert story_languages({}) == [DEFAULT_LANGUAGE]

    # 2c. Translation reply splitting.
    assert _split_translated("Kapitel Eins\n\nEr ging.", "old") == ("Kapitel Eins", "Er ging.")
    assert _split_translated("Titel: Salz\n\nSie tauchte.", "old")[0] == "Salz"
    # A first line that reads like prose is prose — the title is not eaten.
    assert _split_translated("Er ging fort.\n\nDann kam sie.", "old") == \
        ("old", "Er ging fort.\n\nDann kam sie.")
    assert _split_translated("", "old") == ("old", "")

    # 2d. Chapter views and stale marking.
    chapter = {"index": 0, "title": "One", "text": "English prose.",
               "translations": {"de": {"title": "Eins", "text": "Deutsche Prosa.",
                                       "stale": False}}}
    assert _chapter_view(chapter, None, "en") == ("One", "English prose.")
    assert _chapter_view(chapter, "en", "en") == ("One", "English prose.")
    assert _chapter_view(chapter, "de", "en") == ("Eins", "Deutsche Prosa.")
    assert _chapter_view(chapter, "fr", "en") == ("One", ""), "missing translation is empty"
    _mark_translations_stale(chapter)
    assert chapter["translations"]["de"]["stale"] is True
    assert chapter["text"] == "English prose.", "marking stale changes no text"

    # 2e. Insert / delete renumber everything that carries a chapter index.
    doc = {
        "chapters": [
            {"index": 0, "title": "A", "text": "a"},
            {"index": 1, "title": "B", "text": "b"},
            {"index": 2, "title": "C", "text": "c"},
        ],
        "outline": {"chapters": [{"title": "A"}, {"title": "B"}, {"title": "C"}]},
    }
    assert _insert_into_state(doc, 1, title="NEW") == 1
    assert [c["index"] for c in doc["chapters"]] == [0, 1, 2, 3]
    assert [c["title"] for c in doc["chapters"]] == ["A", "NEW", "B", "C"]
    assert doc["chapters"][1]["text"] == "" and doc["chapters"][1]["beats"] == []
    assert doc["chapters"][1]["status"] == "pending"
    assert doc["chapters"][1]["translations"] == {}
    assert [c["title"] for c in doc["outline"]["chapters"]] == ["A", "NEW", "B", "C"]
    assert not doc.get("synopsis_stale"), "an empty insert changes no prose"
    assert _insert_into_state(doc, 99, title="TAIL", text="tail prose") == 4, "clamped -> append"
    assert doc["chapters"][4]["index"] == 4 and doc["chapters"][4]["status"] == "done"
    assert doc["chapters"][4]["word_count"] == 2 and doc["synopsis_stale"] is True
    doc["synopsis_stale"] = False
    assert _delete_from_state(doc, 4) is True and _delete_from_state(doc, 1) is True
    assert [c["title"] for c in doc["chapters"]] == ["A", "B", "C"]
    assert [c["index"] for c in doc["chapters"]] == [0, 1, 2]
    assert [c["title"] for c in doc["outline"]["chapters"]] == ["A", "B", "C"]
    assert doc["synopsis_stale"] is True, "deleting written prose invalidates the record"
    assert _delete_from_state(doc, 9) is False
    # Renumbering keeps format_story's chapter numbers contiguous.
    assert "## 3. C" in format_story(dict(doc, title="T"), "md")

    # 2f. Seam context for an inserted chapter (bridge=True).
    seam = {
        "params": {"min_pages": 12, "language": "de"},
        "outline": {"title": "Der Salzweg", "logline": "L",
                    "chapters": [{"title": "A"}, {"title": ""}, {"title": "C"}]},
        "synopsis_running": "SYNOPSIS AFTER THE WHOLE STORY",
        "chapters": [
            {"index": 0, "title": "A", "text": "vorher " * 20 + "PREVEND",
             "status": "done"},
            {"index": 1, "title": "", "beats": [], "text": "", "status": "pending"},
            {"index": 2, "title": "C", "text": "NEXTHEAD " + "spaeter " * 900,
             "status": "done", "synopsis_at_start": "SYNOPSIS BEFORE C"},
        ],
    }
    seam_ctx = build_chapter_context(seam, 1, instruction="ein ruhiges Zwischenspiel",
                                     bridge=True)
    assert "PREVEND" in seam_ctx, "end of the previous chapter"
    assert "NEXTHEAD" in seam_ctx, "beginning of the following chapter"
    assert "SYNOPSIS BEFORE C" in seam_ctx, "the story as it stood at the seam"
    assert "SYNOPSIS AFTER THE WHOLE STORY" not in seam_ctx, "not the ending"
    assert "ein ruhiges Zwischenspiel" in seam_ctx, "the brief is the instruction"
    assert "German (de)" in seam_ctx, "the story's language reaches the pass"
    assert "NEXTHEAD" not in build_chapter_context(seam, 1), "only in bridge mode"
    assert _word_count(_head_words("w " * 5000)) == CONTEXT_HEAD_WORDS
    assert _word_count(seam_ctx) < 2600, "the seam context stays flat too"

    # 2g. Analysis merge: characters unioned, invented chapter indices dropped.
    merged = _merge_chapter_analyses([
        (0, {"characters": [{"name": "Ada", "role": "protagonist",
                             "description": "A tester.", "traits": ["dry"]},
                            {"name": "Bo", "traits": ["loud"]}],
             "dialogue": [{"speaker": "Ada", "line_excerpt": "It compiles."}],
             "issues": [
                 {"kind": "plot_hole", "severity": "high", "chapter": 1,
                  "description": "Fixes itself.", "suggestion": "Show it."},
                 {"kind": "vibes", "severity": "catastrophic", "chapter": 2,
                  "description": "Unknown vocabulary."},
                 {"kind": "timeline", "severity": "low", "chapter": 99,
                  "description": "Invented chapter."},
                 {"kind": "pacing", "severity": "low", "chapter": "nonsense",
                  "description": "Unparseable chapter."},
                 {"kind": "character", "severity": "low",
                  "description": "No chapter given."},
             ],
             "when": "day one", "where": "the lab", "summary": "Ada tested."}),
        (1, {"characters": [{"name": "ada", "traits": ["dry", "stubborn"]}],
             "dialogue": [{"speaker": "Bo", "line_excerpt": ""}],
             "issues": [], "summary": "Bo shouted."}),
    ], total_chapters=2)
    assert [c["name"] for c in merged["characters"]] == ["Ada", "Bo"], merged["characters"]
    ada = merged["characters"][0]
    assert ada["chapters"] == [0, 1] and ada["first_chapter"] == 0 and ada["last_chapter"] == 1
    assert ada["role"] == "protagonist" and ada["description"] == "A tester."
    assert ada["traits"] == ["dry", "stubborn"], "traits unioned, no duplicates"
    assert merged["dropped_refs"] == 2, "invented and unparseable chapter refs"
    assert [(i["kind"], i["severity"], i["chapter"]) for i in merged["issues"]] == [
        ("plot_hole", "high", 0), ("character", "low", 0), ("continuity", "medium", 1),
    ], merged["issues"]
    assert [d["speaker"] for d in merged["dialogue_map"]] == ["Ada"], "empty lines dropped"
    assert merged["dialogue_map"][0]["chapter"] == 0
    assert [t["chapter"] for t in merged["timeline"]] == [0, 1]
    assert merged["timeline"][1]["when"] == "unclear", "missing when is not invented"
    assert merged["truncated"] is False
    # The dialogue map is bounded, and says so.
    flood = _merge_chapter_analyses([
        (i, {"dialogue": [{"speaker": "A", "line_excerpt": f"line {j}"}
                          for j in range(ANALYZE_DIALOGUE_PER_CHAPTER + 10)],
             "characters": [], "issues": []})
        for i in range(8)
    ], total_chapters=8)
    assert len(flood["dialogue_map"]) == MAX_DIALOGUE_ENTRIES
    assert flood["truncated"] is True

    # 3. Absorbing cancel semantics.
    pid = "selftest1"
    with _story_lock:
        _stories[pid] = {"story_id": pid, "status": "writing", "chapters": [],
                         "progress": _progress(0, 1, "x")}
    assert _update_story(pid, status="planning") is True
    with _story_lock:
        _stories[pid]["status"] = "cancelled"
    assert _update_story(pid, status="completed") is False, "cancel absorbs completion"
    assert _update_story(pid, status="failed", error="boom") is False
    assert _update_story(pid, progress=_progress(1, 1, "done")) is False
    assert _stories[pid]["status"] == "cancelled"
    assert _update_story(pid, chapters=[{"index": 0}]) is True, "late artifact lands"
    assert _update_story(pid, output_files=["a.md"]) is True
    assert _stories[pid]["chapters"] == [{"index": 0}]
    with _story_lock:
        _stories.pop(pid, None)

    # 4. Export formatting.
    export_state = {
        "title": "The Salt Line",
        "outline": {"logline": "A diver hunts her brother's killer."},
        "chapters": [
            {"index": 0, "title": "Low Tide", "text": "The tide went out."},
            {"index": 1, "title": "The Wreck", "text": "She dove."},
            {"index": 2, "title": "Salt", "text": ""},
        ],
    }
    md = format_story(export_state, "md")
    assert md.startswith("# The Salt Line\n"), md[:40]
    assert "## 1. Low Tide" in md and "## 2. The Wreck" in md
    assert "Salt" not in md.split("## 2.")[1], "empty chapters are skipped"
    assert "*A diver hunts" in md and md.endswith("\n")
    txt = format_story(export_state, "txt")
    assert txt.startswith("THE SALT LINE") and "CHAPTER 1: LOW TIDE" in txt
    assert "#" not in txt, "txt carries no markdown"
    assert _safe_slug("The Salt Line!") == "the_salt_line"
    assert _safe_slug("") == "story"

    # 5. Prose cleanup + JSON parsing.
    assert _clean_prose("<think>hm</think>\n## Chapter 3: Salt\n\nShe dove.") == "She dove."
    assert _clean_prose("Chapter 3\n\nShe dove.") == "She dove."
    assert _parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert _parse_json_object('noise {"a": 1} trailing') == {"a": 1}
    assert _parse_json_object("not json at all") in (None, {})

    # 6. Fake-LLM end-to-end: persistence roundtrip, passes, export, delete.
    fake = types.ModuleType("services.llm_service")
    fake.MODEL_REGISTRY = {"fake/outliner": {"gguf_file": "o.gguf"},
                           "fake/writer": {"gguf_file": "w.gguf"}}
    fake._last_system_prompt = ""
    fake._last_user_prompt = ""
    fake._stream_buffer = ""
    fake._last_thinking_text = None
    fake.calls = []

    def _models_for_use_case(use_case):
        return ["fake/outliner"] if use_case == "story_outline" else ["fake/writer"]

    def _generate_streaming(**kwargs):
        fake.calls.append(kwargs)
        fake._last_system_prompt = kwargs.get("system_prompt", "")
        fake._last_user_prompt = kwargs.get("prompt", "")
        schema = kwargs.get("json_schema") or {}
        props = (schema.get("properties") or {})
        if "logline" in props:
            out = {"title": "Fake Story", "logline": "A test.",
                   "setting": "A lab",
                   "characters": [{"name": "Ada", "description": "A tester."}],
                   "chapters": [{"title": "One", "beats": ["a", "b"]},
                                {"title": "Two", "beats": ["c", "d"]}]}
        elif "chapters" in props:
            out = {"chapters": [{"title": "Three", "beats": ["e", "f"]}]}
        elif "synopsis" in props:
            out = {"synopsis": "Ada tested things.",
                   "characters": [{"name": "Ada", "state": "in the lab"}],
                   "open_threads": ["the build is red"]}
        elif "dialogue" in props:
            # One pass per chapter. The first chapter also names a chapter
            # that does not exist and uses vocabulary outside the schema —
            # both must be handled server-side, not stored.
            first = (kwargs.get("stream_id") or "").endswith("ch0")
            out = {
                "characters": [
                    {"name": "Ada", "role": "protagonist",
                     "description": "A tester.", "traits": ["dry", "stubborn"]},
                    {"name": "Bo", "description": "A colleague.",
                     "traits": ["loud"]},
                ] if first else [
                    {"name": "ada", "role": "", "description": "",
                     "traits": ["stubborn", "tired"]},
                ],
                "dialogue": [{"speaker": "Ada", "line_excerpt": "It compiles.",
                              "context": "reporting the build"}],
                "issues": [
                    {"kind": "plot_hole", "severity": "high", "chapter": 1,
                     "description": "The build fixes itself.",
                     "suggestion": "Show the fix."},
                    {"kind": "vibes", "severity": "catastrophic", "chapter": 2,
                     "description": "Unknown vocabulary."},
                    {"kind": "timeline", "severity": "low", "chapter": 99,
                     "description": "A chapter that does not exist."},
                ] if first else [],
                "when": "day one", "where": "the lab",
                "summary": "Ada tested things.",
            }
        elif "summary" in props:
            out = {"summary": "A tidy little test story."}
        else:
            stream_id = kwargs.get("stream_id") or ""
            if "-tr-" in stream_id:
                fake._stream_buffer = "Kapitel Eins\n\n[de] " + "wort " * 50
            elif "-rw" in stream_id:
                fake._stream_buffer = "REWRITTEN."
            else:
                fake._stream_buffer = "## Chapter\n\n" + "word " * 120
            return fake._stream_buffer
        fake._stream_buffer = json.dumps(out)
        return fake._stream_buffer

    fake.models_for_use_case = _models_for_use_case
    fake.generate_streaming = _generate_streaming
    fake.set_idle_timeout = lambda seconds=None: fake.__dict__.setdefault(
        "idle", []).append(seconds)
    fake.cancel_stream = lambda stream_id: False
    fake.get_model_dir = lambda: tempfile.gettempdir()
    import services as _services_pkg
    sys.modules["services.llm_service"] = fake
    _services_pkg.llm_service = fake

    workdir = tempfile.mkdtemp(prefix="storycheck_")
    loaded_models: list[str] = []
    try:
        request = {"premise": "A tester writes a test.", "min_pages": 8,
                   "chapter_count": 2, "genre": "comedy", "nsfw": False,
                   "explicitness": "graphic", "temperature": 0.9}
        story_id = start_story(request, workdir, loaded_models.append)
        thread = _story_threads.get(story_id)
        if thread:
            thread.join(timeout=30)
        live = get_story(story_id)
        assert live["status"] == "completed", live.get("error")
        assert len(live["chapters"]) == 2
        assert all(c["status"] == "done" and c["word_count"] == 120
                   for c in live["chapters"])
        assert live["title"] == "Fake Story"
        assert live["synopsis_running"] == "Ada tested things."
        assert live["character_state"]["characters"] == {"Ada": "in the lab"}
        assert live["character_state"]["open_threads"] == ["the build is red"]
        assert "_ensure_model" not in live and "out_dir" not in live
        assert loaded_models == ["fake/outliner", "fake/writer", "fake/outliner",
                                 "fake/writer", "fake/outliner"], loaded_models
        # Idle timeout raised for the run, restored afterwards.
        assert fake.__dict__["idle"] == [STORY_IDLE_TIMEOUT_S, None]
        # Stream ids are unique per pass and namespaced away from the Director.
        ids = [c["stream_id"] for c in fake.calls]
        assert ids[0] == f"story-{story_id}-outline"
        assert f"story-{story_id}-ch0" in ids and f"story-{story_id}-ch1" in ids
        assert len(set(ids)) == len(ids), ids
        # Prose is high-temperature and unconstrained; the bookkeeping
        # passes are low-temperature and schema-constrained.
        prose_calls = [c for c in fake.calls if c.get("json_schema") is None]
        assert len(prose_calls) == 2
        assert all(c["temperature"] == 0.9 for c in prose_calls)
        assert all(c["max_new_tokens"] == chapter_token_budget(1100)
                   for c in prose_calls)
        assert fake.calls[0]["temperature"] == OUTLINE_TEMPERATURE
        # nsfw=False -> the tame guide path, no explicit block.
        assert "ADULT CONTENT" not in prose_calls[0]["system_prompt"]
        assert len(live["llm_passes"]) == 5, live["llm_passes"]
        assert live["llm_passes"][0]["pass"] == "outline"

        # Persistence roundtrip.
        saved = load_story(workdir, story_id)
        assert saved["version"] == STORY_STATE_VERSION
        assert saved["story_id"] == story_id
        assert saved["_params_snapshot"] == request, "full original request kept"
        assert saved["status"] == "completed" and saved["total_time_sec"] >= 0
        assert set(saved) <= set(_STORY_PERSISTED_FIELDS), set(saved)
        assert "out_dir" not in saved and "_ensure_model" not in saved
        assert [c["text"] for c in saved["chapters"]] == \
               [c["text"] for c in live["chapters"]]
        assert os.path.isfile(
            os.path.join(workdir, f"{_STORY_FILE_PREFIX}{story_id}.json"))
        summaries = list_stories(workdir)
        assert [s["id"] for s in summaries] == [story_id]
        assert summaries[0]["chapters_done"] == 2
        assert summaries[0]["word_count"] == 240

        # Manual edit -> stale synopsis, pre-edit text preserved.
        assert update_chapter_text(workdir, story_id, 0, "My own words.") is True
        edited = load_story(workdir, story_id)
        assert edited["synopsis_stale"] is True
        assert edited["chapters"][0]["text"] == "My own words."
        assert edited["chapters"][0]["edited"] is True
        assert edited["chapters"][0]["text_pre_edit"].startswith("word")
        assert edited["chapters"][0]["word_count"] == 3

        # Export lands in the workspace and is registered as an output.
        md_path = export_story(workdir, story_id, "md")
        assert os.path.isfile(md_path) and md_path.endswith(".md")
        with open(md_path, encoding="utf-8") as handle:
            assert "My own words." in handle.read()
        txt_path = export_story(workdir, story_id, "txt")
        assert os.path.isfile(txt_path)
        exported = load_story(workdir, story_id)
        assert sorted(exported["output_files"]) == sorted(
            [os.path.basename(md_path), os.path.basename(txt_path)])

        # Extend: plans + writes one more chapter, rebuilding the stale record.
        ok, why = extend_story(story_id, 1, out_dir=workdir,
                               ensure_model=loaded_models.append)
        assert ok, why
        thread = _story_threads.get(story_id)
        if thread:
            thread.join(timeout=30)
        extended = get_story(story_id)
        assert extended["status"] == "completed", extended.get("error")
        assert len(extended["chapters"]) == 3
        assert extended["chapters"][2]["title"] == "Three"
        assert extended["chapters"][2]["status"] == "done"
        assert extended["synopsis_stale"] is False, "stale flag cleared by rebuild"
        assert extended["chapters"][0]["text"] == "My own words.", "edit survives"

        # Regenerate one chapter with an instruction.
        ok, why = regenerate_chapter(story_id, 1, instruction="darker",
                                     out_dir=workdir,
                                     ensure_model=loaded_models.append)
        assert ok, why
        thread = _story_threads.get(story_id)
        if thread:
            thread.join(timeout=30)
        regenerated = get_story(story_id)
        assert regenerated["status"] == "completed", regenerated.get("error")
        assert regenerated["chapters"][1]["instruction"] == "darker"
        assert "darker" in fake.calls[-3]["prompt"] or any(
            "darker" in c.get("prompt", "") for c in fake.calls)
        assert not any_story_active()
        ok, why = regenerate_chapter(story_id, 99, out_dir=workdir)
        assert not ok and "does not exist" in why, why
        assert not any_story_active(), "a rejected regen leaves no worker"

        # Progress shape is exactly what PipelinePlaceholder reads.
        assert set(regenerated["progress"]) == {
            "current", "total", "message", "step", "total_steps"}

        # 6b. Translation roundtrip: originals untouched, own stream ids.
        chapter_total = len(regenerated["chapters"])
        ok, why = translate_story(story_id, "DE", out_dir=workdir,
                                  ensure_model=loaded_models.append)
        assert ok, why
        thread = _story_threads.get(story_id)
        if thread:
            thread.join(timeout=30)
        translated = get_story(story_id)
        assert translated["status"] == "completed", translated.get("error")
        assert translated["languages"] == ["en", "de"], translated["languages"]
        for chapter in translated["chapters"]:
            entry = chapter["translations"]["de"]
            assert entry["title"] == "Kapitel Eins"
            assert entry["text"].startswith("[de] ") and entry["stale"] is False
            assert entry["translated_at"] > 0
        assert translated["chapters"][0]["text"] == "My own words.", "original kept"
        tr_calls = [c for c in fake.calls if "-tr-de-" in (c.get("stream_id") or "")]
        assert len(tr_calls) == chapter_total, tr_calls
        assert tr_calls[0]["stream_id"] == f"story-{story_id}-tr-de-ch0"
        assert all(c["temperature"] == TRANSLATE_TEMPERATURE for c in tr_calls)
        assert all(c.get("json_schema") is None for c in tr_calls)
        assert "German (de)" in tr_calls[0]["system_prompt"], "target language, not source"
        assert load_story(workdir, story_id)["languages"] == ["en", "de"]
        # A translation exports to its own file and carries the translated prose.
        de_path = export_story(workdir, story_id, "md", lang="de")
        assert de_path.endswith("_de.md")
        with open(de_path, encoding="utf-8") as handle:
            de_text = handle.read()
        assert "[de] wort" in de_text and "My own words." not in de_text

        # 6c. Editing the original marks its translations stale; editing a
        #     translation touches nothing else.
        assert update_chapter_text(workdir, story_id, 1,
                                   "Alpha one. Beta two. Alpha one.") is True
        edited = load_story(workdir, story_id)
        assert edited["chapters"][1]["translations"]["de"]["stale"] is True
        assert edited["chapters"][0]["translations"]["de"]["stale"] is False, \
            "only the edited chapter goes stale"
        assert edited["synopsis_stale"] is True
        assert update_chapter_text(workdir, story_id, 1, "Meine Worte.",
                                   lang="de") is True
        tr_edited = load_story(workdir, story_id)
        assert tr_edited["chapters"][1]["translations"]["de"]["text"] == "Meine Worte."
        assert tr_edited["chapters"][1]["translations"]["de"]["stale"] is False
        assert tr_edited["chapters"][1]["text"] == "Alpha one. Beta two. Alpha one.", \
            "the original is not touched by a translation edit"

        # 6d. Re-translating one chapter refreshes only that chapter.
        ok, why = retranslate_chapter(story_id, 0, "de", out_dir=workdir,
                                      ensure_model=loaded_models.append)
        assert ok, why
        thread = _story_threads.get(story_id)
        if thread:
            thread.join(timeout=30)
        retranslated = get_story(story_id)
        assert retranslated["status"] == "completed", retranslated.get("error")
        assert retranslated["chapters"][0]["translations"]["de"]["stale"] is False
        assert retranslated["chapters"][1]["translations"]["de"]["text"] == "Meine Worte."
        assert retranslated["synopsis_stale"] is True, "translating rebuilds nothing"
        assert retranslate_chapter(story_id, 0, "en", out_dir=workdir)[0] is False
        assert retranslate_chapter(story_id, 99, "de", out_dir=workdir)[1] \
            .endswith("does not exist.")
        assert translate_story(story_id, "en", out_dir=workdir)[0] is False, \
            "the original language is not a translation target"
        assert translate_story(story_id, "!!", out_dir=workdir)[0] is False
        assert not any_story_active(), "a rejected translation leaves no worker"

        # 6e. Passage rewrite: unique match only, and applying it is separate.
        good = rewrite_passage(story_id, 1, "Beta two.", "make it darker",
                               out_dir=workdir, ensure_model=loaded_models.append)
        assert good["ok"] is True, good.get("error")
        assert good["replacement"] == "REWRITTEN."
        assert good["before"] == "Alpha one. " and good["after"] == " Alpha one."
        rewrite_call = [c for c in fake.calls
                        if (c.get("stream_id") or "").endswith("-rw1")][-1]
        assert "Beta two." in rewrite_call["prompt"]
        assert "make it darker" in rewrite_call["prompt"]
        assert "Point of view" in rewrite_call["prompt"], "style params reach the pass"
        missing = rewrite_passage(story_id, 1, "Gamma three.", "x", out_dir=workdir)
        assert missing["ok"] is False and "not found" in missing["error"]
        ambiguous = rewrite_passage(story_id, 1, "Alpha one.", "x", out_dir=workdir)
        assert ambiguous["ok"] is False and ambiguous["occurrences"] == 2
        assert "appears 2 times" in ambiguous["error"], ambiguous["error"]
        assert rewrite_passage(story_id, 1, "Beta two.", "  ", out_dir=workdir)["ok"] \
            is False, "an empty instruction is rejected before the model runs"
        assert rewrite_passage(story_id, 1, "", "x", out_dir=workdir)["ok"] is False
        assert rewrite_passage(story_id, 99, "x", "y", out_dir=workdir)["ok"] is False
        assert rewrite_passage(story_id, 1, "Meine Worte.", "kürzer", lang="de",
                               out_dir=workdir)["ok"] is True, "translations too"
        assert not any_story_active(), "a rewrite releases its claim"

        assert apply_passage_rewrite(workdir, story_id, 1, "Beta two.",
                                     "REWRITTEN.") is True
        applied = load_story(workdir, story_id)
        assert applied["chapters"][1]["text"] == "Alpha one. REWRITTEN. Alpha one."
        assert applied["chapters"][1]["translations"]["de"]["stale"] is True, \
            "the original changed under the translation"
        assert apply_passage_rewrite(workdir, story_id, 1, "Alpha one.", "X") is False, \
            "ambiguous match is never guessed at"
        assert apply_passage_rewrite(workdir, story_id, 1, "Nope.", "X") is False
        assert load_story(workdir, story_id)["chapters"][1]["text"] == \
            "Alpha one. REWRITTEN. Alpha one.", "a refused apply changes nothing"

        # 6f. Insert / delete renumber the saved state and the plan list.
        assert insert_chapter(workdir, story_id, 1, title="Inserted") is True
        inserted = load_story(workdir, story_id)
        assert [c["index"] for c in inserted["chapters"]] == \
            list(range(chapter_total + 1))
        assert inserted["chapters"][1]["title"] == "Inserted"
        assert inserted["chapters"][1]["text"] == ""
        assert inserted["chapters"][1]["beats"] == []
        assert inserted["chapters"][2]["text"].startswith("Alpha one."), "moved down"
        assert len(inserted["outline"]["chapters"]) == len(inserted["chapters"])
        assert insert_chapter(workdir, story_id, 999, title="Tail",
                              text="Tail prose.") is True
        appended = load_story(workdir, story_id)
        assert appended["chapters"][-1]["title"] == "Tail"
        assert appended["chapters"][-1]["index"] == len(appended["chapters"]) - 1
        assert appended["chapters"][-1]["status"] == "done"
        assert delete_chapter(workdir, story_id, len(appended["chapters"]) - 1) is True
        assert delete_chapter(workdir, story_id, 1) is True
        assert delete_chapter(workdir, story_id, 99) is False
        restored = load_story(workdir, story_id)
        assert [c["index"] for c in restored["chapters"]] == list(range(chapter_total))
        assert [c["title"] for c in restored["chapters"]] == \
            [c["title"] for c in translated["chapters"]], "back to the original set"
        assert len(restored["outline"]["chapters"]) == chapter_total

        # 6g. write_chapter_at: inserts, writes to fit the seam, renumbers.
        ok, why = write_chapter_at(story_id, 1, brief="a quiet interlude",
                                   out_dir=workdir,
                                   ensure_model=loaded_models.append)
        assert ok, why
        thread = _story_threads.get(story_id)
        if thread:
            thread.join(timeout=30)
        bridged = get_story(story_id)
        assert bridged["status"] == "completed", bridged.get("error")
        assert len(bridged["chapters"]) == chapter_total + 1
        assert [c["index"] for c in bridged["chapters"]] == \
            list(range(chapter_total + 1))
        assert bridged["chapters"][1]["status"] == "done"
        assert bridged["chapters"][1]["word_count"] == 120
        assert bridged["chapters"][1]["instruction"] == "a quiet interlude"
        assert bridged["chapters"][2]["text"] == "Alpha one. REWRITTEN. Alpha one."
        seam_prompt = [c for c in fake.calls
                       if c.get("stream_id") == f"story-{story_id}-ch1"][-1]["prompt"]
        assert "BEGINNING OF THE FOLLOWING CHAPTER" in seam_prompt
        assert "Alpha one." in seam_prompt, "the following chapter is in the context"
        assert "a quiet interlude" in seam_prompt
        assert write_chapter_at(story_id, 999, out_dir=workdir)[0] is False
        assert not any_story_active()

        # 6h. Analysis: per-chapter passes merged, invented indices discarded.
        analysis = analyze_story(story_id, out_dir=workdir,
                                 ensure_model=loaded_models.append)
        assert analysis["ok"] is True, analysis.get("error")
        total_now = len(bridged["chapters"])
        assert analysis["chapters_analyzed"] == total_now
        an_calls = [c for c in fake.calls if "-an-ch" in (c.get("stream_id") or "")]
        assert len(an_calls) == total_now, "one pass per chapter, never one big one"
        assert all(c["temperature"] == ANALYZE_TEMPERATURE for c in an_calls)
        assert all(c.get("json_schema") for c in an_calls), "schema-constrained"
        names = [c["name"] for c in analysis["characters"]]
        assert names == ["Ada", "Bo"], names
        ada = analysis["characters"][0]
        assert ada["chapters"] == list(range(total_now)), ada
        assert ada["first_chapter"] == 0 and ada["last_chapter"] == total_now - 1
        assert ada["role"] == "protagonist" and ada["description"] == "A tester."
        assert ada["traits"] == ["dry", "stubborn", "tired"]
        assert analysis["dropped_refs"] == 1, "the invented chapter index is counted"
        assert all(0 <= i["chapter"] < total_now for i in analysis["issues"])
        assert ("plot_hole", "high", 0) in {
            (i["kind"], i["severity"], i["chapter"]) for i in analysis["issues"]}
        assert ("continuity", "medium", 1) in {
            (i["kind"], i["severity"], i["chapter"]) for i in analysis["issues"]}, \
            "unknown kind and severity are snapped, not stored"
        assert [t["chapter"] for t in analysis["timeline"]] == list(range(total_now))
        assert analysis["dialogue_map"][0]["speaker"] == "Ada"
        assert analysis["truncated"] is False
        assert analysis["summary"] == "A tidy little test story."
        stored = load_story(workdir, story_id)
        assert stored["analysis"]["analyzed_at"] > 0
        assert stored["analysis"]["summary"] == analysis["summary"]
        assert stored["status"] == "completed", "analysing is not a run"
        assert "Analysing" not in stored["progress"]["message"], "progress restored"
        assert not any_story_active(), "analysis releases its claim"

        # Delete removes the state file and the exports.
        assert delete_story(workdir, story_id)["ok"] is True
        assert load_story(workdir, story_id) is None
        assert not os.path.isfile(md_path) and not os.path.isfile(txt_path)
        assert list_stories(workdir) == []
    finally:
        with _story_lock:
            _stories.clear()
            _story_threads.clear()
        sys.modules.pop("services.llm_service", None)
        if getattr(_services_pkg, "llm_service", None) is fake:
            del _services_pkg.llm_service
        shutil.rmtree(workdir, ignore_errors=True)

    # 7. Guides exist and are real content, not placeholders.
    for name in ("outline", "chapter", "continuity", "outline_explicit",
                 "chapter_explicit", "translate", "rewrite", "analyze"):
        text = _guide(name)
        assert len(text) > 500, f"guide {name} is missing or too short"
    assert "ADULT" in _guide("chapter_explicit")
    assert _system_prompt("chapter", "chapter_explicit", {"nsfw": True}) != \
        _system_prompt("chapter", "chapter_explicit", {"nsfw": False})
    assert "ADULT CONTENT" in _system_prompt(
        "chapter", "chapter_explicit", {"nsfw": True})
    # Every pass is told which language to write in; the analysis pass gets no
    # content block (it writes no prose), the translation pass gets the target.
    assert "English (en)" in _system_prompt("chapter", "chapter_explicit", {})
    assert "German (de)" in _system_prompt("chapter", "chapter_explicit",
                                           {"language": "de"})
    assert "French (fr)" in _system_prompt("translate", "chapter_explicit",
                                           {"language": "de"}, language="fr")
    assert "ADULT CONTENT" not in _system_prompt(
        "analyze", None, {"nsfw": True}), "the audit pass writes no prose"

    # 8. Cancelling a synchronous pass, without touching the story status.
    op_pid = "selfcheck-op"
    _stories[op_pid] = {"title": "Op story", "status": "completed",
                        "progress": _progress(1, 4, "Analysing chapter 2 of 4")}
    assert not cancel_story_operation(op_pid), "nothing claimed it yet"
    assert _claim_story_operation(op_pid)
    assert not operation_cancelled(op_pid)
    listed = [i for i in active_operations() if i["id"] == op_pid]
    assert len(listed) == 1 and "chapter 2" in listed[0]["message"], listed
    assert cancel_story_operation(op_pid) and operation_cancelled(op_pid)
    assert [i for i in active_operations() if i["id"] == op_pid][0]["cancelling"]
    assert _stories[op_pid]["status"] == "completed", "cancel must not fail the story"
    _release_story_operation(op_pid)
    assert not active_operations() or all(i["id"] != op_pid for i in active_operations())
    # The stop request must not leak into the next operation.
    assert _claim_story_operation(op_pid) and not operation_cancelled(op_pid)
    _release_story_operation(op_pid)
    del _stories[op_pid]

    # 9. A story left active by a dead process is reported as crashed *with a
    # reason*. "crashed" and an empty error is what a user cannot act on.
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, f"{_STORY_FILE_PREFIX}dead.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"story_id": "dead", "status": "writing",
                       "phase": "writing chapter 2 of 4", "title": "Half a book",
                       "chapters": [{"status": "done", "word_count": 900}]}, handle)
        summary = [s for s in list_stories(tmp) if s["id"] == "dead"]
        assert len(summary) == 1, summary
        assert summary[0]["status"] == "crashed", summary[0]
        assert "writing chapter 2 of 4" in summary[0]["error"], summary[0]["error"]
        assert "extend" in summary[0]["error"], "must say how to recover"
        # Persisted, and a second read does not rewrite a different reason.
        with open(path, encoding="utf-8") as handle:
            stored = json.load(handle)
        assert stored["status"] == "crashed" and stored["error"]
        assert list_stories(tmp)[0]["error"] == stored["error"]
        # An explicit error from a real exception is never overwritten.
        stored.update(status="writing", error="CUDA out of memory")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(stored, handle)
        assert list_stories(tmp)[0]["error"] == "CUDA out of memory"

    print("story_pipeline self-check: OK")


if __name__ == "__main__":
    _self_check()
