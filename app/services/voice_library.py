"""A workspace-wide library of named voices.

Reference clips, TTS engine and parameters live here once and are reused
everywhere: an audiobook project imports a voice instead of re-uploading the
same recording, and Audio → Speech can read a line with a voice that was
auditioned in the library.

Stored as one JSON file per workspace (`_voices.json`) with the same atomic
temp+os.replace write the pipelines use. Audio files are NOT copied into the
library — the entry points at wherever the upload landed, so nothing is
duplicated on disk. A missing file therefore has to be reported rather than
assumed, which `sanitize_entry` does.
"""

import json
import os
import random
import threading
import time
import uuid

LIBRARY_FILENAME = "_voices.json"
LIBRARY_VERSION = 1

# ponytail: one lock for the whole file — it is a handful of small entries,
# per-entry locking would buy nothing.
_lock = threading.Lock()

# Engines a library voice can be bound to, and what each one can do. The UI
# uses this to show only controls that have an effect.
ENGINES = {
    "index_tts2": {"label": "IndexTTS2", "clone": True, "emotion": "native",
                   "needs_reference": True},
    "kugelaudio_0_open": {"label": "KugelAudio 7B", "clone": True, "emotion": "none",
                          "needs_reference": False},
    "chatterbox": {"label": "Chatterbox (multilingual)", "clone": True,
                   "emotion": "partial", "needs_reference": True},
    "qwen3_tts_voicedesign": {"label": "Qwen3 Voice Design", "clone": False,
                              "emotion": "instruction", "needs_reference": False},
    "qwen3_tts_customvoice": {"label": "Qwen3 Custom Voice", "clone": False,
                              "emotion": "instruction", "needs_reference": False},
}

SWATCHES = ["#22d3ee", "#a78bfa", "#f472b6", "#4ade80", "#fb923c",
            "#facc15", "#60a5fa", "#f87171"]

# 31 bits, matching the seed range the generation params accept.
_SEED_MAX = 0x7FFFFFFF


def new_seed() -> int:
    """A fresh voice identity.

    Every voice gets one at creation and keeps it. For the engines that build
    a speaker from a written description (Qwen3 Voice Design / Custom Voice)
    the seed decides who that speaker is, so a voice without a fixed seed
    sounds like a different person in every paragraph — which is exactly what
    it used to do. Re-rolling is therefore an explicit action, not a
    side effect of previewing again.
    """
    return random.randint(1, _SEED_MAX)


def library_path(out_dir: str) -> str:
    return os.path.join(out_dir, LIBRARY_FILENAME)


def _write_atomic(path: str, payload: dict) -> None:
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False, default=str)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def new_entry(name: str = "", model_type: str = "index_tts2", *,
              color: str = "", reference_path: str = None,
              emotion_reference_path: str = None, default_emotion: str = None,
              language: str = None, description: str = "",
              params: dict = None, index: int = 0) -> dict:
    return {
        "id": uuid.uuid4().hex[:12],
        "name": (name or "Voice").strip()[:80],
        "color": color or SWATCHES[index % len(SWATCHES)],
        "model_type": model_type if model_type in ENGINES else "index_tts2",
        "reference_path": reference_path,
        "emotion_reference_path": emotion_reference_path,
        "default_emotion": default_emotion,
        "language": language,
        "description": (description or "").strip()[:400],
        "params": dict(params or {}),
        "seed": new_seed(),
        "created_at": time.time(),
        "updated_at": time.time(),
        # Filled by a preview render so the UI can replay an audition
        # without regenerating it.
        "sample_path": None,
    }


def sanitize_entry(entry: dict) -> dict:
    """Normalise one entry and report what is actually usable.

    `reference_missing` matters: the library points at uploaded files rather
    than copying them, so a file deleted from the workspace must surface as
    a broken voice instead of failing later inside a render.
    """
    out = dict(entry or {})
    out.setdefault("id", uuid.uuid4().hex[:12])
    out["name"] = (out.get("name") or "Voice").strip()[:80] or "Voice"
    if out.get("model_type") not in ENGINES:
        out["model_type"] = "index_tts2"
    out["params"] = dict(out.get("params") or {})
    for key in ("reference_path", "emotion_reference_path", "sample_path",
                "default_emotion", "language"):
        out.setdefault(key, None)
    out.setdefault("description", "")
    out.setdefault("color", SWATCHES[0])
    try:
        seed = int(out.get("seed"))
    except (TypeError, ValueError):
        seed = 0
    # Heals voices saved before seeds existed: without this they would keep
    # drifting for as long as the library lives.
    out["seed"] = seed if 0 < seed <= _SEED_MAX else new_seed()

    caps = ENGINES[out["model_type"]]
    ref = out.get("reference_path")
    out["reference_missing"] = bool(ref) and not os.path.isfile(ref)
    # Usable = the engine has what it needs. IndexTTS2 without a clip cannot
    # speak at all, so that is not a warning but a blocker.
    out["ready"] = not (caps["needs_reference"] and (not ref or out["reference_missing"]))
    out["emotion_support"] = caps["emotion"]
    out["supports_cloning"] = caps["clone"]
    return out


def load_library(out_dir: str) -> list[dict]:
    path = library_path(out_dir)
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    entries = [e for e in (data.get("voices") if isinstance(data, dict) else data) or []
               if isinstance(e, dict)]
    voices = [sanitize_entry(e) for e in entries]
    # Write the healed seeds straight back. sanitize_entry mints one for a
    # voice saved before seeds existed, and a mint that is never persisted is
    # worse than none: the voice gets a NEW identity on every request, which
    # is exactly the drift this is meant to stop.
    if any(before.get("seed") != after["seed"]
           for before, after in zip(entries, voices)):
        try:
            save_library(out_dir, voices)
        except OSError as e:
            print(f"[voices] Could not persist healed seeds: {e}")
    return voices


def save_library(out_dir: str, voices: list[dict]) -> None:
    os.makedirs(out_dir, exist_ok=True)
    # Strip the derived fields — they are recomputed on load, and persisting
    # them would let a stale "ready: true" outlive the file it described.
    persisted = []
    for voice in voices:
        clean = {k: v for k, v in voice.items()
                 if k not in ("ready", "reference_missing", "emotion_support",
                              "supports_cloning")}
        persisted.append(clean)
    with _lock:
        _write_atomic(library_path(out_dir),
                      {"version": LIBRARY_VERSION, "voices": persisted})


def add_voice(out_dir: str, **kwargs) -> dict:
    voices = load_library(out_dir)
    entry = new_entry(index=len(voices), **kwargs)
    voices.append(entry)
    save_library(out_dir, voices)
    return sanitize_entry(entry)


def update_voice(out_dir: str, voice_id: str, patch: dict) -> dict | None:
    voices = load_library(out_dir)
    found = None
    for voice in voices:
        if voice.get("id") != voice_id:
            continue
        for key, value in (patch or {}).items():
            # id and created_at are identity, not content.
            if key in ("id", "created_at"):
                continue
            voice[key] = value
        voice["updated_at"] = time.time()
        found = voice
        break
    if found is None:
        return None
    save_library(out_dir, voices)
    return sanitize_entry(found)


def delete_voice(out_dir: str, voice_id: str) -> bool:
    voices = load_library(out_dir)
    remaining = [v for v in voices if v.get("id") != voice_id]
    if len(remaining) == len(voices):
        return False
    save_library(out_dir, remaining)
    return True


def get_voice(out_dir: str, voice_id: str) -> dict | None:
    return next((v for v in load_library(out_dir) if v.get("id") == voice_id), None)


def to_audiobook_profile(voice: dict, *, index: int = 0) -> dict:
    """Shape a library voice as an audiobook VoiceProfile dict.

    The two are deliberately separate: importing copies the configuration so
    later edits inside a book cannot rewrite the shared library entry, and a
    book can hold voices that were never meant to be reused.
    """
    return {
        "id": uuid.uuid4().hex[:12],
        "name": voice.get("name") or "Voice",
        "color": voice.get("color") or SWATCHES[index % len(SWATCHES)],
        "model_type": voice.get("model_type") or "index_tts2",
        "voice_ref_path": voice.get("reference_path"),
        "emotion_ref_path": voice.get("emotion_reference_path"),
        "default_emotion": voice.get("default_emotion"),
        # Without this the imported copy would get a per-run seed again and
        # sound like someone other than the voice that was auditioned.
        "seed": voice.get("seed"),
        "params": dict(voice.get("params") or {}),
    }


if __name__ == "__main__":
    import shutil
    import tempfile

    d = tempfile.mkdtemp(prefix="voicelib-selfcheck-")
    try:
        assert load_library(d) == [], "missing file reads as empty, not an error"

        # -- an engine that needs a clip is not ready without one
        v1 = add_voice(d, name="Narrator", model_type="index_tts2")
        assert v1["ready"] is False, v1
        assert v1["supports_cloning"] is True and v1["emotion_support"] == "native"

        # -- an engine that designs a voice is ready immediately
        v2 = add_voice(d, name="Designed", model_type="qwen3_tts_voicedesign",
                       params={"voice_description": "older man, gravelly"})
        assert v2["ready"] is True, v2
        assert v2["supports_cloning"] is False

        # -- the seed is the voice identity: every voice has one, it survives
        #    edits, and a library written before seeds existed gets one on load
        assert 0 < v1["seed"] <= _SEED_MAX, v1["seed"]
        assert 0 < v2["seed"] <= _SEED_MAX, v2["seed"]
        kept = update_voice(d, v2["id"], {"description": "renamed"})
        assert kept["seed"] == v2["seed"], "editing a voice must not reroll it"
        legacy = sanitize_entry({"id": "old", "name": "Legacy",
                                 "model_type": "qwen3_tts_voicedesign"})
        assert isinstance(legacy["seed"], int) and legacy["seed"] > 0

        # -- a minted seed must be PERSISTED, not re-minted per read. An
        #    unsaved heal gives the voice a new identity on every request,
        #    which is worse than having no seed at all.
        legacy_dir = os.path.join(d, "legacy")   # own library: the writes below
        os.makedirs(legacy_dir, exist_ok=True)   # would clobber the one above
        save_library(legacy_dir, [{"id": "seedless", "name": "Seedless",
                                   "model_type": "kugelaudio_0_open"}])
        healed = load_library(legacy_dir)[0]
        assert healed["seed"] > 0
        assert load_library(legacy_dir)[0]["seed"] == healed["seed"],             "a re-minted seed on every read is the drift this is meant to stop"
        raw = json.load(open(library_path(legacy_dir), encoding="utf-8"))
        assert raw["voices"][0]["seed"] == healed["seed"], "the heal never reached disk"
        for bad in (0, -5, None, "", "abc"):
            assert sanitize_entry({"seed": bad})["seed"] > 0, bad
        assert to_audiobook_profile(v2)["seed"] == v2["seed"],             "the book copy must speak with the voice that was auditioned"
        assert v2["color"] != v1["color"], "colours should differ by default"

        # -- unknown engine falls back rather than persisting nonsense
        v3 = add_voice(d, name="Bogus", model_type="does_not_exist")
        assert v3["model_type"] == "index_tts2"

        # -- a real reference file makes it ready; deleting it breaks it
        clip = os.path.join(d, "ref.wav")
        with open(clip, "wb") as f:
            f.write(b"RIFF....WAVE")
        updated = update_voice(d, v1["id"], {"reference_path": clip})
        assert updated["ready"] is True and updated["reference_missing"] is False
        os.remove(clip)
        reloaded = get_voice(d, v1["id"])
        assert reloaded["reference_missing"] is True and reloaded["ready"] is False, reloaded

        # -- derived fields must not be persisted (a stale "ready" would lie)
        with open(library_path(d), "r", encoding="utf-8") as f:
            raw = json.load(f)
        assert all("ready" not in v for v in raw["voices"]), raw
        assert raw["version"] == LIBRARY_VERSION

        # -- id and created_at survive a patch that tries to change them
        before = get_voice(d, v2["id"])
        patched = update_voice(d, v2["id"], {"id": "hacked", "created_at": 0,
                                            "name": "Renamed"})
        assert patched["id"] == v2["id"] and patched["created_at"] == before["created_at"]
        assert patched["name"] == "Renamed"
        assert update_voice(d, "nope", {"name": "x"}) is None

        # -- importing into a book copies, so edits there cannot leak back
        profile = to_audiobook_profile(get_voice(d, v2["id"]))
        assert profile["id"] != v2["id"], "imported profile needs its own id"
        assert profile["model_type"] == "qwen3_tts_voicedesign"
        assert profile["params"] is not get_voice(d, v2["id"])["params"]

        assert len(load_library(d)) == 3
        assert delete_voice(d, v3["id"]) is True
        assert delete_voice(d, v3["id"]) is False
        assert len(load_library(d)) == 2

        # -- a corrupt file degrades to empty instead of crashing the app
        with open(library_path(d), "w", encoding="utf-8") as f:
            f.write("{ not json")
        assert load_library(d) == []

        print("voice_library self-check: OK")
    finally:
        shutil.rmtree(d, ignore_errors=True)
