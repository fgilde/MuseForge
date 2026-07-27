"""TTS bridge — one Run + VoiceProfile + emotion → one generation request.

Pure planning only.  ``plan_run()`` returns the dict that ``POST
/api/v1/generate`` accepts verbatim (``launch.py:8009`` stores the request body
as ``job["params"]``), so wiring the endpoints is "post this dict, poll the
job".  Nothing here submits, loads a model or touches the GPU.

──────────────────────────────────────────────────────────────────────────────
PARAMETER MAPPING PER MODEL
──────────────────────────────────────────────────────────────────────────────
Common to all: ``model_type``, ``prompt`` (the run text), ``seed``,
``video_length: 0``, ``num_inference_steps: 0``, ``negative_prompt: ""``,
``repeat_generation: 1``, ``multi_prompts_gen_type: 2`` (keep the whole run in
ONE prompt — with 0/1/3 wgp splits on newlines and emits several clips, which
would fragment a paragraph, see ``wgp.py:7113``).

index_tts2  (models/TTS/index_tts2_handler.py)
    Voice cloning     audio_prompt_type "A"  + audio_guide  = voice_ref_path
                      audio_prompt_type "AB" + audio_guide2 = emotion_ref_path
                      (mode "AB2" = two-speaker dialogue; unused per-run —
                      the audiobook assigns one voice per run instead)
    Emotion           NATIVE: ``[happy] text`` tags in the prompt, one per
                      sentence.  ``alt_prompt`` carries a default emotion
                      instruction for the whole request.
    Sampling          temperature, top_p, top_k
    Length            duration_seconds (max, 1..600), pause_seconds
    Required          audio_guide — the handler rejects the job without it.

kugelaudio_0_open  (models/TTS/kugelaudio_handler.py)
    Voice cloning     audio_prompt_type ""  = text only (built-in voice)
                      audio_prompt_type "A" + audio_guide = voice_ref_path
    Emotion           NOT SUPPORTED. No emotion tags, no instruction field.
                      Fallback: the emotion is dropped from the text (a literal
                      "[sad]" would be READ ALOUD) and a warning is returned;
                      temperature is nudged up for high-arousal emotions as the
                      only available expressiveness knob.
    Sampling          temperature, guidance_scale
    Length            duration_seconds, pause_seconds
    Note              long single runs accelerate; custom_settings
                      {"auto_split_every_s": 30} is the handler's own remedy.

chatterbox  (models/TTS/chatterbox_handler.py)
    Voice cloning     audio_prompt_type "A" + audio_guide = voice_ref_path
    Language          model_mode = ISO code ("en", "de", …) — per MODEL, so a
                      profile is bound to one language.
    Emotion           PARTIAL: custom_settings {"exaggeration", "pace"}.
                      Emotion maps to an exaggeration value (0.25..2.0,
                      0.5 = neutral); no per-word control.
    Sampling          temperature
    Note              no duration_seconds — the model has no length slider.

qwen3_tts_voicedesign  (models/TTS/qwen3_handler.py)
    Voice cloning     none — the voice is DESCRIBED, not cloned.
    Voice/Emotion     alt_prompt = natural-language voice instruction.  Emotion
                      is appended to it ("…, speaking in a sad tone"), which is
                      the model's intended emotion channel.
    Language          model_mode = language code or "auto"
    Sampling          temperature, top_k
    Length            duration_seconds

qwen3_tts_customvoice  (models/TTS/qwen3_handler.py)
    Voice cloning     none — model_mode selects a built-in speaker preset
                      ("serena", …); ``params["speaker"]`` chooses it.
    Emotion           via alt_prompt instruction, same as voicedesign.
    Sampling          temperature, top_k
    Length            duration_seconds

Not mapped here: ``qwen3_tts_base`` (cloning + reference transcript in
alt_prompt) — add an entry to ``MODEL_CAPS`` if a voice profile ever needs it.

Determinism: the seed comes from ``run_id|voice_id|emotion`` (SHA-256 truncated
to 31 bits), so the paragraph preview and the chapter export request the exact
same generation.  Change any of the three and you get a new performance; change
nothing and it sounds identical.

Self-check: ``python -m services.audiobook.tts`` from ``app/``.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from services.audiobook.model import Project, Run, VoiceProfile

# Seed space: wgp treats -1 as "random", so stay strictly positive and inside
# the signed 32-bit range every backend accepts.
_SEED_MODULUS = 0x7FFF_FFFF

# IndexTTS2's own documented emotion vocabulary (defaults/index_tts2.json and
# the handler's alt_prompt placeholder).  Free text is passed through as-is —
# the model detects emotion from the tag semantically.
INDEX_TTS2_EMOTIONS = (
    "happy", "angry", "sad", "afraid", "disgusted", "melancholic",
    "surprised", "calm", "fear", "sadness", "disgust", "anger",
)

# Emotion → Chatterbox exaggeration (0.25 neutral-flat … 2.0 theatrical;
# 0.5 is the model's neutral).  ponytail: hand-tuned table, not learned —
# calibrate against a listen test, the numbers are meant to be edited.
_CHATTERBOX_EXAGGERATION = {
    "calm": 0.35, "tender": 0.4, "whispering": 0.35, "melancholic": 0.45,
    "sad": 0.5, "sadness": 0.5, "neutral": 0.5,
    "surprised": 0.8, "excited": 0.9, "happy": 0.75,
    "afraid": 0.85, "fear": 0.85, "angry": 1.1, "anger": 1.1,
    "disgusted": 0.7, "disgust": 0.7,
}

# High-arousal emotions get a small temperature bump on models with no emotion
# channel at all.  It is not emotion control; it is the only lever there is.
_HIGH_AROUSAL = {"angry", "anger", "excited", "afraid", "fear", "surprised"}

_WORD_RE = re.compile(r"\S+")
# Strip any emotion tag already in the text so it cannot be spoken aloud by a
# model that does not understand it.
_TAG_RE = re.compile(r"\[[^\[\]\n]{1,40}\]")


@dataclass(frozen=True)
class ModelCaps:
    """What one TTS model can actually do — drives the mapping, not ifs."""

    model_type: str
    emotion: str            # "native_tag" | "instruction" | "param" | "none"
    clone: str              # "required" | "optional" | "none"
    supports_emotion_ref: bool = False
    supports_duration: bool = True
    supports_pause: bool = False
    sampling: tuple[str, ...] = ()
    language_key: Optional[str] = None   # which param carries the language
    max_duration: float = 600.0
    default_params: dict = field(default_factory=dict)


MODEL_CAPS: dict[str, ModelCaps] = {
    "index_tts2": ModelCaps(
        model_type="index_tts2",
        emotion="native_tag",
        clone="required",
        supports_emotion_ref=True,
        supports_pause=True,
        sampling=("temperature", "top_p", "top_k"),
        default_params={"temperature": 0.8, "top_p": 0.8, "top_k": 30},
    ),
    "kugelaudio_0_open": ModelCaps(
        model_type="kugelaudio_0_open",
        emotion="none",
        clone="optional",
        supports_pause=True,
        sampling=("temperature", "guidance_scale"),
        default_params={"temperature": 1.0, "guidance_scale": 3.0},
    ),
    "chatterbox": ModelCaps(
        model_type="chatterbox",
        emotion="param",
        clone="optional",
        supports_duration=False,
        sampling=("temperature",),
        language_key="model_mode",
        default_params={"temperature": 0.8},
    ),
    "qwen3_tts_voicedesign": ModelCaps(
        model_type="qwen3_tts_voicedesign",
        emotion="instruction",
        clone="none",
        sampling=("temperature", "top_k"),
        language_key="model_mode",
        default_params={"temperature": 0.9, "top_k": 50},
    ),
    "qwen3_tts_customvoice": ModelCaps(
        model_type="qwen3_tts_customvoice",
        emotion="instruction",
        clone="none",
        sampling=("temperature", "top_k"),
        default_params={"temperature": 0.9, "top_k": 50},
    ),
}

SUPPORTED_MODEL_TYPES = tuple(MODEL_CAPS)


@dataclass
class TtsPlan:
    """One planned generation call for one run."""

    run_id: str
    model_type: str
    params: dict
    seed: int
    emotion: Optional[str] = None
    emotion_mode: str = "none"
    warnings: list[str] = field(default_factory=list)
    estimated_seconds: float = 0.0

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "model_type": self.model_type,
            "params": dict(self.params),
            "seed": self.seed,
            "emotion": self.emotion,
            "emotion_mode": self.emotion_mode,
            "warnings": list(self.warnings),
            "estimated_seconds": self.estimated_seconds,
        }


class TtsPlanError(ValueError):
    """The run cannot be rendered as configured (missing voice, unknown model)."""


# ── Deterministic seed ─────────────────────────────────────────────────────


def derive_seed(run_id: str, voice_id: str, emotion: Optional[str] = None) -> int:
    """Stable positive seed from ``run_id|voice_id|emotion``.

    SHA-256 rather than FNV-1a: same length of code, no hand-rolled arithmetic
    to get wrong, and it stays stable across Python versions (``hash()`` does
    not — PYTHONHASHSEED randomises str hashing, which would make preview and
    export differ between processes).
    """
    key = f"{run_id}|{voice_id}|{(emotion or '').strip().lower()}"
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    # 4 bytes is plenty; mask to 31 bits so the value is always positive.
    value = int.from_bytes(digest[:4], "big") & _SEED_MODULUS
    return value or 1


# ── Text / emotion helpers ─────────────────────────────────────────────────


def resolve_emotion(run: Run, profile: Optional[VoiceProfile]) -> Optional[str]:
    """Run override wins over the profile's default emotion."""
    overrides = run.overrides or {}
    emotion = overrides.get("emotion")
    if isinstance(emotion, str) and emotion.strip():
        return emotion.strip()
    if profile and profile.default_emotion and profile.default_emotion.strip():
        return profile.default_emotion.strip()
    return None


def estimate_speech_seconds(text: str, *, words_per_second: float = 2.5) -> float:
    """Conservative length estimate used to size ``duration_seconds``.

    Too small truncates the audio, so it errs high: 2.5 wps is faster than
    audiobook narration (~2.3), plus a 2.5 s floor and 25 % headroom.
    """
    words = len(_WORD_RE.findall(text or ""))
    return max(2.5, words / max(0.5, words_per_second) * 1.25)


def strip_emotion_tags(text: str) -> str:
    """Remove ``[tag]`` markers, for models that would read them aloud."""
    return re.sub(r"\s{2,}", " ", _TAG_RE.sub("", text or "")).strip()


def apply_emotion_tag(text: str, emotion: Optional[str]) -> str:
    """Prefix an IndexTTS2 emotion tag, one per line (its native syntax).

    Per-line rather than once at the top: IndexTTS2 detects/applies emotion per
    sentence, and a single leading tag would only colour the first one.
    """
    cleaned = strip_emotion_tags(text)
    if not emotion:
        return cleaned
    tag = f"[{emotion.strip().lower()}]"
    lines = [one.strip() for one in cleaned.split("\n") if one.strip()]
    return "\n".join(f"{tag} {one}" for one in lines) if lines else cleaned


def emotion_instruction(base: str, emotion: Optional[str]) -> str:
    """Merge an emotion into a Qwen3 natural-language voice instruction."""
    base = (base or "").strip().rstrip(",.")
    if not emotion:
        return base
    phrase = f"speaking in a {emotion.strip().lower()} tone"
    return f"{base}, {phrase}" if base else phrase.capitalize()


def chatterbox_exaggeration(emotion: Optional[str], default: float = 0.5) -> float:
    """Emotion → Chatterbox exaggeration, clamped to the documented range."""
    if not emotion:
        return default
    value = _CHATTERBOX_EXAGGERATION.get(emotion.strip().lower(), default)
    return max(0.25, min(2.0, value))


# ── Planning ───────────────────────────────────────────────────────────────


def _sampling_params(caps: ModelCaps, profile: VoiceProfile, run: Run) -> dict:
    """Model defaults ← profile params ← run overrides, for allowed keys only."""
    values = dict(caps.default_params)
    for key in caps.sampling:
        if key in profile.params and profile.params[key] is not None:
            values[key] = profile.params[key]
    overrides = run.overrides or {}
    # "style"/"stability"/"speed" are ElevenLabs-shaped; only temperature has a
    # meaningful local equivalent, so that is the only one honoured here.
    if overrides.get("stability") is not None and "temperature" in caps.sampling:
        # ElevenLabs stability 0..1: high stability = low variance.
        try:
            values["temperature"] = round(
                max(0.1, min(1.5, 1.4 - float(overrides["stability"]) * 0.9)), 3,
            )
        except (TypeError, ValueError):
            pass
    return values


def plan_run(
    project: Project,
    run: Run,
    *,
    profile: Optional[VoiceProfile] = None,
    workspace: Optional[str] = None,
    language: Optional[str] = None,
    pause_seconds: float = 0.2,
) -> TtsPlan:
    """Map one run onto a concrete ``/api/v1/generate`` request body.

    Raises ``TtsPlanError`` when the run cannot be rendered — an unknown model,
    or a cloning-only model without a reference clip.  Anything merely degraded
    (an emotion the model cannot express) comes back as a warning so the render
    proceeds and the UI can say what was dropped.
    """
    text = (run.text or "").strip()
    if not text:
        raise TtsPlanError("Run has no text to speak.")

    profile = profile or project.resolve_profile(run)
    if profile is None:
        raise TtsPlanError(
            "No voice profile for this run and no project default is set."
        )

    overrides = run.overrides or {}
    model_type = str(overrides.get("model_type") or profile.model_type or "")
    caps = MODEL_CAPS.get(model_type)
    if caps is None:
        raise TtsPlanError(
            f"Unsupported TTS model '{model_type}'. Supported: "
            + ", ".join(SUPPORTED_MODEL_TYPES)
        )

    emotion = resolve_emotion(run, profile)
    warnings: list[str] = []
    seed = derive_seed(run.id, profile.id, emotion)
    estimated = estimate_speech_seconds(text)
    lang = language or profile.params.get("language") or project.language or "en"

    params: dict[str, Any] = {
        "model_type": model_type,
        "prompt": strip_emotion_tags(text),
        "seed": seed,
        "negative_prompt": "",
        "repeat_generation": 1,
        # Keep the run in ONE prompt — see the module docstring.
        "multi_prompts_gen_type": 2,
        "video_length": 0,
        "num_inference_steps": 0,
    }
    if workspace:
        params["workspace"] = workspace

    # -- voice reference ------------------------------------------------
    voice_ref = profile.voice_ref_path
    if caps.clone == "required" and not voice_ref:
        raise TtsPlanError(
            f"Voice '{profile.name}' uses {model_type}, which requires a "
            "reference voice clip (voice_ref_path)."
        )
    if caps.clone == "none" and voice_ref:
        warnings.append(
            f"{model_type} cannot clone a voice; the reference clip on "
            f"'{profile.name}' is ignored."
        )
    if caps.clone in ("required", "optional") and voice_ref:
        params["audio_guide"] = voice_ref
        params["audio_prompt_type"] = "A"
        if caps.supports_emotion_ref and profile.emotion_ref_path:
            # IndexTTS2 "AB": second clip supplies the emotion, not a speaker.
            params["audio_guide2"] = profile.emotion_ref_path
            params["audio_prompt_type"] = "AB"
    elif caps.clone == "optional":
        params["audio_prompt_type"] = ""

    # -- emotion --------------------------------------------------------
    emotion_mode = caps.emotion if emotion else "none"
    if emotion and caps.emotion == "native_tag":
        params["prompt"] = apply_emotion_tag(text, emotion)
        params["alt_prompt"] = str(profile.params.get("emotion_instruction") or "")
    elif caps.emotion == "instruction":
        params["alt_prompt"] = emotion_instruction(
            str(
                profile.params.get("voice_instruction")
                or profile.params.get("instruction")
                or ""
            ),
            emotion,
        )
    elif emotion and caps.emotion == "param":
        custom = dict(profile.params.get("custom_settings") or {})
        custom["exaggeration"] = chatterbox_exaggeration(emotion)
        custom.setdefault("pace", profile.params.get("pace", 0.5))
        params["custom_settings"] = custom
        warnings.append(
            f"{model_type} has no emotion tags; '{emotion}' was mapped to "
            f"exaggeration={custom['exaggeration']}."
        )
    elif emotion and caps.emotion == "none":
        warnings.append(
            f"{model_type} does not support emotion; '{emotion}' was dropped "
            "(the tag would otherwise be read aloud). Use IndexTTS2 or a Qwen3 "
            "voice for emotional delivery."
        )
    elif caps.emotion == "param":
        custom = dict(profile.params.get("custom_settings") or {})
        custom.setdefault("exaggeration", profile.params.get("exaggeration", 0.5))
        custom.setdefault("pace", profile.params.get("pace", 0.5))
        params["custom_settings"] = custom

    # -- sampling / length / language -----------------------------------
    params.update(_sampling_params(caps, profile, run))
    if emotion and caps.emotion == "none" and emotion.strip().lower() in _HIGH_AROUSAL:
        if "temperature" in params:
            params["temperature"] = round(min(1.4, float(params["temperature"]) * 1.15), 3)

    if caps.supports_duration:
        params["duration_seconds"] = int(min(caps.max_duration, max(2.0, estimated)))
    if caps.supports_pause:
        params["pause_seconds"] = float(pause_seconds)
    if caps.language_key:
        params[caps.language_key] = lang
    if model_type == "qwen3_tts_customvoice":
        speaker = profile.params.get("speaker")
        if not speaker:
            raise TtsPlanError(
                f"Voice '{profile.name}' uses {model_type}, which needs a "
                "built-in speaker preset in params['speaker']."
            )
        params["model_mode"] = str(speaker)

    # Speed override: no local model exposes a rate control, so say so once
    # instead of silently ignoring it.
    if overrides.get("speed") is not None:
        warnings.append(
            "Per-run speed is not supported by the local TTS models; use "
            "Chatterbox 'pace' on the voice profile instead."
        )

    return TtsPlan(
        run_id=run.id,
        model_type=model_type,
        params=params,
        seed=seed,
        emotion=emotion,
        emotion_mode=emotion_mode,
        warnings=warnings,
        estimated_seconds=estimated,
    )


def plan_chapter(
    project: Project,
    chapter,
    *,
    workspace: Optional[str] = None,
    pause_seconds: float = 0.2,
) -> tuple[list[TtsPlan], list[str]]:
    """Plan every speech run of a chapter, in reading order.

    Returns ``(plans, errors)``.  A run that cannot be planned becomes a string
    in ``errors`` instead of aborting the chapter, so the UI can list exactly
    which paragraphs need a voice before the render starts.
    """
    from services.audiobook.model import iter_speech_runs

    plans: list[TtsPlan] = []
    errors: list[str] = []
    for block, run in iter_speech_runs(chapter):
        try:
            plans.append(
                plan_run(
                    project, run,
                    workspace=workspace,
                    language=chapter.language or project.language,
                    pause_seconds=pause_seconds,
                )
            )
        except TtsPlanError as exc:
            errors.append(f"block {block.id} / run {run.id}: {exc}")
    return plans, errors


if __name__ == "__main__":
    # Self-check: seed determinism, per-model mapping, emotion handling and
    # the failure modes.  `python -m services.audiobook.tts` from app/.
    from services.audiobook.model import Block, Chapter

    # 1. Seed is deterministic, positive, and sensitive to all three inputs.
    seed_a = derive_seed("run1", "voice1", "sad")
    assert seed_a == derive_seed("run1", "voice1", "sad")
    assert seed_a == derive_seed("run1", "voice1", " SAD ")      # normalised
    assert 0 < seed_a <= _SEED_MODULUS
    assert derive_seed("run1", "voice1", "happy") != seed_a
    assert derive_seed("run2", "voice1", "sad") != seed_a
    assert derive_seed("run1", "voice2", "sad") != seed_a
    assert derive_seed("run1", "voice1") == derive_seed("run1", "voice1", "")

    # 2. IndexTTS2: native emotion tags, one per line; cloning via audio_guide.
    index_voice = VoiceProfile(
        id="v-index", name="Narrator", model_type="index_tts2",
        voice_ref_path="/refs/narrator.wav",
    )
    project = Project(
        id="p", title="Book", language="de",
        voice_profiles=[index_voice], default_profile_id="v-index",
    )
    run = Run(id="r1", text="Er kam näher.\nDann blieb er stehen.",
              profile_id="v-index", overrides={"emotion": "afraid"})
    plan = plan_run(project, run, workspace="ws")
    assert plan.params["model_type"] == "index_tts2"
    assert plan.params["audio_prompt_type"] == "A"
    assert plan.params["audio_guide"] == "/refs/narrator.wav"
    assert plan.params["prompt"] == "[afraid] Er kam näher.\n[afraid] Dann blieb er stehen.", \
        plan.params["prompt"]
    assert plan.params["seed"] == derive_seed("r1", "v-index", "afraid")
    assert plan.params["multi_prompts_gen_type"] == 2
    assert plan.params["workspace"] == "ws"
    assert plan.params["temperature"] == 0.8 and plan.params["top_k"] == 30
    assert plan.params["duration_seconds"] >= 3
    assert plan.params["pause_seconds"] == 0.2
    assert plan.emotion_mode == "native_tag" and not plan.warnings

    # 3. IndexTTS2 with an emotion reference clip switches to "AB".
    index_voice.emotion_ref_path = "/refs/angry.wav"
    plan_ab = plan_run(project, run)
    assert plan_ab.params["audio_prompt_type"] == "AB"
    assert plan_ab.params["audio_guide2"] == "/refs/angry.wav"
    index_voice.emotion_ref_path = None

    # 4. Preview and export produce byte-identical requests (determinism).
    assert plan_run(project, run).params == plan_run(project, run).params

    # 5. A literal tag already in the text is not doubled.
    tagged = Run(id="r2", text="[happy] Schon getaggt.", profile_id="v-index",
                 overrides={"emotion": "sad"})
    assert plan_run(project, tagged).params["prompt"] == "[sad] Schon getaggt."

    # 6. IndexTTS2 without a reference clip is a hard error (the handler
    #    rejects such a job anyway — better to say so before submitting).
    project.voice_profiles = [VoiceProfile(id="v-index", name="N", model_type="index_tts2")]
    project.default_profile_id = "v-index"
    try:
        plan_run(project, Run(id="r3", text="Hallo", profile_id="v-index"))
    except TtsPlanError as exc:
        assert "reference voice" in str(exc), exc
    else:
        raise AssertionError("expected TtsPlanError for missing voice_ref_path")

    # 7. KugelAudio: no emotion channel → tag dropped, warning, temp nudged.
    kugel = VoiceProfile(id="v-kugel", name="Kugel", model_type="kugelaudio_0_open")
    project = Project(id="p", voice_profiles=[kugel], default_profile_id="v-kugel")
    kugel_plan = plan_run(
        project,
        Run(id="r4", text="Sofort raus!", profile_id="v-kugel",
            overrides={"emotion": "angry"}),
    )
    assert kugel_plan.params["prompt"] == "Sofort raus!"
    assert "[angry]" not in kugel_plan.params["prompt"]
    assert kugel_plan.params["audio_prompt_type"] == ""
    assert "audio_guide" not in kugel_plan.params
    assert kugel_plan.params["guidance_scale"] == 3.0
    assert kugel_plan.params["temperature"] > 1.0, kugel_plan.params["temperature"]
    assert any("does not support emotion" in one for one in kugel_plan.warnings)

    kugel.voice_ref_path = "/refs/k.wav"
    assert plan_run(project, Run(id="r5", text="Hi", profile_id="v-kugel")) \
        .params["audio_prompt_type"] == "A"

    # 8. Chatterbox: emotion → exaggeration, language → model_mode.
    chatter = VoiceProfile(
        id="v-cb", name="CB", model_type="chatterbox",
        voice_ref_path="/refs/cb.wav", params={"language": "de"},
    )
    project = Project(id="p", voice_profiles=[chatter], default_profile_id="v-cb")
    cb_plan = plan_run(
        project,
        Run(id="r6", text="Raus hier!", profile_id="v-cb",
            overrides={"emotion": "angry"}),
    )
    assert cb_plan.params["model_mode"] == "de"
    assert cb_plan.params["custom_settings"]["exaggeration"] == 1.1
    assert cb_plan.params["custom_settings"]["pace"] == 0.5
    assert "duration_seconds" not in cb_plan.params, "chatterbox has no length slider"
    assert cb_plan.emotion_mode == "param"
    # Neutral run still gets valid custom_settings.
    assert plan_run(project, Run(id="r7", text="Hallo", profile_id="v-cb")) \
        .params["custom_settings"]["exaggeration"] == 0.5
    assert chatterbox_exaggeration("calm") == 0.35
    assert chatterbox_exaggeration("unbekannt") == 0.5

    # 9. Qwen3 voice design: emotion merged into the natural-language
    #    instruction; a reference clip is reported as ignored.
    design = VoiceProfile(
        id="v-qd", name="Designed", model_type="qwen3_tts_voicedesign",
        voice_ref_path="/refs/ignored.wav",
        params={"voice_instruction": "young female, warm tone", "language": "de"},
    )
    project = Project(id="p", voice_profiles=[design], default_profile_id="v-qd")
    qd_plan = plan_run(
        project,
        Run(id="r8", text="Guten Tag.", profile_id="v-qd",
            overrides={"emotion": "melancholic"}),
    )
    assert qd_plan.params["alt_prompt"] == \
        "young female, warm tone, speaking in a melancholic tone", qd_plan.params["alt_prompt"]
    assert qd_plan.params["model_mode"] == "de"
    assert "audio_guide" not in qd_plan.params
    assert any("cannot clone" in one for one in qd_plan.warnings)
    assert qd_plan.params["top_k"] == 50

    # 10. Qwen3 custom voice needs a speaker preset.
    custom = VoiceProfile(id="v-qc", name="Serena", model_type="qwen3_tts_customvoice")
    project = Project(id="p", voice_profiles=[custom], default_profile_id="v-qc")
    try:
        plan_run(project, Run(id="r9", text="Hallo", profile_id="v-qc"))
    except TtsPlanError as exc:
        assert "speaker preset" in str(exc), exc
    else:
        raise AssertionError("expected TtsPlanError for missing speaker")
    custom.params["speaker"] = "serena"
    qc_plan = plan_run(project, Run(id="r10", text="Hallo", profile_id="v-qc"))
    assert qc_plan.params["model_mode"] == "serena"

    # 11. Unknown model and unknown-model overrides fail loudly.
    broken = VoiceProfile(id="v-x", name="X", model_type="elevenlabs")
    project = Project(id="p", voice_profiles=[broken], default_profile_id="v-x")
    try:
        plan_run(project, Run(id="r11", text="Hi", profile_id="v-x"))
    except TtsPlanError as exc:
        assert "Unsupported TTS model" in str(exc), exc
    else:
        raise AssertionError("expected TtsPlanError for unknown model")

    # 12. Profile fallback: a run with no profile_id uses the project default.
    index_voice = VoiceProfile(
        id="v-def", name="Default", model_type="index_tts2",
        voice_ref_path="/refs/d.wav",
    )
    project = Project(id="p", voice_profiles=[index_voice], default_profile_id="v-def")
    assert plan_run(project, Run(id="r12", text="Ohne Profil")).params["audio_guide"] \
        == "/refs/d.wav"

    # 13. Chapter planning collects per-run errors instead of aborting.
    chapter = Chapter(
        id="c1", title="One",
        blocks=[
            Block(runs=[Run(id="ok", text="Erster Satz.", profile_id="v-def")]),
            Block(runs=[Run(id="bad", text="Zweiter.", profile_id="v-missing")]),
            Block(runs=[Run(id="blank", text="   ", profile_id="v-def")]),
        ],
    )
    project.voice_profiles.append(VoiceProfile(id="v-missing", name="M", model_type="index_tts2"))
    plans, errors = plan_chapter(project, chapter)
    assert [one.run_id for one in plans] == ["ok"], [one.run_id for one in plans]
    assert len(errors) == 1 and "v-missing" not in errors[0] and "run bad" in errors[0], errors

    # 14. Length estimate errs high, never below the floor.
    assert estimate_speech_seconds("") == 2.5
    assert estimate_speech_seconds(" ".join(["wort"] * 100)) > 40

    # 15. Stability override maps onto temperature (inverted).
    stable = plan_run(
        project,
        Run(id="r13", text="Ruhig.", profile_id="v-def", overrides={"stability": 1.0}),
    )
    jittery = plan_run(
        project,
        Run(id="r14", text="Ruhig.", profile_id="v-def", overrides={"stability": 0.0}),
    )
    assert stable.params["temperature"] < jittery.params["temperature"]

    print("audiobook.tts self-check OK")
