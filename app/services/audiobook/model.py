"""AudioBook data model — port of the reference tool's block format.

PLAN-text-audiobook.md §3.1.  The shape mirrors
``audient-scribe-studio/src/lib/blocks.ts`` with snake_case keys:

    Project → Chapter[] → Block[]
    Block   = Paragraph{runs: Run[], attached_sfx?, attached_music?} | Sfx{sfx_id}
    Run     = {id, text, profile_id?, overrides?}

The single design decision worth restating: **a paragraph is a list of runs,
not a string plus character ranges.**  Voice assignments therefore cannot
drift when the user edits text — editing a run's text touches nothing else.

Everything here is a pure function over plain data.  Serialization is
``to_dict``/``from_dict`` on dataclasses; ``from_dict`` ignores unknown keys so
a newer UI can round-trip a project through an older server without loss of
the fields it does know.

Self-check: ``python -m services.audiobook.model`` from ``app/``.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

# Bumped when a field's *meaning* changes (not when one is added).
MODEL_VERSION = 1

BLOCK_PARAGRAPH = "paragraph"
BLOCK_SFX = "sfx"

# Per-run performance overrides.  ``emotion`` is the portable one — it becomes
# an IndexTTS2 ``[tag]``, a Qwen3 instruction phrase, or a Chatterbox
# exaggeration nudge (see ``tts.py``).  The rest are ElevenLabs-shaped values
# the reference tool carried; they map onto per-model params where an
# equivalent exists and are ignored (with a warning) where none does.
OVERRIDE_KEYS = ("emotion", "stability", "style", "speed", "model_type")

_WORD_RE = re.compile(r"\S+")


def new_id() -> str:
    """Short, collision-safe id.  Same shape as pipeline/job ids elsewhere."""
    return uuid.uuid4().hex[:12]


def _pick(data: Any, *names: str) -> Any:
    """First present non-None value among ``names`` (camelCase tolerance)."""
    if not isinstance(data, dict):
        return None
    for name in names:
        if data.get(name) is not None:
            return data[name]
    return None


def _coerce_seed(value: Any) -> Optional[int]:
    """A stored seed, or None when there is no usable one.

    Tolerates the string a JSON round-trip through a form can produce, and
    rejects 0 and negatives: the generator treats 0 as "pick one for me", so
    storing it would look pinned while behaving random.
    """
    try:
        seed = int(value)
    except (TypeError, ValueError):
        return None
    return seed if seed > 0 else None


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


# ── Leaf records ───────────────────────────────────────────────────────────


@dataclass
class Run:
    """One contiguous stretch of text spoken by one voice."""

    id: str = field(default_factory=new_id)
    text: str = ""
    profile_id: Optional[str] = None
    overrides: Optional[dict] = None

    def to_dict(self) -> dict:
        out: dict = {"id": self.id, "text": self.text}
        if self.profile_id:
            out["profile_id"] = self.profile_id
        if self.overrides:
            out["overrides"] = dict(self.overrides)
        return out

    @classmethod
    def from_dict(cls, data: dict) -> "Run":
        return cls(
            id=str(_pick(data, "id") or new_id()),
            text=str(_pick(data, "text") or ""),
            profile_id=_pick(data, "profile_id", "profileId") or None,
            overrides=_pick(data, "overrides") or None,
        )


@dataclass
class Block:
    """A paragraph (runs) or a standalone sound effect.

    One dataclass rather than a class hierarchy: the JSON is a discriminated
    union on ``type`` and a two-variant union does not earn two classes.
    """

    id: str = field(default_factory=new_id)
    type: str = BLOCK_PARAGRAPH
    runs: list[Run] = field(default_factory=list)
    # Paragraph-only: ambience/music that plays *parallel* to this block.
    attached_sfx: Optional[dict] = None      # {sfx_id, loop?, volume?}
    attached_music: Optional[dict] = None    # {music_id, loop?, volume?}
    # Sfx-block only.
    sfx_id: Optional[str] = None

    @property
    def is_paragraph(self) -> bool:
        return self.type == BLOCK_PARAGRAPH

    def text(self) -> str:
        return "".join(run.text for run in self.runs)

    def to_dict(self) -> dict:
        if self.type == BLOCK_SFX:
            return {"id": self.id, "type": BLOCK_SFX, "sfx_id": self.sfx_id}
        out: dict = {
            "id": self.id,
            "type": BLOCK_PARAGRAPH,
            "runs": [run.to_dict() for run in self.runs],
        }
        if self.attached_sfx:
            out["attached_sfx"] = dict(self.attached_sfx)
        if self.attached_music:
            out["attached_music"] = dict(self.attached_music)
        return out

    @classmethod
    def from_dict(cls, data: dict) -> "Block":
        block_type = str(_pick(data, "type") or BLOCK_PARAGRAPH)
        if block_type == BLOCK_SFX:
            return cls(
                id=str(_pick(data, "id") or new_id()),
                type=BLOCK_SFX,
                sfx_id=_pick(data, "sfx_id", "sfxId"),
            )
        raw_runs = _pick(data, "runs") or []
        return cls(
            id=str(_pick(data, "id") or new_id()),
            type=BLOCK_PARAGRAPH,
            runs=[Run.from_dict(one) for one in raw_runs if isinstance(one, dict)],
            attached_sfx=_pick(data, "attached_sfx", "attachedSfx") or None,
            attached_music=_pick(data, "attached_music", "attachedMusic") or None,
        )


@dataclass
class VoiceProfile:
    """A named voice bound to one TTS model.

    ``params`` is model-shaped and driven by that model's ``model_defaults()``
    (temperature, top_p, guidance_scale, exaggeration, speaker, language, …) so
    a parameter set that only fits one engine is never hardcoded.
    """

    id: str = field(default_factory=new_id)
    name: str = "Narrator"
    color: str = "#7c8cf8"
    model_type: str = "index_tts2"
    voice_ref_path: Optional[str] = None
    # IndexTTS2 second reference audio (emotion transfer), optional.
    emotion_ref_path: Optional[str] = None
    default_emotion: Optional[str] = None
    # Fixed generation seed, so every run of this voice asks for the same
    # thing and the render cache can do its job. It does not make a
    # description-built voice reproducible — those engines resample the speaker
    # every run (see voice_library.new_seed). None means "derive one per run".
    seed: Optional[int] = None
    params: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "color": self.color,
            "model_type": self.model_type,
            "voice_ref_path": self.voice_ref_path,
            "emotion_ref_path": self.emotion_ref_path,
            "default_emotion": self.default_emotion,
            "seed": self.seed,
            "params": dict(self.params),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "VoiceProfile":
        return cls(
            id=str(_pick(data, "id") or new_id()),
            name=str(_pick(data, "name") or "Narrator"),
            color=str(_pick(data, "color") or "#7c8cf8"),
            model_type=str(_pick(data, "model_type", "modelType") or "index_tts2"),
            voice_ref_path=_pick(data, "voice_ref_path", "voiceRefPath"),
            emotion_ref_path=_pick(data, "emotion_ref_path", "emotionRefPath"),
            default_emotion=_pick(data, "default_emotion", "defaultEmotion"),
            seed=_coerce_seed(_pick(data, "seed")),
            params=_pick(data, "params") or {},
        )


@dataclass
class SfxAsset:
    """A generated (MMAudio) or uploaded sound effect in the project library."""

    id: str = field(default_factory=new_id)
    label: str = ""
    prompt: str = ""
    duration: float = 3.0
    audio_path: Optional[str] = None
    playback_mode: str = "parallel"   # "parallel" (ambience) | "sequential"
    loop: bool = False
    volume: float = 0.6

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "prompt": self.prompt,
            "duration": self.duration,
            "audio_path": self.audio_path,
            "playback_mode": self.playback_mode,
            "loop": self.loop,
            "volume": self.volume,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SfxAsset":
        mode = str(_pick(data, "playback_mode", "playbackMode") or "parallel")
        return cls(
            id=str(_pick(data, "id") or new_id()),
            label=str(_pick(data, "label") or ""),
            prompt=str(_pick(data, "prompt") or ""),
            duration=_as_float(
                _pick(data, "duration", "duration_seconds", "durationSeconds"), 3.0,
            ),
            audio_path=_pick(data, "audio_path", "audioPath"),
            playback_mode=mode if mode in {"parallel", "sequential"} else "parallel",
            loop=_as_bool(_pick(data, "loop")),
            volume=_as_float(_pick(data, "volume"), 0.6),
        )


@dataclass
class MusicAsset:
    """Background music: ACE-Step generated or uploaded."""

    id: str = field(default_factory=new_id)
    title: str = ""
    source: str = "generated"   # "generated" | "upload"
    prompt: str = ""
    audio_path: Optional[str] = None
    duration: float = 0.0
    volume: float = 0.25
    loop: bool = True

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "source": self.source,
            "prompt": self.prompt,
            "audio_path": self.audio_path,
            "duration": self.duration,
            "volume": self.volume,
            "loop": self.loop,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MusicAsset":
        return cls(
            id=str(_pick(data, "id") or new_id()),
            title=str(_pick(data, "title") or ""),
            source=str(_pick(data, "source") or "generated"),
            prompt=str(_pick(data, "prompt") or ""),
            audio_path=_pick(data, "audio_path", "audioPath"),
            duration=_as_float(_pick(data, "duration"), 0.0),
            volume=_as_float(_pick(data, "volume"), 0.25),
            loop=_as_bool(_pick(data, "loop"), True),
        )


# ── Containers ─────────────────────────────────────────────────────────────


@dataclass
class Chapter:
    id: str = field(default_factory=new_id)
    title: str = ""
    blocks: list[Block] = field(default_factory=list)
    # Chapter-level background music (a block's attached_music overrides it).
    music_id: Optional[str] = None
    language: Optional[str] = None
    # Render cache: the content hash the audio at ``audio_path`` was made from.
    audio_path: Optional[str] = None
    audio_hash: Optional[str] = None
    audio_duration: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "blocks": [block.to_dict() for block in self.blocks],
            "music_id": self.music_id,
            "language": self.language,
            "audio_path": self.audio_path,
            "audio_hash": self.audio_hash,
            "audio_duration": self.audio_duration,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Chapter":
        raw_blocks = _pick(data, "blocks") or []
        return cls(
            id=str(_pick(data, "id") or new_id()),
            title=str(_pick(data, "title") or ""),
            blocks=[Block.from_dict(one) for one in raw_blocks if isinstance(one, dict)],
            music_id=_pick(data, "music_id", "musicId"),
            language=_pick(data, "language"),
            audio_path=_pick(data, "audio_path", "audioPath"),
            audio_hash=_pick(data, "audio_hash", "audioHash"),
            audio_duration=_pick(data, "audio_duration", "audioDuration"),
        )


@dataclass
class Project:
    id: str = field(default_factory=new_id)
    title: str = "Untitled audiobook"
    language: str = "en"
    version: int = MODEL_VERSION
    created_at: float = 0.0
    updated_at: float = 0.0
    chapters: list[Chapter] = field(default_factory=list)
    voice_profiles: list[VoiceProfile] = field(default_factory=list)
    sfx: list[SfxAsset] = field(default_factory=list)
    music: list[MusicAsset] = field(default_factory=list)
    default_profile_id: Optional[str] = None
    # Mix/export knobs — see mix.MixOptions for the meaning and defaults.
    render_settings: dict = field(default_factory=dict)
    # Verbatim original create/import request, for reproducibility.  Same role
    # as director_pipeline's ``_params_snapshot``.
    params_snapshot: dict = field(default_factory=dict)

    # -- lookups -------------------------------------------------------
    def profile(self, profile_id: Optional[str]) -> Optional[VoiceProfile]:
        if profile_id:
            for one in self.voice_profiles:
                if one.id == profile_id:
                    return one
        return None

    def resolve_profile(self, run: Run) -> Optional[VoiceProfile]:
        """Profile for a run, falling back to the project default."""
        return self.profile(run.profile_id) or self.profile(self.default_profile_id)

    def sfx_asset(self, sfx_id: Optional[str]) -> Optional[SfxAsset]:
        for one in self.sfx:
            if sfx_id and one.id == sfx_id:
                return one
        return None

    def music_asset(self, music_id: Optional[str]) -> Optional[MusicAsset]:
        for one in self.music:
            if music_id and one.id == music_id:
                return one
        return None

    def chapter(self, chapter_id: Optional[str]) -> Optional[Chapter]:
        for one in self.chapters:
            if chapter_id and one.id == chapter_id:
                return one
        return None

    # -- serialization -------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "project_id": self.id,
            "title": self.title,
            "language": self.language,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "default_profile_id": self.default_profile_id,
            "chapters": [one.to_dict() for one in self.chapters],
            "voice_profiles": [one.to_dict() for one in self.voice_profiles],
            "sfx": [one.to_dict() for one in self.sfx],
            "music": [one.to_dict() for one in self.music],
            "render_settings": dict(self.render_settings),
            "_params_snapshot": dict(self.params_snapshot),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Project":
        return cls(
            id=str(_pick(data, "project_id", "id", "pid") or new_id()),
            title=str(_pick(data, "title") or "Untitled audiobook"),
            language=str(_pick(data, "language") or "en"),
            version=int(_pick(data, "version") or MODEL_VERSION),
            created_at=_as_float(_pick(data, "created_at", "createdAt"), 0.0),
            updated_at=_as_float(_pick(data, "updated_at", "updatedAt"), 0.0),
            chapters=[
                Chapter.from_dict(one)
                for one in (_pick(data, "chapters") or [])
                if isinstance(one, dict)
            ],
            voice_profiles=[
                VoiceProfile.from_dict(one)
                for one in (_pick(data, "voice_profiles", "voiceProfiles") or [])
                if isinstance(one, dict)
            ],
            sfx=[
                SfxAsset.from_dict(one)
                for one in (_pick(data, "sfx", "sound_effects") or [])
                if isinstance(one, dict)
            ],
            music=[
                MusicAsset.from_dict(one)
                for one in (_pick(data, "music") or [])
                if isinstance(one, dict)
            ],
            default_profile_id=_pick(data, "default_profile_id", "defaultProfileId"),
            render_settings=_pick(data, "render_settings", "renderSettings") or {},
            params_snapshot=_pick(data, "_params_snapshot", "params_snapshot") or {},
        )


# ── Self-healing ───────────────────────────────────────────────────────────


def normalize_overrides(
    overrides: Optional[dict], has_profile: bool,
) -> Optional[dict]:
    """Drop ghost overrides.

    Without a voice profile an override has nothing to override, so it is
    discarded wholesale (this is what keeps "ghost runs" from surviving a
    profile deletion).  Empty strings and ``None`` values are dropped too, so
    an all-empty override object becomes ``None`` and stops splitting runs.
    """
    if not overrides or not has_profile or not isinstance(overrides, dict):
        return None
    cleaned: dict = {}
    for key in OVERRIDE_KEYS:
        value = overrides.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            value = value.strip()
            if not value:
                continue
        cleaned[key] = value
    return cleaned or None


def sanitize_blocks(
    blocks: list[Block], known_profile_ids: Optional[set[str]] = None,
) -> list[Block]:
    """Merge adjacent identical runs and heal dangling references.

    Applied on load *and* before save so old data heals itself:
      * a ``profile_id`` that no longer exists is cleared (deleted voice),
      * overrides on a profile-less run are discarded,
      * runs with the same (profile, overrides) are concatenated — this is what
        keeps the block list from fragmenting into thousands of one-word runs
        after a few edit/undo cycles,
      * a paragraph always keeps at least one run so the editor has a caret.
    """
    result: list[Block] = []
    for block in blocks:
        if not block.is_paragraph:
            result.append(block)
            continue
        merged: list[Run] = []
        for run in block.runs:
            profile_id = run.profile_id or None
            if known_profile_ids is not None and profile_id not in known_profile_ids:
                profile_id = None
            overrides = normalize_overrides(run.overrides, bool(profile_id))
            last = merged[-1] if merged else None
            if (
                last is not None
                and (last.profile_id or None) == profile_id
                and _override_key(last.overrides) == _override_key(overrides)
            ):
                last.text += run.text or ""
                continue
            merged.append(
                Run(
                    id=run.id or new_id(),
                    text=run.text or "",
                    profile_id=profile_id,
                    overrides=overrides,
                )
            )
        # Drop empty runs, but never leave a paragraph without one.
        merged = [one for one in merged if one.text] or [Run(text="")]
        block.runs = merged
        result.append(block)
    return result


def _override_key(overrides: Optional[dict]) -> str:
    """Order-independent comparison key for two override dicts."""
    if not overrides:
        return ""
    return json.dumps(overrides, sort_keys=True, ensure_ascii=False, default=str)


def sanitize_project(project: Project) -> Project:
    """Heal a whole project in place and return it.

    Beyond ``sanitize_blocks``: dangling chapter music / attached asset ids are
    cleared, and ``default_profile_id`` falls back to the first profile so a
    run without an explicit voice is always renderable.
    """
    profile_ids = {one.id for one in project.voice_profiles}
    sfx_ids = {one.id for one in project.sfx}
    music_ids = {one.id for one in project.music}

    if project.default_profile_id not in profile_ids:
        project.default_profile_id = (
            project.voice_profiles[0].id if project.voice_profiles else None
        )

    for chapter in project.chapters:
        if chapter.music_id not in music_ids:
            chapter.music_id = None
        kept: list[Block] = []
        for block in chapter.blocks:
            if block.type == BLOCK_SFX:
                # A sfx block whose asset is gone is dead weight, not content.
                if block.sfx_id in sfx_ids:
                    kept.append(block)
                continue
            attached = block.attached_sfx or None
            if attached and _pick(attached, "sfx_id", "sfxId") not in sfx_ids:
                block.attached_sfx = None
            attached_music = block.attached_music or None
            if attached_music and _pick(attached_music, "music_id", "musicId") not in music_ids:
                block.attached_music = None
            kept.append(block)
        chapter.blocks = sanitize_blocks(kept, profile_ids)
        if not chapter.blocks:
            chapter.blocks = [Block(runs=[Run(text="")])]
    project.version = MODEL_VERSION
    return project


# ── Derived values ─────────────────────────────────────────────────────────


def chapter_plain_text(chapter: Chapter) -> str:
    """Full text of a chapter, for search, export and word counts."""
    parts = []
    for block in chapter.blocks:
        if block.is_paragraph:
            parts.append(block.text())
        else:
            parts.append(f"[SFX:{block.sfx_id}]")
    return "\n\n".join(parts)


def count_words(chapter: Chapter) -> int:
    """Word count over paragraph text only (SFX markers do not count)."""
    return sum(
        len(_WORD_RE.findall(block.text()))
        for block in chapter.blocks
        if block.is_paragraph
    )


def estimate_duration_seconds(chapter: Chapter, words_per_minute: float = 150.0) -> float:
    """Rough runtime estimate for the UI, before anything is rendered.

    ponytail: flat words-per-minute, no per-voice speed. Calibrate wpm against
    a real render if the estimate drifts (audiobook narration is 140-160).
    """
    words = count_words(chapter)
    sfx_seconds = sum(1.0 for block in chapter.blocks if not block.is_paragraph)
    return words / max(1.0, words_per_minute) * 60.0 + sfx_seconds


def chapter_content_hash(project: Project, chapter: Chapter) -> str:
    """Stable SHA-256 over everything that changes the rendered chapter.

    Included: the chapter's blocks, the voice profiles actually referenced (a
    changed reference clip or temperature must invalidate the cache), the SFX
    and music assets actually used, and the project's render settings.

    Deliberately excluded: chapter title, ids of *unused* assets, and the
    cached audio fields themselves — otherwise the hash would change on every
    save and the render cache would never hit.
    """
    used_profiles: set[str] = set()
    used_sfx: set[str] = set()
    used_music: set[str] = set()
    if chapter.music_id:
        used_music.add(chapter.music_id)
    for block in chapter.blocks:
        if block.type == BLOCK_SFX:
            if block.sfx_id:
                used_sfx.add(block.sfx_id)
            continue
        for run in block.runs:
            profile = project.resolve_profile(run)
            if profile:
                used_profiles.add(profile.id)
        attached = block.attached_sfx or {}
        sfx_id = _pick(attached, "sfx_id", "sfxId")
        if sfx_id:
            used_sfx.add(str(sfx_id))
        attached_music = block.attached_music or {}
        music_id = _pick(attached_music, "music_id", "musicId")
        if music_id:
            used_music.add(str(music_id))

    payload = {
        "v": MODEL_VERSION,
        "blocks": [block.to_dict() for block in chapter.blocks],
        "language": chapter.language or project.language,
        "profiles": [
            one.to_dict() for one in project.voice_profiles if one.id in used_profiles
        ],
        "sfx": [one.to_dict() for one in project.sfx if one.id in used_sfx],
        "music": [one.to_dict() for one in project.music if one.id in used_music],
        "render_settings": project.render_settings,
    }
    encoded = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def chapter_is_cached(project: Project, chapter: Chapter) -> bool:
    """Whether ``chapter.audio_path`` still matches the current content."""
    return bool(
        chapter.audio_path
        and chapter.audio_hash
        and chapter.audio_hash == chapter_content_hash(project, chapter)
    )


def iter_speech_runs(chapter: Chapter):
    """Yield ``(block, run)`` for every non-empty speech run, in order."""
    for block in chapter.blocks:
        if not block.is_paragraph:
            continue
        for run in block.runs:
            if run.text and run.text.strip():
                yield block, run


def new_paragraph(text: str = "") -> Block:
    return Block(type=BLOCK_PARAGRAPH, runs=[Run(text=text)])


def new_sfx_block(sfx_id: str) -> Block:
    return Block(type=BLOCK_SFX, sfx_id=sfx_id)


if __name__ == "__main__":
    # Self-check: sanitize (the self-healing rules) + content hash stability.
    # `python -m services.audiobook.model` from app/.

    narrator = VoiceProfile(id="p1", name="Narrator", model_type="index_tts2")
    villain = VoiceProfile(id="p2", name="Villain", model_type="index_tts2")

    # 1. Adjacent runs with the same profile+overrides merge into one.
    block = Block(
        runs=[
            Run(id="r1", text="Hello ", profile_id="p1"),
            Run(id="r2", text="world.", profile_id="p1"),
            Run(id="r3", text=" Never!", profile_id="p2", overrides={"emotion": "angry"}),
        ]
    )
    merged = sanitize_blocks([block], {"p1", "p2"})[0]
    assert len(merged.runs) == 2, [r.to_dict() for r in merged.runs]
    assert merged.runs[0].text == "Hello world.", merged.runs[0].text
    assert merged.runs[1].overrides == {"emotion": "angry"}

    # 2. Overrides without a profile are discarded, and the run then merges
    #    with its plain neighbour (this is the "ghost run" bug being healed).
    ghost = Block(
        runs=[
            Run(id="a", text="one "),
            Run(id="b", text="two", overrides={"emotion": "sad"}),
        ]
    )
    healed = sanitize_blocks([ghost], set())[0]
    assert len(healed.runs) == 1, [r.to_dict() for r in healed.runs]
    assert healed.runs[0].overrides is None
    assert healed.runs[0].text == "one two"

    # 3. Empty overrides ({} / whitespace) must not split a run either.
    empty_ov = Block(
        runs=[
            Run(text="a", profile_id="p1", overrides={"emotion": "  "}),
            Run(text="b", profile_id="p1", overrides={}),
        ]
    )
    assert len(sanitize_blocks([empty_ov], {"p1"})[0].runs) == 1

    # 4. A paragraph never ends up with zero runs.
    assert len(sanitize_blocks([Block(runs=[])], set())[0].runs) == 1

    # 5. A deleted voice clears profile_id (and therefore its overrides).
    dangling = Block(runs=[Run(text="x", profile_id="gone", overrides={"emotion": "sad"})])
    fixed = sanitize_blocks([dangling], {"p1"})[0]
    assert fixed.runs[0].profile_id is None and fixed.runs[0].overrides is None

    # 6. sanitize_project: dangling asset refs and default profile.
    project = Project(
        id="proj", title="T",
        voice_profiles=[narrator, villain],
        sfx=[SfxAsset(id="s1", label="rain", playback_mode="parallel")],
        chapters=[
            Chapter(
                id="c1", title="One", music_id="nope",
                blocks=[
                    Block(runs=[Run(text="Hi", profile_id="p1")],
                          attached_sfx={"sfx_id": "s1", "volume": 0.4}),
                    Block(runs=[Run(text="Bye", profile_id="p1")],
                          attached_sfx={"sfx_id": "missing"}),
                    Block(type=BLOCK_SFX, sfx_id="s1"),
                    Block(type=BLOCK_SFX, sfx_id="ghost"),
                ],
            )
        ],
        default_profile_id="does-not-exist",
    )
    sanitize_project(project)
    chapter = project.chapters[0]
    assert project.default_profile_id == "p1"
    assert chapter.music_id is None
    assert chapter.blocks[0].attached_sfx == {"sfx_id": "s1", "volume": 0.4}
    assert chapter.blocks[1].attached_sfx is None
    assert len(chapter.blocks) == 3, [b.to_dict() for b in chapter.blocks]

    # 7. Text / word count.
    assert chapter_plain_text(chapter) == "Hi\n\nBye\n\n[SFX:s1]"
    assert count_words(chapter) == 2
    assert len(list(iter_speech_runs(chapter))) == 2

    # 8. Content hash: stable across round-trip, sensitive to edits, blind to
    #    title changes and unused assets.
    hash_before = chapter_content_hash(project, chapter)
    round_tripped = Project.from_dict(json.loads(json.dumps(project.to_dict())))
    assert chapter_content_hash(round_tripped, round_tripped.chapters[0]) == hash_before

    chapter.title = "Renamed"
    assert chapter_content_hash(project, chapter) == hash_before, "title must not count"

    project.sfx.append(SfxAsset(id="unused", label="unused"))
    assert chapter_content_hash(project, chapter) == hash_before, "unused asset must not count"

    chapter.blocks[0].runs[0].text = "Hi there"
    assert chapter_content_hash(project, chapter) != hash_before, "text edit must invalidate"

    hash_after_text = chapter_content_hash(project, chapter)
    narrator.params["temperature"] = 0.9
    assert chapter_content_hash(project, chapter) != hash_after_text, "voice param must invalidate"

    # 9. Cache predicate.
    chapter.audio_path, chapter.audio_hash = "/x/c1.wav", chapter_content_hash(project, chapter)
    assert chapter_is_cached(project, chapter)
    chapter.blocks[0].runs[0].text += "!"
    assert not chapter_is_cached(project, chapter)

    # A stored seed survives the JSON round-trip; unusable values read as
    # "not pinned" rather than as a seed of 0, which the generator would treat
    # as "pick one for me" while the voice looked pinned.
    pinned = VoiceProfile.from_dict({"id": "v", "seed": "1234"})
    assert pinned.seed == 1234
    assert VoiceProfile.from_dict(pinned.to_dict()).seed == 1234
    for bad in (0, -1, None, "", "abc", {}):
        assert VoiceProfile.from_dict({"id": "v", "seed": bad}).seed is None, bad

    print("audiobook.model self-check OK")
