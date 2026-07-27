"""Render planning: Chapter + rendered run audio → ffmpeg argv + timeline.

PLAN §3.4.  Everything here is a **pure function**.  No ``subprocess``, no file
IO, no existence checks — ``plan_chapter_mix()`` returns the argument list and
``build_*`` return the sidecar file *contents*; the caller writes them and runs
ffmpeg under the normal job lifecycle.  That is what makes the mix graph
testable without an audio engine.

The graph, in order:

  1. **Speech** runs, sequential, with a short gap between runs and a longer
     pause between paragraphs.  Each becomes ``adelay``-ed at its absolute
     start time, so the "mix" is really a set of offsets — no concat, no
     silence padding to get wrong.
  2. **Standalone SFX** blocks, sequential, in the same timeline.
  3. **Attached SFX / ambience**, parallel over its paragraph's span at
     reduced gain, looped if the asset is shorter than the span.
  4. **Background music**, parallel, with **auto-ducking under speech**: an
     explicit piecewise-linear gain envelope compiled into a ``volume``
     expression.  Calibration (all overridable in ``MixOptions``): music down
     to 35 % during speech, ducking starts 0.15 s *before* speech, recovers
     over 0.4 s afterwards, 0.4 s fade-in / 0.6 s fade-out at the segment
     edges.  An envelope rather than ``sidechaincompress`` because the
     pre-duck is a look-ahead — a sidechain compressor cannot duck before the
     speech it reacts to.
  5. ``amix`` (``normalize=0`` — amix's default normalisation would drop every
     source's level by 1/N), then a compressor, then **EBU R128 ``loudnorm``**,
     then a safety limiter.

Export targets: MP3 / WAV / FLAC / M4A, plus **M4B with chapter markers** via
``build_book_plan()``, which returns the concat list and the FFMETADATA text.

Self-check: ``python -m services.audiobook.mix`` from ``app/``.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Iterable, Optional

from services.audiobook.model import BLOCK_SFX, Chapter, Project

# One internal working format for the whole graph.  Mixing sources with
# different rates/layouts is the classic cause of "half the mix is silent".
_WORK_RATE = 48000
_WORK_FORMAT = f"aformat=sample_fmts=fltp:sample_rates={_WORK_RATE}:channel_layouts=stereo"

EXPORT_FORMATS = ("mp3", "wav", "flac", "m4a", "m4b")


class MixPlanError(ValueError):
    """The chapter cannot be mixed (nothing to mix, missing audio)."""


@dataclass
class MixOptions:
    """Every calibration knob of the mix, in one place.

    The defaults are the reference tool's measured values (PLAN §3.4) plus the
    loudness target audiobook platforms ask for.  They are starting points —
    a real room and a real narrator need tuning these, which is exactly why
    they are data and not literals in the graph builder.
    """

    # -- timing --------------------------------------------------------
    run_gap: float = 0.12            # between runs inside one paragraph
    paragraph_pause: float = 0.55    # between paragraphs
    sfx_gap: float = 0.25            # around a standalone SFX block
    lead_in: float = 0.30            # silence before the first word
    tail: float = 0.80               # silence after the last word
    # -- levels --------------------------------------------------------
    speech_gain: float = 1.0
    ambience_gain: float = 0.45      # multiplier on the asset's own volume
    music_gain: float = 1.0          # multiplier on the asset's own volume
    sfx_gain: float = 1.0
    # -- ducking -------------------------------------------------------
    duck_ratio: float = 0.35         # music level during speech
    duck_pre: float = 0.15           # start ducking this long BEFORE speech
    duck_recovery: float = 0.40      # ramp back up over this long
    music_fade_in: float = 0.40
    music_fade_out: float = 0.60
    speech_merge_gap: float = 0.35   # gaps shorter than this are not un-ducked
    duck_ambience: bool = True       # ambience ducks too, but gently
    ambience_duck_ratio: float = 0.8
    # -- master chain --------------------------------------------------
    compressor: str = "acompressor=threshold=-18dB:ratio=3:attack=20:release=250:makeup=2"
    loudnorm_i: float = -19.0        # LUFS; ACX wants -18..-23
    loudnorm_tp: float = -3.0        # dBTP
    loudnorm_lra: float = 9.0
    limiter_ceiling: float = 0.97
    # -- output --------------------------------------------------------
    fmt: str = "wav"                 # chapter renders stay lossless by default
    bitrate: str = "192k"
    sample_rate: int = _WORK_RATE

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "MixOptions":
        """Build from ``project.render_settings``, ignoring unknown keys."""
        options = cls()
        for key, value in (data or {}).items():
            if not hasattr(options, key) or value is None:
                continue
            current = getattr(options, key)
            try:
                if isinstance(current, bool):
                    setattr(options, key, bool(value))
                elif isinstance(current, float):
                    setattr(options, key, float(value))
                elif isinstance(current, int):
                    setattr(options, key, int(value))
                else:
                    setattr(options, key, value)
            except (TypeError, ValueError):
                continue
        return options


@dataclass
class TimelineEntry:
    """One sounding element, positioned on the chapter's absolute timeline.

    The speech entries are also the karaoke map: ``run_id`` plus ``start``
    tells the UI which run to highlight at any playback position (word-level
    timings come from faster-whisper on top of this).
    """

    kind: str                 # "speech" | "sfx" | "ambience" | "music"
    start: float
    end: float
    path: str
    gain: float = 1.0
    block_id: Optional[str] = None
    run_id: Optional[str] = None
    asset_id: Optional[str] = None
    loop: bool = False
    source_duration: float = 0.0

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "start": round(self.start, 4),
            "end": round(self.end, 4),
            "duration": round(self.duration, 4),
            "path": self.path,
            "gain": round(self.gain, 4),
            "block_id": self.block_id,
            "run_id": self.run_id,
            "asset_id": self.asset_id,
            "loop": self.loop,
        }


@dataclass
class MixPlan:
    """Everything the render worker needs, and nothing it has to figure out."""

    output_path: str
    duration: float
    inputs: list[str] = field(default_factory=list)
    filter_complex: str = ""
    args: list[str] = field(default_factory=list)
    timeline: list[TimelineEntry] = field(default_factory=list)
    speech_intervals: list[tuple[float, float]] = field(default_factory=list)
    duck_envelopes: dict[str, list[tuple[float, float]]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "output_path": self.output_path,
            "duration": round(self.duration, 4),
            "inputs": list(self.inputs),
            "filter_complex": self.filter_complex,
            "args": list(self.args),
            "timeline": [one.to_dict() for one in self.timeline],
            "speech_intervals": [
                [round(a, 4), round(b, 4)] for a, b in self.speech_intervals
            ],
            "warnings": list(self.warnings),
        }


# ── Timeline ───────────────────────────────────────────────────────────────


def merge_intervals(
    intervals: Iterable[tuple[float, float]], gap: float = 0.0,
) -> list[tuple[float, float]]:
    """Merge overlapping / near-adjacent intervals.

    ``gap`` is the tolerance: speech separated by less than this stays one
    interval, so the music does not pump up and down between two sentences.
    """
    ordered = sorted((a, b) for a, b in intervals if b > a)
    merged: list[list[float]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1] + gap:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(a, b) for a, b in merged]


def build_timeline(
    project: Project,
    chapter: Chapter,
    rendered: dict,
    options: Optional[MixOptions] = None,
) -> tuple[list[TimelineEntry], list[str], float]:
    """Lay the chapter out in time.

    ``rendered`` maps ``run_id`` → ``{"path": str, "duration": float}`` — the
    audio the TTS pass produced.  A run with no entry is skipped and reported;
    that is what a partially-rendered chapter looks like and it must not crash
    the preview.

    Returns ``(entries, warnings, total_duration)``.
    """
    options = options or MixOptions()
    entries: list[TimelineEntry] = []
    warnings: list[str] = []
    cursor = max(0.0, options.lead_in)

    for block in chapter.blocks:
        if block.type == BLOCK_SFX:
            asset = project.sfx_asset(block.sfx_id)
            if asset is None or not asset.audio_path:
                warnings.append(f"SFX block {block.id}: no rendered audio, skipped.")
                continue
            duration = max(0.05, float(asset.duration or 0.0))
            cursor += options.sfx_gap
            entries.append(TimelineEntry(
                kind="sfx", start=cursor, end=cursor + duration,
                path=asset.audio_path,
                gain=max(0.0, asset.volume * options.sfx_gain),
                block_id=block.id, asset_id=asset.id,
                source_duration=duration,
            ))
            cursor += duration + options.sfx_gap
            continue

        block_start = cursor
        spoke = False
        for run in block.runs:
            if not (run.text or "").strip():
                continue
            info = rendered.get(run.id)
            path = (info or {}).get("path")
            duration = float((info or {}).get("duration") or 0.0)
            if not path or duration <= 0:
                warnings.append(f"Run {run.id}: not rendered yet, skipped.")
                continue
            entries.append(TimelineEntry(
                kind="speech", start=cursor, end=cursor + duration,
                path=path, gain=options.speech_gain,
                block_id=block.id, run_id=run.id, source_duration=duration,
            ))
            cursor += duration + options.run_gap
            spoke = True
        if not spoke:
            continue
        # Undo the trailing inter-run gap, then apply the paragraph pause.
        cursor -= options.run_gap
        block_end = cursor
        cursor += options.paragraph_pause

        attached = block.attached_sfx or {}
        asset = project.sfx_asset(attached.get("sfx_id") or attached.get("sfxId"))
        if asset and asset.audio_path:
            volume = attached.get("volume")
            base = float(volume) if volume is not None else float(asset.volume)
            loop = bool(attached.get("loop", asset.loop))
            span = max(0.05, block_end - block_start)
            source = max(0.05, float(asset.duration or span))
            if not loop and source < span:
                span = source
            entries.append(TimelineEntry(
                kind="ambience", start=block_start, end=block_start + span,
                path=asset.audio_path,
                gain=max(0.0, base * options.ambience_gain),
                block_id=block.id, asset_id=asset.id, loop=loop,
                source_duration=source,
            ))

    # -- music segments ------------------------------------------------
    speech_and_sfx = [one for one in entries if one.kind in ("speech", "sfx")]
    total = (
        max((one.end for one in speech_and_sfx), default=0.0) + max(0.0, options.tail)
    )

    overrides: list[TimelineEntry] = []
    for block in chapter.blocks:
        attached = block.attached_music or {}
        music_id = attached.get("music_id") or attached.get("musicId")
        asset = project.music_asset(music_id)
        if asset is None or not asset.audio_path:
            continue
        block_entries = [one for one in entries if one.block_id == block.id and one.kind == "speech"]
        if not block_entries:
            continue
        volume = attached.get("volume")
        overrides.append(TimelineEntry(
            kind="music",
            start=min(one.start for one in block_entries),
            end=max(one.end for one in block_entries),
            path=asset.audio_path,
            gain=max(0.0, (float(volume) if volume is not None else asset.volume)
                     * options.music_gain),
            block_id=block.id, asset_id=asset.id,
            loop=bool(attached.get("loop", asset.loop)),
            source_duration=float(asset.duration or 0.0),
        ))
    overrides.sort(key=lambda one: one.start)

    base_music = project.music_asset(chapter.music_id)
    if base_music and base_music.audio_path and total > 0:
        # The chapter's music fills every span the per-block overrides leave.
        cursor_music = 0.0
        gaps: list[tuple[float, float]] = []
        for one in overrides:
            if one.start > cursor_music:
                gaps.append((cursor_music, one.start))
            cursor_music = max(cursor_music, one.end)
        if cursor_music < total:
            gaps.append((cursor_music, total))
        for start, end in gaps:
            if end - start < 0.5:      # too short to fade in and out again
                continue
            entries.append(TimelineEntry(
                kind="music", start=start, end=end,
                path=base_music.audio_path,
                gain=max(0.0, base_music.volume * options.music_gain),
                asset_id=base_music.id, loop=bool(base_music.loop),
                source_duration=float(base_music.duration or 0.0),
            ))
    entries.extend(overrides)
    entries.sort(key=lambda one: (one.start, one.kind))
    return entries, warnings, total


# ── Ducking ────────────────────────────────────────────────────────────────


def build_duck_envelope(
    segment_start: float,
    segment_end: float,
    full_gain: float,
    speech_intervals: list[tuple[float, float]],
    options: Optional[MixOptions] = None,
    *,
    duck_ratio: Optional[float] = None,
) -> list[tuple[float, float]]:
    """Piecewise-linear ``(time, gain)`` keyframes for one music segment.

    Shape, in order: fade in from silence; then for each speech interval hold
    full gain until ``duck_pre`` seconds *before* the speech, ramp down so the
    ducked level is reached exactly when the voice starts, hold it for the
    speech, ramp back to full over ``duck_recovery``; finally fade out to
    silence.  Times are absolute chapter seconds, so the resulting ``volume``
    expression can be applied after ``adelay``.

    Keyframes are strictly non-decreasing in time: a later keyframe that would
    move backwards (speech intervals closer together than the recovery time)
    replaces the previous gain instead of inserting an out-of-order point.
    That is what stops the envelope from jumping back to full volume between
    two quick sentences.
    """
    options = options or MixOptions()
    ratio = options.duck_ratio if duck_ratio is None else duck_ratio
    span = max(0.0, segment_end - segment_start)
    if span <= 0 or full_gain <= 0:
        return []
    duck_gain = max(0.0, full_gain * ratio)
    fade_in = min(options.music_fade_in, span / 4.0)
    fade_out = min(options.music_fade_out, span / 4.0)
    body_start = segment_start + fade_in
    body_end = max(body_start, segment_end - fade_out)

    keyframes: list[tuple[float, float]] = []

    def push(time: float, gain: float) -> None:
        """Add a keyframe; a non-advancing time overwrites the last gain."""
        time = min(max(time, segment_start), segment_end)
        if keyframes and time <= keyframes[-1][0]:
            # Same instant or backwards: the newer value wins.
            keyframes[-1] = (keyframes[-1][0], gain)
            return
        keyframes.append((time, gain))

    def hold(time: float, gain: float) -> None:
        """Add a keyframe only if it advances — never rewrites the last gain."""
        time = min(max(time, segment_start), segment_end)
        if not keyframes or time > keyframes[-1][0]:
            keyframes.append((time, gain))

    push(segment_start, 0.0)
    push(body_start, full_gain)

    for start, end in speech_intervals:
        clipped_start = max(start, segment_start)
        clipped_end = min(end, segment_end)
        if clipped_end <= clipped_start:
            continue
        # Stay at full until the pre-duck point, then ramp down so the ducked
        # level is reached exactly at the first word.
        hold(max(body_start, clipped_start - options.duck_pre), full_gain)
        push(min(body_end, clipped_start), duck_gain)
        push(min(body_end, clipped_end), duck_gain)
        recovery_at = clipped_end + options.duck_recovery
        if recovery_at < body_end:
            push(recovery_at, full_gain)
        # else: the segment's fade-out starts before the music could recover,
        # so it stays ducked into the fade instead of swelling for a moment.

    hold(body_end, keyframes[-1][1] if keyframes else full_gain)
    push(segment_end, 0.0)
    return keyframes


def envelope_to_volume_expr(keyframes: list[tuple[float, float]]) -> str:
    """Compile keyframes into an ffmpeg ``volume`` expression.

    Flat sum of gated linear segments rather than nested ``if()``:
    ``(gte(t,t0)*lt(t,t1))*(g0+slope*(t-t0)) + …``.  Nesting hundreds of
    ``if()`` calls (one per speech run in a chapter) is what breaks ffmpeg's
    expression parser; a flat sum has no depth at all.
    """
    terms: list[str] = []
    for (t0, g0), (t1, g1) in zip(keyframes, keyframes[1:]):
        if t1 <= t0:
            continue
        slope = (g1 - g0) / (t1 - t0)
        if abs(slope) < 1e-9:
            body = f"{g0:.6f}"
        else:
            body = f"({g0:.6f}+{slope:.6f}*(t-{t0:.5f}))"
        terms.append(f"(gte(t,{t0:.5f})*lt(t,{t1:.5f}))*{body}")
    return "+".join(terms) if terms else "0"


def evaluate_envelope(keyframes: list[tuple[float, float]], time: float) -> float:
    """Reference evaluation of the envelope — what ffmpeg's expression does.

    Used by the self-check to prove the compiled expression and the keyframes
    describe the same curve.
    """
    if not keyframes:
        return 0.0
    if time < keyframes[0][0]:
        return 0.0
    for (t0, g0), (t1, g1) in zip(keyframes, keyframes[1:]):
        if t0 <= time < t1:
            return g0 + (g1 - g0) * (time - t0) / (t1 - t0)
    return 0.0 if time >= keyframes[-1][0] else keyframes[-1][1]


# ── ffmpeg graph ───────────────────────────────────────────────────────────


def _delay_ms(seconds: float) -> int:
    return max(0, int(round(seconds * 1000.0)))


def _codec_args(options: MixOptions, output_path: str) -> list[str]:
    fmt = (options.fmt or os.path.splitext(output_path)[1].lstrip(".")).lower()
    if fmt not in EXPORT_FORMATS:
        raise MixPlanError(
            f"Unsupported export format '{fmt}'. Supported: {', '.join(EXPORT_FORMATS)}"
        )
    common = ["-ac", "2", "-ar", str(int(options.sample_rate))]
    if fmt == "wav":
        return common + ["-c:a", "pcm_s16le"]
    if fmt == "flac":
        return common + ["-c:a", "flac"]
    if fmt == "mp3":
        return common + ["-c:a", "libmp3lame", "-b:a", options.bitrate]
    # m4a / m4b are the same container; the extension is what players read.
    return common + [
        "-c:a", "aac", "-b:a", options.bitrate,
        "-movflags", "+faststart", "-f", "mp4",
    ]


def _master_chain(options: MixOptions) -> str:
    return (
        f"{options.compressor},"
        f"loudnorm=I={options.loudnorm_i}:TP={options.loudnorm_tp}:LRA={options.loudnorm_lra},"
        f"alimiter=limit={options.limiter_ceiling}"
    )


def plan_chapter_mix(
    project: Project,
    chapter: Chapter,
    rendered: dict,
    output_path: str,
    options: Optional[MixOptions] = None,
) -> MixPlan:
    """Build the full ffmpeg invocation for one chapter.

    ``rendered``: ``{run_id: {"path": ..., "duration": ...}}``.
    ``output_path``: where ffmpeg should write; its extension is used when
    ``options.fmt`` is not set explicitly.
    """
    options = options or MixOptions.from_dict(project.render_settings)
    entries, warnings, total = build_timeline(project, chapter, rendered, options)
    sounding = [one for one in entries if one.duration > 0]
    if not sounding:
        raise MixPlanError(
            "Nothing to mix: no rendered run audio for this chapter."
        )

    speech_intervals = merge_intervals(
        [(one.start, one.end) for one in sounding if one.kind == "speech"],
        gap=options.speech_merge_gap,
    )

    # Distinct input files, in first-use order.
    inputs: list[str] = []
    input_index: dict[str, int] = {}
    for entry in sounding:
        if entry.path not in input_index:
            input_index[entry.path] = len(inputs)
            inputs.append(entry.path)

    chains: list[str] = []
    labels: list[str] = []
    envelopes: dict[str, list[tuple[float, float]]] = {}

    for number, entry in enumerate(sounding):
        label = f"a{number}"
        steps = [_WORK_FORMAT]
        if entry.loop and entry.source_duration > 0:
            # aloop buffers `size` samples in memory; the asset is a short
            # ambience/music bed, so one period is the right buffer.
            # ponytail: whole-period in RAM. If someone loops a 10-minute bed,
            # switch to `-stream_loop -1` on that input instead.
            samples = max(1, int(math.ceil(entry.source_duration * _WORK_RATE)))
            steps.append(f"aloop=loop=-1:size={samples}")
        if entry.kind in ("ambience", "music") or entry.loop:
            steps.append(f"atrim=duration={entry.duration:.5f}")
            steps.append("asetpts=N/SR/TB")
        if entry.start > 0:
            steps.append(f"adelay={_delay_ms(entry.start)}:all=1")
        if entry.kind == "music":
            keyframes = build_duck_envelope(
                entry.start, entry.end, entry.gain, speech_intervals, options,
            )
            envelopes[f"{entry.asset_id}@{entry.start:.3f}"] = keyframes
            expression = envelope_to_volume_expr(keyframes)
            # Quoted so the commas inside the expression are literal to
            # ffmpeg's filtergraph parser.
            steps.append(f"volume=volume='{expression}':eval=frame")
        elif entry.kind == "ambience" and options.duck_ambience and speech_intervals:
            keyframes = build_duck_envelope(
                entry.start, entry.end, entry.gain, speech_intervals, options,
                duck_ratio=options.ambience_duck_ratio,
            )
            envelopes[f"{entry.asset_id}@{entry.start:.3f}"] = keyframes
            steps.append(
                f"volume=volume='{envelope_to_volume_expr(keyframes)}':eval=frame"
            )
        elif abs(entry.gain - 1.0) > 1e-6:
            steps.append(f"volume={entry.gain:.5f}")
        chains.append(f"[{input_index[entry.path]}:a]" + ",".join(steps) + f"[{label}]")
        labels.append(label)

    mix = (
        "".join(f"[{one}]" for one in labels)
        + f"amix=inputs={len(labels)}:normalize=0:dropout_transition=0:duration=longest,"
        + f"apad=whole_dur={total:.5f},"
        + _master_chain(options)
        + "[out]"
    )
    filter_complex = ";".join(chains + [mix])

    args = ["ffmpeg", "-y", "-nostdin", "-hide_banner"]
    for path in inputs:
        args += ["-i", path]
    args += ["-filter_complex", filter_complex, "-map", "[out]"]
    args += _codec_args(options, output_path)
    args.append(output_path)

    return MixPlan(
        output_path=output_path,
        duration=total,
        inputs=inputs,
        filter_complex=filter_complex,
        args=args,
        timeline=entries,
        speech_intervals=speech_intervals,
        duck_envelopes=envelopes,
        warnings=warnings,
    )


# ── Whole-book export (M4B with chapter markers) ────────────────────────────


def build_chapter_metadata(
    chapters: list[tuple[str, float]],
    *,
    title: str = "",
    author: str = "",
    album: str = "",
) -> str:
    """FFMETADATA1 text with one ``[CHAPTER]`` per chapter.

    ``chapters`` is ``[(title, duration_seconds), …]`` in playback order;
    start/end are accumulated so the caller never has to compute timestamps.
    Milliseconds timebase — what every M4B player expects.
    """
    lines = [";FFMETADATA1"]
    if title:
        lines.append(f"title={_escape_metadata(title)}")
    if author:
        lines.append(f"artist={_escape_metadata(author)}")
    lines.append(f"album={_escape_metadata(album or title)}")
    lines.append("genre=Audiobook")
    cursor_ms = 0
    for index, (chapter_title, duration) in enumerate(chapters, start=1):
        length_ms = max(1, int(round(max(0.0, duration) * 1000)))
        lines += [
            "[CHAPTER]",
            "TIMEBASE=1/1000",
            f"START={cursor_ms}",
            f"END={cursor_ms + length_ms}",
            f"title={_escape_metadata(chapter_title or f'Chapter {index}')}",
        ]
        cursor_ms += length_ms
    return "\n".join(lines) + "\n"


def _escape_metadata(value: str) -> str:
    """Escape the four characters FFMETADATA treats as special."""
    out = []
    for char in value:
        if char in ("=", ";", "#", "\\"):
            out.append("\\" + char)
        elif char == "\n":
            out.append(" ")
        else:
            out.append(char)
    return "".join(out)


def build_concat_list(paths: list[str]) -> str:
    """ffmpeg concat-demuxer list text.  Single quotes are escaped its way."""
    lines = []
    for path in paths:
        escaped = path.replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    return "\n".join(lines) + "\n"


@dataclass
class BookPlan:
    """Whole-book export: two sidecar files to write, one command to run."""

    output_path: str
    args: list[str]
    concat_list: str
    metadata: str
    chapter_starts: list[float]
    duration: float


def build_book_plan(
    chapter_files: list[tuple[str, str, float]],
    output_path: str,
    concat_list_path: str,
    metadata_path: str,
    options: Optional[MixOptions] = None,
    *,
    title: str = "",
    author: str = "",
) -> BookPlan:
    """Concatenate rendered chapters into one file with chapter markers.

    ``chapter_files``: ``[(audio_path, chapter_title, duration_seconds), …]``.
    The caller writes ``concat_list`` to ``concat_list_path`` and ``metadata``
    to ``metadata_path``, then runs ``args``.

    ``-map_metadata 1`` pulls the chapter markers from the metadata input;
    ``-map_chapters 1`` is what actually attaches them to an M4B.
    """
    if not chapter_files:
        raise MixPlanError("No rendered chapters to export.")
    options = options or MixOptions()
    fmt = (options.fmt or os.path.splitext(output_path)[1].lstrip(".")).lower()
    if fmt == "wav":
        # A concatenated 20-hour WAV is 13 GB and no player wants chapters in
        # it; say so instead of producing it by accident.
        raise MixPlanError(
            "Whole-book export to WAV is not offered (no chapter markers, "
            "huge file). Use m4b, mp3 or flac."
        )
    export_options = MixOptions.from_dict({**options.__dict__, "fmt": fmt})

    starts: list[float] = []
    cursor = 0.0
    for _, _, duration in chapter_files:
        starts.append(cursor)
        cursor += max(0.0, duration)

    args = [
        "ffmpeg", "-y", "-nostdin", "-hide_banner",
        "-f", "concat", "-safe", "0", "-i", concat_list_path,
        "-i", metadata_path,
        "-map_metadata", "1", "-map_chapters", "1",
        "-map", "0:a",
    ]
    args += _codec_args(export_options, output_path)
    args.append(output_path)

    return BookPlan(
        output_path=output_path,
        args=args,
        concat_list=build_concat_list([one[0] for one in chapter_files]),
        metadata=build_chapter_metadata(
            [(one[1], one[2]) for one in chapter_files], title=title, author=author,
        ),
        chapter_starts=starts,
        duration=cursor,
    )


if __name__ == "__main__":
    # Self-check: timeline layout, ducking envelope shape, expression compile,
    # graph structure, chapter metadata.  `python -m services.audiobook.mix`.
    from services.audiobook.model import (
        Block, MusicAsset, Run, SfxAsset, VoiceProfile,
    )

    options = MixOptions(lead_in=0.0, tail=0.0, run_gap=0.1, paragraph_pause=0.5,
                         sfx_gap=0.25)

    # 1. merge_intervals: overlap, adjacency inside the tolerance, and a real gap.
    assert merge_intervals([(0, 1), (0.5, 2)]) == [(0.0, 2.0)]
    assert merge_intervals([(0, 1), (1.2, 2)], gap=0.35) == [(0.0, 2.0)]
    assert merge_intervals([(0, 1), (2, 3)], gap=0.35) == [(0.0, 1.0), (2.0, 3.0)]
    assert merge_intervals([(1, 1)]) == []

    # ── a small book -----------------------------------------------------
    narrator = VoiceProfile(id="v1", name="N", model_type="index_tts2",
                            voice_ref_path="/refs/n.wav")
    rain = SfxAsset(id="s-rain", label="rain", duration=4.0, audio_path="/a/rain.wav",
                    playback_mode="parallel", loop=True, volume=0.5)
    door = SfxAsset(id="s-door", label="door", duration=1.5, audio_path="/a/door.wav",
                    playback_mode="sequential", volume=0.9)
    theme = MusicAsset(id="m1", title="Theme", audio_path="/a/theme.wav",
                       duration=30.0, volume=0.4, loop=True)
    project = Project(
        id="p", title="Book", voice_profiles=[narrator], default_profile_id="v1",
        sfx=[rain, door], music=[theme],
    )
    chapter = Chapter(
        id="c1", title="One", music_id="m1",
        blocks=[
            Block(id="b1", runs=[
                Run(id="r1", text="Erster Satz.", profile_id="v1"),
                Run(id="r2", text="Zweiter Satz.", profile_id="v1"),
            ], attached_sfx={"sfx_id": "s-rain", "volume": 0.5, "loop": True}),
            Block(id="b2", type=BLOCK_SFX, sfx_id="s-door"),
            Block(id="b3", runs=[Run(id="r3", text="Dritter Satz.", profile_id="v1")]),
        ],
    )
    rendered = {
        "r1": {"path": "/a/r1.wav", "duration": 2.0},
        "r2": {"path": "/a/r2.wav", "duration": 3.0},
        "r3": {"path": "/a/r3.wav", "duration": 1.0},
    }

    # 2. Timeline: speech sequential with gaps, SFX sequential, ambience parallel.
    entries, warnings, total = build_timeline(project, chapter, rendered, options)
    assert not warnings, warnings
    speech = [one for one in entries if one.kind == "speech"]
    assert [one.run_id for one in speech] == ["r1", "r2", "r3"]
    assert speech[0].start == 0.0 and speech[0].end == 2.0
    assert abs(speech[1].start - 2.1) < 1e-9, speech[1].start   # + run_gap
    assert abs(speech[1].end - 5.1) < 1e-9
    # b1 ends at 5.1, + paragraph_pause 0.5, + sfx_gap 0.25 → door at 5.85
    sfx_entries = [one for one in entries if one.kind == "sfx"]
    assert len(sfx_entries) == 1
    assert abs(sfx_entries[0].start - 5.85) < 1e-9, sfx_entries[0].start
    assert abs(sfx_entries[0].end - 7.35) < 1e-9
    # r3 follows the door's trailing gap: 7.35 + 0.25 = 7.6
    assert abs(speech[2].start - 7.6) < 1e-9, speech[2].start
    assert abs(total - 8.6) < 1e-9, total

    ambience = [one for one in entries if one.kind == "ambience"]
    assert len(ambience) == 1
    assert ambience[0].start == 0.0 and abs(ambience[0].end - 5.1) < 1e-9, ambience[0]
    assert abs(ambience[0].gain - 0.5 * options.ambience_gain) < 1e-9
    assert ambience[0].loop is True

    music = [one for one in entries if one.kind == "music"]
    assert len(music) == 1 and music[0].start == 0.0
    assert abs(music[0].end - total) < 1e-9
    assert abs(music[0].gain - 0.4) < 1e-9

    # 3. Non-looping ambience shorter than its paragraph is not stretched, and
    #    the block's own loop/volume win over the asset's defaults.
    chapter.blocks[0].attached_sfx = {"sfx_id": "s-rain", "volume": 0.5, "loop": False}
    rain.duration = 1.0
    short_amb = [
        one for one in build_timeline(project, chapter, rendered, options)[0]
        if one.kind == "ambience"
    ][0]
    assert abs(short_amb.duration - 1.0) < 1e-9, short_amb.duration
    assert short_amb.loop is False
    rain.duration = 4.0
    chapter.blocks[0].attached_sfx = {"sfx_id": "s-rain", "volume": 0.5, "loop": True}

    # 4. A missing render is reported, not fatal.
    partial_entries, partial_warnings, _ = build_timeline(
        project, chapter, {"r1": rendered["r1"]}, options,
    )
    assert len(partial_warnings) == 2, partial_warnings
    assert len([one for one in partial_entries if one.kind == "speech"]) == 1

    # ── ducking ---------------------------------------------------------
    speech_intervals = merge_intervals(
        [(one.start, one.end) for one in speech], gap=options.speech_merge_gap,
    )
    # r1+r2 merge (0.1s gap), r3 is separate (2.5s gap after the door).
    assert len(speech_intervals) == 2, speech_intervals
    assert abs(speech_intervals[0][0]) < 1e-9 and abs(speech_intervals[0][1] - 5.1) < 1e-9
    assert abs(speech_intervals[1][0] - 7.6) < 1e-9 and abs(speech_intervals[1][1] - 8.6) < 1e-9

    keyframes = build_duck_envelope(0.0, total, 0.4, speech_intervals, options)
    # 5. Monotonic non-decreasing time, starts and ends at silence.
    times = [t for t, _ in keyframes]
    assert times == sorted(times), times
    assert keyframes[0] == (0.0, 0.0)
    assert keyframes[-1] == (total, 0.0)
    # 6. Ducked to 35 % while speaking, full in a long enough pause.
    duck_gain = 0.4 * options.duck_ratio
    assert abs(evaluate_envelope(keyframes, 3.0) - duck_gain) < 1e-6, \
        evaluate_envelope(keyframes, 3.0)
    assert abs(evaluate_envelope(keyframes, 8.0) - duck_gain) < 1e-6
    # 7. Recovery after the first speech block, before the pre-duck of r3.
    recovered = evaluate_envelope(keyframes, 6.4)
    assert abs(recovered - 0.4) < 1e-6, recovered
    # 8. Pre-duck: full until 7.45 (= 7.6 - 0.15), ramping in between, and
    #    fully ducked by the time r3's first word starts at 7.6.
    assert abs(evaluate_envelope(keyframes, 7.44) - 0.4) < 1e-6
    mid_ramp = evaluate_envelope(keyframes, 7.525)
    assert duck_gain < mid_ramp < 0.4, mid_ramp
    assert abs(evaluate_envelope(keyframes, 7.6) - duck_gain) < 1e-6
    # 9. Fade in from silence over music_fade_in.
    assert evaluate_envelope(keyframes, 0.0) == 0.0
    assert 0.0 < evaluate_envelope(keyframes, 0.2) < 0.4
    # 10. Never above the segment's full gain, never negative.
    step = total / 400.0
    samples = [evaluate_envelope(keyframes, i * step) for i in range(401)]
    assert max(samples) <= 0.4 + 1e-9, max(samples)
    assert min(samples) >= 0.0

    # 11. Two sentences closer together than the recovery time must not bounce
    #     back to full volume in between.
    tight = build_duck_envelope(0.0, 10.0, 1.0, [(1.0, 3.0), (3.2, 5.0)], options)
    assert evaluate_envelope(tight, 3.1) <= options.duck_ratio + 1e-6, \
        evaluate_envelope(tight, 3.1)
    assert [t for t, _ in tight] == sorted(t for t, _ in tight)

    # 12. Degenerate inputs return nothing rather than a broken envelope.
    assert build_duck_envelope(5.0, 5.0, 1.0, [], options) == []
    assert build_duck_envelope(0.0, 5.0, 0.0, [], options) == []

    # 13. The compiled expression matches the reference evaluation, and has no
    #     unescaped character that would break ffmpeg's parser.
    expression = envelope_to_volume_expr(keyframes)
    assert "if(" not in expression and ":" not in expression
    assert "'" not in expression
    for index in range(0, 401, 7):
        time = index * step
        # Python mirror of the flat gated sum ffmpeg evaluates.
        value = 0.0
        for (t0, g0), (t1, g1) in zip(keyframes, keyframes[1:]):
            if t1 > t0 and t0 <= time < t1:
                value += g0 + (g1 - g0) * (time - t0) / (t1 - t0)
        assert abs(value - evaluate_envelope(keyframes, time)) < 1e-6, time
    assert envelope_to_volume_expr([]) == "0"

    # ── full graph ------------------------------------------------------
    plan = plan_chapter_mix(
        project, chapter, rendered, "/out/c1.wav", options,
    )
    # 14. One -i per distinct file, in first-use order.
    assert plan.inputs == [
        "/a/rain.wav", "/a/theme.wav", "/a/r1.wav", "/a/r2.wav",
        "/a/door.wav", "/a/r3.wav",
    ], plan.inputs
    assert plan.args[0] == "ffmpeg" and plan.args[-1] == "/out/c1.wav"
    assert plan.args.count("-i") == len(plan.inputs)
    # 15. Every element is delayed to its start, amix does not normalise, and
    #     the master chain ends in loudnorm + limiter.
    assert "adelay=2100:all=1" in plan.filter_complex, plan.filter_complex
    assert f"amix=inputs={len(plan.timeline)}:normalize=0" in plan.filter_complex
    assert "loudnorm=I=-19.0:TP=-3.0:LRA=9.0" in plan.filter_complex
    assert "alimiter=limit=0.97" in plan.filter_complex
    assert "acompressor=" in plan.filter_complex
    assert plan.filter_complex.count("volume=volume='") == 2, "music + ambience ducked"
    assert "aloop=loop=-1:size=192000" in plan.filter_complex   # 4s @ 48k
    assert "-c:a pcm_s16le" in " ".join(plan.args)
    assert plan.filter_complex.endswith("[out]")
    # 16. Labels are unique and all of them reach amix.
    labels = [f"a{i}" for i in range(len(plan.timeline))]
    for label in labels:
        assert f"[{label}]" in plan.filter_complex, label
    # 17. The timeline doubles as the karaoke map.
    karaoke = [
        (one.run_id, round(one.start, 4))
        for one in plan.timeline if one.kind == "speech"
    ]
    assert karaoke == [("r1", 0.0), ("r2", 2.1), ("r3", 7.6)], karaoke

    # 18. Formats.
    for fmt, expected in (
        ("mp3", "libmp3lame"), ("flac", "flac"), ("m4b", "aac"), ("m4a", "aac"),
    ):
        args = plan_chapter_mix(
            project, chapter, rendered, f"/out/c1.{fmt}",
            MixOptions.from_dict({**options.__dict__, "fmt": fmt}),
        ).args
        assert expected in " ".join(args), (fmt, args)
    try:
        plan_chapter_mix(project, chapter, rendered, "/out/c1.ogg",
                         MixOptions.from_dict({**options.__dict__, "fmt": "ogg"}))
    except MixPlanError as exc:
        assert "Unsupported export format" in str(exc)
    else:
        raise AssertionError("expected MixPlanError for ogg")

    # 19. Nothing rendered → explicit error, not an empty ffmpeg command.
    speech_only = Chapter(
        id="c2", title="Two", music_id="m1",
        blocks=[Block(id="b9", runs=[Run(id="r9", text="Nie gerendert.", profile_id="v1")])],
    )
    try:
        plan_chapter_mix(project, speech_only, {}, "/out/x.wav", options)
    except MixPlanError as exc:
        assert "Nothing to mix" in str(exc)
    else:
        raise AssertionError("expected MixPlanError for an unrendered chapter")
    # A chapter of nothing but standalone SFX is still mixable.
    sfx_only = Chapter(id="c3", title="Three",
                       blocks=[Block(id="b10", type=BLOCK_SFX, sfx_id="s-door")])
    assert plan_chapter_mix(project, sfx_only, {}, "/out/y.wav", options).duration > 0

    # 20. Per-block music override splits the chapter bed around it.
    alt = MusicAsset(id="m2", title="Alt", audio_path="/a/alt.wav", duration=20.0,
                     volume=0.3)
    project.music.append(alt)
    chapter.blocks[2].attached_music = {"music_id": "m2", "volume": 0.3}
    music_entries = [
        one for one in build_timeline(project, chapter, rendered, options)[0]
        if one.kind == "music"
    ]
    assert len(music_entries) == 2, [one.to_dict() for one in music_entries]
    base, override = music_entries[0], music_entries[1]
    assert base.asset_id == "m1" and abs(base.end - 7.6) < 1e-9, base.to_dict()
    assert override.asset_id == "m2" and abs(override.start - 7.6) < 1e-9
    chapter.blocks[2].attached_music = None

    # ── whole-book export -----------------------------------------------
    book = build_book_plan(
        [("/out/c1.m4a", "Kapitel 1", 10.5), ("/out/c2.m4a", "Kapitel 2", 20.25)],
        "/out/book.m4b", "/tmp/list.txt", "/tmp/meta.txt",
        MixOptions.from_dict({"fmt": "m4b"}),
        title="Mein Buch", author="Autor",
    )
    # 21. Chapter timestamps accumulate; metadata carries them in ms.
    assert book.chapter_starts == [0.0, 10.5], book.chapter_starts
    assert abs(book.duration - 30.75) < 1e-9
    assert "START=0" in book.metadata and "END=10500" in book.metadata
    assert "START=10500" in book.metadata and "END=30750" in book.metadata
    assert book.metadata.count("[CHAPTER]") == 2
    assert "title=Kapitel 1" in book.metadata
    assert book.metadata.startswith(";FFMETADATA1")
    assert "-map_chapters" in book.args and "aac" in " ".join(book.args)
    # 22. Concat list quoting.
    assert build_concat_list(["/a/b c.wav"]) == "file '/a/b c.wav'\n"
    assert "'\\''" in build_concat_list(["/a/it's.wav"])
    # 23. Metadata escaping: '=' and ';' must not end the field.
    escaped = build_chapter_metadata([("A=B;C", 1.0)])
    assert "title=A\\=B\\;C" in escaped, escaped
    # 24. Untitled chapters get a fallback title.
    assert "title=Chapter 2" in build_chapter_metadata([("x", 1.0), ("", 1.0)])
    # 25. WAV book export is refused with a reason.
    try:
        build_book_plan([("/out/c1.wav", "K1", 1.0)], "/out/b.wav",
                        "/tmp/l.txt", "/tmp/m.txt", MixOptions.from_dict({"fmt": "wav"}))
    except MixPlanError as exc:
        assert "WAV" in str(exc)
    else:
        raise AssertionError("expected MixPlanError for wav book export")
    try:
        build_book_plan([], "/out/b.m4b", "/tmp/l.txt", "/tmp/m.txt")
    except MixPlanError as exc:
        assert "No rendered chapters" in str(exc)
    else:
        raise AssertionError("expected MixPlanError for an empty book")

    # 26. MixOptions.from_dict ignores junk and coerces types.
    parsed = MixOptions.from_dict(
        {"duck_ratio": "0.2", "duck_ambience": 0, "nonsense": 1, "run_gap": None}
    )
    assert parsed.duck_ratio == 0.2 and parsed.duck_ambience is False
    assert parsed.run_gap == MixOptions().run_gap

    print("audiobook.mix self-check OK")
