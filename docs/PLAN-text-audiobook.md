# Plan: Text mode (Chat + Storywriter) and AudioBook Creator

Status: proposal, awaiting sign-off. Written against the code as of commit `7126259`.

Two features that are independent except for two explicit hand-off points
(§4). Both are additive — nothing existing changes behaviour.

---

## 0. Principles

1. **Reuse the established patterns, don't invent parallel ones.** Every
   piece below names the existing precedent it copies. The Director
   pipeline, the `tools` mode and the audio sub-mode toggle already solve
   most of the structural problems.
2. **Server owns the work, UI renders state.** Anything a user can do must
   be reachable over `/api/v1` and therefore over MCP. No feature may live
   only in browser memory.
3. **Persist at phase boundaries.** Long-form generation runs for minutes
   to hours. A browser reload, a container restart or a crash must not lose
   a story or an audiobook project.
4. **NSFW behaves exactly like the rest of the app** — one master switch,
   same gating semantics (§2.5).

---

## 1. Groundwork (shared by both features)

### 1.1 Multi-turn chat + per-session streaming — the actual blocker

Two hard limitations in the current LLM layer make Chat impossible as-is:

- `POST /api/v1/llm/generate` (`app/launch.py:6433`) accepts only a single
  `prompt`. There is no `messages` array anywhere — every call is
  single-turn. The richer capabilities (`json_schema`, images, thinking,
  `stop`) exist in `llm_service.generate()` but are not exposed.
- `_stream_buffer` / `_stream_done` (`app/services/llm_service.py:46-49`)
  are **module-level globals — a single stream slot for the whole
  process**. `get_stream_status()` returns that one buffer. A chat running
  while a Director pipeline plans would have the two overwrite each other's
  output. Same for two browser tabs.

**Fix (must land before Chat or Storywriter):**

- Introduce a session-keyed stream registry in `llm_service`:
  `_streams: dict[str, {"text": str, "done": bool, "started_at": float}]`
  guarded by the existing lock, with a `stream_id` parameter on
  `generate_streaming()`. Keep the old globals as an alias for
  `stream_id="default"` so `PromptInput.tsx`, `DirectorChat.tsx` and
  `_capture_llm_pass` (`director_pipeline.py:2906`) keep working unchanged.
- Reap entries older than ~10 min so the dict can't grow unbounded.
- New endpoint `POST /api/v1/llm/chat`: takes `messages[]`, `model_id?`,
  `system_prompt?`, sampling params, `stream_id?`, `json_schema?`,
  `image_paths?`. Returns `{stream_id, text}` and streams into the
  registry. `GET /api/v1/llm/stream-status?stream_id=…` becomes the poller
  (default arg keeps the existing signature valid).
- Conversation state lives **server-side** (§2.2) so MCP clients and the UI
  see the same threads.

Polling stays the transport (800 ms, as `DirectorChat` already does).
SSE would be nicer but would mean a second transport for one feature —
not worth it now. Noted as a possible later upgrade.

### 1.2 Text-model registry for long-form writing

`MODEL_REGISTRY` (`llm_service.py:253-506`) is tuned for prompt
enhancement: small models, 60 s idle-unload, 65 k context default. Long-form
writing needs the opposite. Work:

- Add a `use_cases: ["chat", "story", "enhance"]` field to registry
  entries and filter the picker by the calling feature, so the Storywriter
  offers long-context writers and the prompt enhancer keeps offering fast
  small ones. `_PUBLIC_MODEL_ORDER` (`:521`) becomes per-use-case.
- Add strong long-form models (research task — GGUF availability, context
  length, VRAM fit for 24 GB, and how uncensored each one actually is).
  Candidates to evaluate, not a commitment: the existing Qwen3.6 27B
  Heretic (262 k context — already the best fit we have), plus dedicated
  creative-writing fine-tunes and a mid-size 12–14 B option for people
  with less VRAM.
- Raise the idle-unload timeout for story sessions (currently 60 s,
  `llm_service.py:44`) — reloading a 16 GB model between chapters is
  minutes of dead time. Make it a per-use-case value.
- Fix the known bug: registry entry `:352` declares `arch: "gemma4-26b"`
  which `LLM_ARCHITECTURES` (`:122-137`) doesn't know, so `_estimate_kv_gb`
  returns 0 and the 26 B model's VRAM hint is ~4 GB too low.

### 1.3 Make text a first-class output

`GET /api/v1/outputs` filters on `media_exts` (`app/launch.py:12781`) which
has no `.txt`/`.md`, and the type fallback (`:12842`) would label a text
file `"image"`. Add a `"text"` type end to end: `media_exts`, `ftype`
detection, `OutputFile.type` union (`ui/src/types/index.ts:154`), and a
text branch in `MediaFeedItem.tsx:412` rendering a titled excerpt card that
opens the reader (§2.4).

---

## 2. Feature A — Text mode

### 2.1 UI anchoring

`tools` mode is the exact precedent: a top-level mode with no generation
model and no Forge button. Files to touch (all verified):

| File | Change |
|---|---|
| `ui/src/types/index.ts:169` | `GenerationMode` += `'text'`; new `TextSubMode = 'chat' \| 'story'` |
| `ui/src/stores/useStore.ts:591` | `modeDefaultModel` += `text: ''` (exhaustive record) |
| `:610-628` | `getFamiliesForMode` → `[]` for `text` |
| `:1877-2025` | `setGenerationMode`: early-return branch like `tools` (`:1884-1908`) — no model/LoRA restore |
| `:3828` | `startGeneration` must not run for `text` (own submit path) |
| `Sidebar.tsx:49-65,157,221` | `isText` flag, panel switch, hide model+Forge bottom bar |
| `GenerationModeSelector.tsx:5-11` | new entry with lucide icon |
| `MainContent.tsx:621-655` | empty-state copy |

New `TextSubModeToggle` component copying `AudioSubModeToggle.tsx` (39
lines, the minimal pattern).

### 2.2 Chat (default sub-mode)

Deliberately plain: model picker (filtered to `use_cases` containing
`chat`), system-prompt field, message thread, streaming answer with the
existing `<think>`-tag handling from `PromptInput.tsx:43-51`, stop button,
token/context indicator.

- Threads persist server-side as JSON in the workspace
  (`_chat_{id}.json`), same atomic-write helper as pipelines
  (`_write_pipeline_json_unlocked`, `director_pipeline.py:216`).
- Endpoints: `GET/POST /api/v1/chat/threads`, `GET/DELETE
  /api/v1/chat/threads/{id}`, `POST /api/v1/chat/threads/{id}/messages`.
- Attach images if the loaded model has vision (registry `mmproj_file`)
  — plumbing already exists (`llm_service.py:1570-1582`).
- "Send to Storywriter" turns a chat into a story premise.

### 2.3 Storywriter — a story pipeline

Mirrors the Director pipeline, minus the render phases. Note
`pipeline_type: "short_film_story"` already exists
(`director_pipeline.py:2455`) and takes a story description — the
Storywriter is essentially that path producing prose instead of clips.

New `app/services/story_pipeline.py` reusing, not copying:

- `pid`-keyed in-memory dict + `_update_story()` wrapper with
  **cancellation as an absorbing terminal state** (the single most
  important pattern, `director_pipeline.py:2141-2153`) — otherwise a
  late-returning chapter overwrites the user's Stop.
- Persistence: `_story_{pid}.json` in the workspace output dir, atomic
  temp+`os.replace`, including a **`_params_snapshot`** of the original
  request (`:370`) so an interrupted story resumes faithfully.
- Worker thread `daemon=False` (`:2156`) so an overnight run survives a
  browser disconnect; `_wait_for_gpu` before planning because the LLM needs
  CUDA.
- Progress dict `{current, total, message, step, total_steps}` (`:2223`) —
  `PipelinePlaceholder` (`MainContent.tsx:264-331`) then renders story
  progress for free.
- Passes captured via `_capture_llm_pass` (`:2906`) so the reader can show
  what the model was asked, per pass.

**Passes:**

1. **Outline** — premise + target length → structured plan (title, logline,
   characters, setting, chapter list with beats). `json_schema`-constrained
   (`llm_service.py:1802`), grammar-enforced on local models.
2. **Chapter draft** — one call per chapter, carrying a rolling context:
   the outline, a running synopsis, character sheet, and the tail of the
   previous chapter. This is what makes *long* stories work — never the
   full text in context, which would blow up past chapter 3.
3. **Continuity pass** (optional, per chapter) — updates the running
   synopsis and character state from what was actually written, so drift
   gets corrected instead of compounding.

**Controls** (the user's spec): free-form premise, **minimum page-length
slider + numeric input** (pages → words → tokens, ~275 words/page, shown as
an estimate with a chapter count), genre/tone, POV and tense, chapter
count or "let the model decide", target audience, explicitness level
(§2.5), model + sampling.

**Prompts as editable Markdown guides** in
`app/services/llm_guides/story/` loaded via `guide_loader.py`, exactly as
the Director does — never string literals in code.

### 2.4 Story reader / editor

Own overlay panel at App root, precedent `DirectorDashboard`
(`ui/src/App.tsx:84`) — the established "large text-heavy read panel".
Not the media feed: a novel needs a reading layout, not a card.

- Reading view: chapter navigation, generous typography (`@tailwindcss/typography`
  is not currently a dependency — either add it or style manually),
  word/page counters, glass styling per house style.
- **Regenerate a part**: per chapter (and per scene where the outline has
  beats) with an optional instruction ("darker", "more dialogue"). Reuses
  the Director's repair semantics: `_claim_pipeline_operation`
  (`:82`) prevents concurrent mutation of the same story;
  `rerun_clip_*` (`:904`, `:1107`) is the shape to copy.
- **Extend the story**: append N chapters, continuing from the current
  synopsis — the same chapter pass with a shifted start index.
- **Reuse the prompt**: the `_params_snapshot` is already persisted, so
  "new story with these settings" and "show me the exact prompt used" are
  both reads, not new plumbing.
- Inline manual edits (it's the user's text), saved back to the story JSON;
  a manual edit invalidates the synopsis, which the continuity pass
  regenerates.
- Export `.md` / `.txt` / `.docx`; write into the workspace so the file
  shows up as a text output (§1.3), and offer **"Create audiobook"** (§4).

### 2.5 NSFW conformance

Important finding: **the LLM model list is not NSFW-gated today.**
`_PUBLIC_MODEL_ORDER` (`llm_service.py:521-527`) exposes only
abliterated/heretic/uncensored builds regardless of `nsfw_mode`; the master
switch affects *guide and prompt content* for the LLM, not model
visibility. Video models are gated via `nsfw_only` in the model JSON,
filtered client-side in `ModelSelector.tsx:49`.

So "conformant like the rest" means:

- Read the master flag the standard way (`servicesConfig.nsfw_mode`), which
  is force-disabled when an external provider is configured
  (`launch.py:5688`, `:5801-5809`) — an explicit story must never be sent
  to OpenAI/Anthropic, and that guard already exists.
- Explicitness control in the Storywriter is **only offered when
  `nsfw_mode` is on**; off = the control is hidden and the story guides use
  the tame variant. Same mechanism the Director uses:
  `app/services/director/nsfw_guidance.py` + `safety_scan.py`, wired
  through `planner_kwargs["nsfw"]` (`director_pipeline.py:3019`).
- Story guides get tame/explicit variants in
  `llm_guides/story/`, selected by that flag — not a prompt suffix bolted
  on at call time.
- `safety_scan.py` runs on story output as it does on Director output.

---

## 3. Feature B — AudioBook Creator

New audio sub-mode. Full inventory of the reference tool
(`C:\dev\privat\github\audient-scribe-studio`) was taken; everything below
maps a feature of it onto local models.

### 3.1 Data model — port the block format

The reference tool's block model is genuinely well designed and worth
copying (`src/lib/blocks.ts:13-63`): a paragraph is a list of **runs**,
each run carries text + voice-profile id + overrides. **No character
indices** — which is why voice assignments can't drift when the text is
edited. Port as-is:

```
Project → Chapters[] → Blocks[]
Block = Paragraph{ runs: Run[], attachedSfx?, attachedMusic? } | Sfx{ sfxId }
Run   = { id, text, profileId?, overrides? }
Overrides = { emotion?, stability?, style?, speed?, modelId? }
VoiceProfile = { id, name, color, model_type, voice_ref_path?, params }
SfxAsset   = { id, label, prompt, duration, audio_path, playback_mode, loop, volume }
MusicAsset = { id, title, source, prompt, audio_path, duration, volume, loop }
```

Persistence: `_audiobook_{pid}.json` per project in the workspace, same
atomic writer; generated audio as real files next to it (no base64 in
JSON). Content-hash per chapter (`computeContentHash` equivalent) to cache
rendered chapter audio and invalidate on edit.

### 3.2 Local replacements for ElevenLabs

| Reference tool used ElevenLabs for | Local replacement |
|---|---|
| TTS with voice settings | **IndexTTS2** (zero-shot cloning + emotion), **KugelAudio 7B**, **Chatterbox** (multilingual), **Qwen3 TTS** |
| Emotion tags `[sad]`, `[angry]` | **IndexTTS2 natively**: emotion auto-detected per paragraph or set with `[]`, plus emotion transfer from a second reference audio. Near-identical syntax → the run-level `styleTag` concept ports directly |
| Voice cloning from samples | IndexTTS2 / KugelAudio zero-shot from a reference clip; seed-vc for conversion |
| Voice design (unused there) | **Qwen3 TTS Voice Design** — describe a voice in natural language |
| Sound-effect generation | **MMAudio** (existing SFX sub-mode) |
| Music generation | **ACE-Step v1/v1.5** (existing music sub-mode) |
| Karaoke timestamps | faster-whisper (already a dependency) via `services/audio_analysis.py`, which does transcription and diarisation |
| Multi-speaker dialogue | IndexTTS2 and KugelAudio accept `Speaker 1:` / `Speaker 2:` line tags |

The ElevenLabs-specific parameters (`stability`, `similarity_boost`,
`style`, `use_speaker_boost`) map onto per-model params
(`temperature`, `top_p`, `guidance_scale`, emotion tag). Each voice profile
is bound to a `model_type`, and the parameter UI is driven by that model's
`model_defaults()` — the same mechanism Studio already uses, so we don't
hardcode a parameter set that only fits one engine.

Determinism matters (playback must match export): keep the reference tool's
trick of a stable seed derived from `runId|voiceId|emotion`
(`ProjectEditor.tsx:882`).

### 3.3 Editor UI

Split view — chapter/asset sidebar + block list — as in the reference tool.
Explicitly **not** a DAW timeline; that tool proved a Notion-style block
editor is enough, and it fits our sidebar layout.

- **Import**: `.txt`, `.md`, `.docx` (mammoth equivalent — server-side via
  python-docx to avoid a frontend dependency), **plus `.pdf` and `.epub`
  which the reference tool advertises but never implemented**. Language
  auto-detection on import.
- **Chapter splitting**: regex (Markdown headings, "Kapitel N"/"Chapter N")
  + manual split at cursor + **LLM auto-split** with a target-words slider
  and a reviewable proposal list (their `auto-split-chapter` function, run
  against our local LLM with `json_schema` instead of tool-calling).
- **Voice assignment**: select text → floating popover → pick a profile;
  runs split and merge with region expansion so partial words can't happen.
- **Emotion per run**: preset chips (sad, angry, whispering, excited,
  tender, …) + free text, rendered as a small removable badge, applied to
  the whole voice region.
- **SFX**: project library with usage counts, create/edit dialog (prompt,
  duration, parallel vs sequential, loop, volume, audition-before-save),
  drag onto a paragraph for ambience or insert as a standalone block.
- **Music**: generate (ACE-Step) or upload, chapter-level assignment plus
  per-block override, volume and loop.
- **Preview**: per paragraph, per selection, and whole chapter with
  **karaoke highlighting** from word timings.

### 3.4 Render, mix, export

The reference tool mixes entirely in the browser (WebAudio, ducking,
compressor). For MuseForge the server is the better home — it is the only
way an MCP agent can render an audiobook, it survives a closed tab, and it
handles a 20-hour book. ffmpeg is already in the image.

- `POST /api/v1/audiobook/{pid}/render` → job, per chapter or whole book,
  reusing the existing job lifecycle so progress and cancel come for free.
- Mix graph, ffmpeg `filter_complex`: sequential speech runs, parallel
  ambience at reduced gain, **auto-ducking of music/ambience under
  speech** (their calibration is a good starting point: music to 35 %,
  0.15 s pre-duck, 0.4 s recovery), then a compressor for consistent
  loudness. Add **EBU R128 loudness normalisation** (`loudnorm`), which
  they lack and audiobook platforms require.
- Export: MP3, plus **M4B with chapter markers** and WAV/FLAC — the
  reference tool only does per-chapter MP3 128 kbps with no whole-book
  export.
- Chapter audio cached by content hash; editing a block invalidates only
  affected chapters.

**Open decision — see question to the user:** whether live chapter playback
also renders server-side (simpler, one mix implementation, slight latency
before playback starts) or keeps a browser-side WebAudio path for instant
scrubbing (nicer UX, two implementations of the mix that must agree).

### 3.5 "AI Magic" — assisted casting

Their strongest feature and worth porting: one LLM pass over a chapter
proposes (a) new characters with a suggested voice, (b) a speaker per run,
(c) one emotion tag per run, (d) run splits where dialogue hides inside
narration, (e) SFX suggestions with prompts, split into ambience (loop, low
volume) and one-shot. All of it lands in a review dialog with per-item
checkboxes — never applied blindly.

Server-side validation of every suggestion against real block/run/profile
ids (they do this, `magic-analyze/index.ts:240-306`) is not optional: an
LLM inventing an id must not corrupt a project.

---

## 4. Hand-off points between the two features

1. **Story → AudioBook**: "Create audiobook" in the story reader creates an
   audiobook project, chapters pre-split from the story's chapter
   structure (no re-parsing needed — the structure is known), and jumps to
   the editor.
2. **AudioBook → Story**: in the import dialog, "Generate a story instead"
   hands the uploaded text (or a premise) to the Storywriter as a premise
   and switches mode.

Both are shallow: one endpoint each plus a mode switch. No shared state.

---

## 5. API and MCP

Everything gets `/api/v1` endpoints, so MCP coverage follows automatically
via the existing generic `api_request`. Dedicated MCP tools for the
workflows an agent would actually drive:

`chat` · `story_start` / `story_status` / `story_extend` / `story_regenerate_chapter` /
`story_export` · `audiobook_create` / `audiobook_import_text` /
`audiobook_assign_voice` / `audiobook_render` / `audiobook_status`

`McpPanel.tsx` tool table and `docs/API.md` stay in sync (same discipline
as the last round).

---

## 6. Deliberately not ported from the reference tool

Supabase, auth, multi-user, credits, Stripe, the admin area — MuseForge is
single-user and local. Also skipped: their dead `render-chapter` edge
function, the legacy `voice_segments` table and legacy voice columns, and
their unauthenticated/unmetered LLM endpoints.

---

## 7. Sequencing

| Phase | Content | Depends on |
|---|---|---|
| **0** | Real download progress (in flight) | — |
| **1** | §1.1 per-session streaming + `/llm/chat`; §1.3 text outputs | — |
| **2** | §2.1 text mode shell + §2.2 Chat | 1 |
| **3** | §1.2 model registry (research + entries) | — (parallel) |
| **4** | §2.3 story pipeline + §2.4 reader | 1, 3 |
| **5** | §3.1–3.3 audiobook data model, TTS bridge, editor | — (parallel to 2/4) |
| **6** | §3.4 render/mix/export | 5 |
| **7** | §3.5 AI Magic; §4 hand-offs; §5 MCP tools | 4, 6 |

Phases 2/4 and 5/6 touch disjoint files and can run in parallel. Each
phase ends with a green `npm run build` + `compileall`, a commit, and a
container restart only when the user says it's safe.
