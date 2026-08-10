# Store packaging

Templates for installing MuseForge from a NAS/home-server app store.

Both are validated on every push by `scripts/check_packaging.py`, which checks
the image reference, the version against `VERSION`, the GPU wiring and the
cross-references Umbrel is strict about.

## Prerequisite for both: the image must be public

The templates pull `ghcr.io/fgilde/museforge:latest`. That package is
**private by default**, and an anonymous pull returns `403` — every install
from either store fails at the first step.

GitHub has no REST endpoint for this, so it is a one-time click:

> <https://github.com/users/fgilde/packages/container/museforge/settings>
> → *Danger Zone* → *Change visibility* → **Public**

Verify from a machine that is not logged in:

```bash
TOKEN=$(curl -s "https://ghcr.io/token?scope=repository:fgilde/museforge:pull" | jq -r .token)
curl -s -o /dev/null -w "%{http_code}\n" \
  -H "Authorization: Bearer $TOKEN" \
  https://ghcr.io/v2/fgilde/museforge/manifests/latest
# 200 = anyone can install. 403 = still private.
```

## Unraid

Unraid is the natural fit: x86, and NVIDIA GPUs work through the
*Nvidia-Driver* plugin. `unraid/museforge.xml` is a complete Community
Applications template.

**Try it before submitting.** In Unraid: *Docker* → *Add Container* →
*Template* → paste the raw URL of `packaging/unraid/museforge.xml`. Or add
this repository under *Community Applications* → *Settings* →
*Additional repositories* to see it as an app.

**Submitting to Community Applications:** CA indexes templates from GitHub
repositories. Announce the container in the Unraid forums' *Docker
Containers* board with a link to this repository; a moderator adds the repo
to the CA feed. The template must stay reachable at the `TemplateURL` it
declares, which is why that field points at `main`.

What the template already handles:

- `--runtime=nvidia` plus `NVIDIA_VISIBLE_DEVICES` and
  `NVIDIA_DRIVER_CAPABILITIES=all`. Compute capability alone is not enough
  for the video pipeline.
- Every volume the compose file uses, so nothing lands in the container's
  writable layer — model weights especially.
- Port 7861 by default, to avoid a clash with a Maestro instance.

## Umbrel

Umbrel is a poorer fit, and it is worth being blunt about why: MuseForge
needs an NVIDIA GPU with 6 GB of VRAM, the image is around 16 GB, and model
weights add 150 GB or more. Umbrel's own hardware (Umbrel Home, Raspberry
Pi) cannot run it at all, and the official app store expects apps that work
on it. A submission there would be declined on hardware grounds, not on
quality.

What does work is a **community app store**, which umbrelOS supports
natively: any git repository laid out like `packaging/umbrel/` can be added
from the Umbrel UI. Users on x86 hardware with an NVIDIA card can then
install MuseForge in one click.

To publish it:

1. Create a repository — for example `fgilde/umbrel-app-store`.
2. Copy the contents of `packaging/umbrel/` into its root, so the repository
   contains `umbrel-app-store.yml` and the `gilde-museforge/` folder.
3. In umbrelOS: *App Store* → *…* → *Community App Stores* → add the
   repository URL.

The app id must stay `gilde-museforge`: Umbrel requires
`<store-id>-<app-name>`, and `app_proxy`'s `APP_HOST` is derived from it
(`gilde-museforge_server_1`). The validator enforces both, because getting
either wrong produces an app that installs and then serves nothing.

## Keeping these current

After a release, bump `version` in `umbrel-app.yml` to match `VERSION` and
rewrite `releaseNotes`. CI fails on the first of those if it is forgotten;
the second is a judgement call no script can make.
