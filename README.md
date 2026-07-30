<p align="center">
  <img src="docs/logo.png" alt="MuseForge" width="420" />
</p>

<p align="center">
  <strong>Self-hosted AI media studio</strong> — video, images, audio, long-form text and audiobooks,<br />
  drivable from a browser, a script or an AI agent.
</p>

---

> **Heritage:** MuseForge is a fork of [Maestro](https://github.com/Blizaine/Maestro) by
> [@Blizaine](https://github.com/Blizaine), which itself builds on the
> [Wan2GP](https://github.com/deepbeepmeep/Wan2GP) generation pipeline. Full credits
> [below](#credits) — this README only covers what MuseForge does differently.

Deploy it anywhere with one `docker compose up`, then use it from the browser or
let an agent drive it over MCP. Video, image and audio generation, an
LLM-planned Director mode, a long-form Storywriter and an audiobook producer —
all in a single Docker image.

## Why a separate tool?

Maestro is a desktop-style app distributed through the Pinokio launcher and optimized
for the person sitting in front of it. MuseForge takes the same generation engine in a
different direction — **infrastructure instead of desktop app**:

- **Docker-first.** One `docker compose up` on any CUDA box — no launcher, no
  Python setup, no per-machine install scripts. Prebuilt images ship from GHCR via CI;
  all state (weights, LoRAs, outputs, settings) lives in named volumes.
- **Agent-first.** Everything the UI can do is a versioned REST API, and a native
  **MCP endpoint** (`/mcp`) makes MuseForge a first-class tool for AI agents: list
  models, submit jobs, poll, fetch outputs, enhance prompts. Optional bearer-token
  auth prepares for multi-user setups.
- **Its own workflow language and face.** Aurora Glass UI (frosted panels, animated
  dialogs, right-hand control dock), **Blueprints** for one-click reusable looks, and
  a **Forge** button instead of yet another "Generate".

If you want the original desktop experience with one-click Pinokio install, use
[Maestro](https://github.com/Blizaine/Maestro). If you want to run the engine as a
service and integrate it, you're in the right place.

## Quick start (Docker)

Requirements: [Docker](https://docs.docker.com/engine/install/) with the
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html),
an NVIDIA GPU (6 GB+ VRAM), and disk headroom for model weights (50–300 GB).

```bash
git clone https://github.com/fgilde/MuseForge.git
cd MuseForge
docker compose up -d
```

Open <http://localhost:7861>. The compose file builds locally by default; switch to
the prebuilt `ghcr.io/fgilde/museforge:latest` image by swapping two lines in
[docker-compose.yml](docker-compose.yml). Port 7861 is deliberate — a Maestro
instance on the same machine keeps 7860, so both can run side by side.

Build notes:

- Default image targets CUDA compute capabilities 8.0/8.6/8.9 (A100, RTX 30xx/40xx).
  Other cards: `docker build --build-arg CUDA_ARCHITECTURES="8.6;8.9;12.0" -t museforge .`
- Compiling the bundled SageAttention kernels needs ~8 GB RAM per job
  (`MAX_JOBS=2` default). On RAM-limited builders skip them — the app falls back to
  sdpa attention: `docker build --target runtime -t museforge:latest . && docker compose up -d --no-build`
- **The published GHCR image is the `runtime` target**, i.e. without
  SageAttention. Building both stages needs the CUDA devel image plus a second
  torch install in parallel, which does not fit a hosted runner's disk. The
  image is fully functional either way; run the *Docker image* workflow
  manually with "Also compile the SageAttention kernels" to publish a
  `:latest-sage` variant.
- The first generation on each model downloads its weights (the default video model
  is ~18 GB); only requested models are fetched.

Manual (non-Docker) install: Python 3.10 venv + torch 2.10/cu128 +
`app/requirements.txt`, clone the seed-vc component, build `ui/`, run
`python launch.py` — the [Dockerfile](Dockerfile) is the executable reference for
the exact steps.

## Using it

- **Studio** — direct control: pick a model (LTX, Wan, Hunyuan, Flux, Qwen, ACE-Step,
  TTS, …), prompt, LoRAs, advanced knobs, hit **Forge**.
- **Director** — describe a music video or short film; a local LLM plans shots,
  writes prompts per model, generates start frames and runs the full multi-clip
  pipeline.
- **Blueprints** — save any output's full recipe (model + LoRAs + settings) and
  re-apply it with one click, or share it as a file.
- **Settings → API & MCP** — endpoint URL, ready-made client configs, tool
  reference and token status.

## API & MCP

REST API at `/api/v1` (interactive docs: <http://localhost:7861/docs>), MCP endpoint
at <http://localhost:7861/mcp> (streamable HTTP):

```bash
claude mcp add --transport http museforge http://localhost:7861/mcp
```

Set `MUSEFORGE_API_TOKEN` (see docker-compose.yml) to require
`Authorization: Bearer <token>` on `/mcp`. Details: [docs/API.md](docs/API.md).

The API has no authentication beyond the optional MCP token and CORS is restricted
to localhost — control exposure via the compose port mapping
(`127.0.0.1:7861:7860` for loopback-only) and don't publish it to untrusted networks.

## Requirements

| | Minimum | Recommended |
|---|---|---|
| **GPU** | NVIDIA, 6 GB VRAM | RTX 3090 / 4090 / 5090, 24 GB+ |
| **RAM** | 16 GB | 32 GB+ |
| **Disk** | 150 GB free | 500 GB free |

AMD GPUs and macOS are not supported (CUDA-only pipeline). Performance auto-tune
profiles the GPU on first launch and picks offload/quantization settings; low-VRAM
cards work but generate slowly.

**Inpaint (SAM 3.1)** is experimental and not bundled in the Docker image — it needs
a separate Python 3.12 env at `app/services/sam/env` with
`app/services/sam/requirements.txt`. Everything else works without it.

## Updating / resetting

Docker: `docker compose pull && docker compose up -d` (or rebuild). Reset: remove the
named volumes you want to wipe (`docker volume ls | grep museforge`) — model weights
live in `ckpts`, leave it unless you want to re-download.

## License

MuseForge is released under the **WanGP Non-Commercial Evaluation License 1.1**,
inherited from upstream Wan2GP. See [LICENSE](LICENSE) and
[app/LICENSE.txt](app/LICENSE.txt). TL;DR: free for non-commercial use; your
generated *outputs* are yours (with attribution); commercial use of the software
itself needs a license from the WanGP licensor.

Third-party components keep their own licenses. The GPL-3.0
[seed-vc](https://github.com/Plachta/seed-vc) voice-conversion component is cloned
from its own repository at build time rather than vendored here.

## Credits

- [**Maestro**](https://github.com/Blizaine/Maestro) by [@Blizaine](https://github.com/Blizaine) — the direct upstream: Director mode, React UI foundation, LoRA tooling, auto-tune.
- [**Wan2GP / WanGP**](https://github.com/deepbeepmeep/Wan2GP) by [@deepbeepmeep](https://github.com/deepbeepmeep) — the entire generation pipeline.
- [**LTX-Video**](https://github.com/Lightricks/LTX-Video) (Lightricks), [**Wan 2.x**](https://github.com/Wan-Video/Wan2.1) (Alibaba), [**Flux**](https://github.com/black-forest-labs/flux) (Black Forest Labs), [**Qwen**](https://github.com/QwenLM/Qwen) (Alibaba), [**Gemma**](https://ai.google.dev/gemma) (Google) — models.
- [**SAM**](https://github.com/facebookresearch/sam2) (Meta), [**MMAudio**](https://github.com/hkchengrex/MMAudio), [**llama.cpp**](https://github.com/ggml-org/llama.cpp), [**CivitAI**](https://civitai.com) — segmentation, audio, local LLM inference, LoRA ecosystem.

## Issues

Bug reports and feature requests: this repository's GitHub issues.
