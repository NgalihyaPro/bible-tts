# bible-tts

Self-hosted [Piper](https://github.com/OHF-Voice/piper1-gpl) text-to-speech backend for a Bible
mobile app. Deployed to a Coolify-managed VPS behind Cloudflare at `tts.b5internet.com`.

## Status

| Component | State |
|---|---|
| Piper engine service (this repo) | in progress |
| FastAPI API layer | not started |
| Bible text database | not started |
| Android client | not started |

## Architecture

Chapter audio is **pre-generated**, not synthesized on demand. Bible chapters are a closed set
(1,189 per translation × voice) with fixed text and a fixed voice, so the output is finite and
deterministic. Generating it once and serving static files removes the need for request-time
synthesis, generation locks, a `GENERATING` state machine, and the CPU contention that on-demand
synthesis would create on a shared host.

```
Bulk generation (offline, developer machine)
    Bible text  ->  Piper  ->  WAV  ->  ffmpeg  ->  AAC/M4A  ->  upload
                                                                    |
Runtime (VPS)                                                       v
    Android app  --HTTPS-->  Cloudflare  -->  FastAPI  -->  static audio
                                                  |
                                                  +-->  Piper engine (fallback,
                                                        arbitrary text only)
```

The Piper engine still runs on the VPS to serve `POST /api/v1/tts` for arbitrary text, but it is
**not** on the critical path for chapter playback.

### Why the engine is not publicly exposed

`piper.http_server` has no authentication and ships a browser UI. It listens on the internal Docker
network only (`expose`, never `ports`). The FastAPI service is the sole public entry point and owns
authentication and rate limiting.

### Why the prebuilt wheel

Upstream's Dockerfile compiles `libpiper` and espeak-ng from source, which yields the same artifact
as the published wheel but costs ~10 minutes of saturated CPU per deploy. This host also runs
production services, so we install `piper-tts` from PyPI instead and cap the container at 1 CPU.

## Deploy

Coolify → project `bible-tts` → environment `production` → Add Resource → Docker Compose, pointed at
this repo. Voices download into the `piper-voices` volume on first boot, so redeploys don't refetch
them.

Configure via environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `PIPER_VOICES` | `en_US-lessac-medium sw_CD-lanfrica-medium` | Space-separated; the first is the server default |
| `PIPER_PORT` | `5000` | Internal listen port |

## Voice licensing

Voice models are **not** committed to this repo, and not only because of their size. Piper voices
carry per-voice licenses that are independent of Piper's own GPL-3.0, and several are unusable in a
shipped product. Verified:

| Voice | Dataset / license | Shippable |
|---|---|---|
| `en_US-libritts-high` | OpenSLR 60, CC BY 4.0, trained from scratch | yes, with attribution |
| `en_US-libritts_r-medium` | CC BY 4.0 dataset, but fine-tuned from lessac | no |
| `en_US-lessac-*` | Blizzard 2013 Lessac — research only, non-commercial | no |
| `en_US-amy-medium` | fine-tuned from lessac | no |
| `en_US-ryan-high`, `en_US-hfc_*` | CC BY-NC-SA 4.0 | no |
| `sw_CD-lanfrica-medium` | dataset terms unstated; fine-tuned from lessac | no |

`lessac` and `lanfrica` are used for development only. Production English is
`en_US-libritts-high`; production Swahili requires a purpose-recorded voice, since no
permissively licensed Swahili Piper voice exists.

Always read the `MODEL_CARD` beside a voice — including its `Training` line, which is where the
fine-tune lineage hides — before shipping it.

Bible translation text is separately copyrighted. Public-domain translations (KJV, ASV, WEB) are
safe; NIV/ESV/NKJV/NLT require a publisher licence, as TTS narration creates an audio derivative.

## Licence

No licence declared for this repo's own files yet, which means default copyright — all rights
reserved. Add one if you want others to reuse it.

Piper itself is GPL-3.0 and is *installed* from PyPI at build time, not vendored here. Running it as
a network service does not trigger GPL distribution obligations; shipping it inside a mobile app
would.
