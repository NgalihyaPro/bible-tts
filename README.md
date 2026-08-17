# bible-tts

Self-hosted text-to-speech backend for a Bible mobile app. FastAPI in front of a
[Piper](https://github.com/OHF-Voice/piper1-gpl) engine, deployed to a Coolify-managed VPS behind
Cloudflare at `tts.b5internet.com`.

## Status

| Component | State |
|---|---|
| Piper engine | in this stack, internal only |
| FastAPI API | built, tested |
| Bible text | sample JSON; database phase pending |
| Pre-generated chapter audio | pending |
| Android client | not started |

## API

All endpoints except `/health` require an `X-API-Key` header.

| Endpoint | Purpose |
|---|---|
| `GET /health` | liveness; reports engine reachability, ffmpeg, cached file count |
| `GET /api/v1/voices` | configured language→voice map vs. what the engine has loaded |
| `POST /api/v1/tts` | arbitrary text → WAV. Capped at 600 chars |
| `GET /api/v1/bible/audio/{lang}/{book}/{chapter}` | chapter audio, with range support |
| `GET /api/v1/bible/audio/status/{lang}/{book}/{chapter}` | `READY` / `GENERATING` / `FAILED` |
| `GET /api/v1/bible/text/{lang}/{book}/{chapter}` | the verses behind a chapter |

Chapter endpoints accept `?translation=` and otherwise fall back to `DEFAULT_TRANSLATIONS`.

## Architecture

Chapter audio is **pre-generated**, not synthesized on demand. Bible chapters are a closed set
(1,189 per translation × voice) with fixed text and a fixed voice, so the output is finite and
deterministic. Generating it once and serving static files means playback is a file read.

```
Bulk generation (offline, developer machine)
    Bible text  ->  Piper  ->  WAV  ->  ffmpeg  ->  AAC/M4A  ->  upload
                                                                    |
Runtime (VPS)                                                       v
    Android app  --HTTPS-->  Cloudflare  -->  FastAPI  -->  cached audio
                                                  |
                                                  +-->  Piper engine (fallback only)
```

On a cache miss the API returns `202 GENERATING` immediately and renders in the background; the
client polls the status endpoint. It never blocks, because one average chapter takes minutes on the
deployed engine — far past Cloudflare's 100s proxy timeout.

### Measured performance

Synthesis speed, `en_US-lessac-medium`, warm process, measured 2026-08-17:

| Host | Speed vs realtime |
|---|---|
| Dev laptop, i7-1255U, 1 process | 16.8× |
| VPS, EPYC 2.0GHz shared vCPU, 1 CPU cap | 0.61× |
| VPS, 2 CPU cap | 1.26× |

Piper narrates at ~224 words/min at `length_scale: 1.0`, so a KJV-sized translation (783k words) is
~58 hours of audio: ~3.5 h of compute on the laptop, ~4 days on the VPS. Hence offline generation.

### Delivery format

AAC-LC ~48 kbps mono in fMP4 (`.m4a`), `+faststart`. Hardware-decoded on every Android version worth
supporting, and it seeks cleanly over HTTP range requests — the API answers `206 Partial Content`, so
dragging the scrubber fetches a byte range instead of re-downloading. Opus is ~2× smaller but its
seeking story over HTTP is messier. A full translation is ~1.3 GB as AAC against ~9.3 GB as WAV.

### Why the engine is not publicly exposed

`piper.http_server` has no authentication and ships a browser UI. It listens on the internal Docker
network only; this API is the sole public entry point and owns authentication and rate limiting.
Synthesis is additionally bounded by a semaphore that returns `429` rather than queueing, because the
engine is single-threaded Flask on a 1-CPU cap — concurrent requests serialize and would time out.

## Deploy

**One** Coolify resource in project `bible-tts`, environment `production`: this repo, Build Pack
Docker Compose, compose file `/docker-compose.yml`, domain `tts.b5internet.com`.

The stack brings up two containers. Compose creates the network, so the API reaches the engine at
`http://piper:5000` by service name — there is no external network to configure or go stale.

| Container | Public | Limits | Volume |
|---|---|---|---|
| `api` | yes, via the domain | 0.5 CPU / 512 MB | `bible-audio` → `/data/audio` |
| `piper` | no, `expose` only | 1.0 CPU / 1500 MB | `piper-voices` → `/data` |

`api` waits on `piper`'s healthcheck before starting, so its first `/health` is meaningful.

`API_KEYS` is the only variable that must be set; it has no default and the stack refuses to start
without it. Everything else in `.env.example` has a working default.

## Voice licensing

Voice models are **not** committed here, and not only because of their size. Piper voices carry
per-voice licenses independent of Piper's GPL-3.0, and several are unusable in a shipped product.
Verified:

| Voice | Dataset / license | Shippable |
|---|---|---|
| `en_US-libritts-high` | OpenSLR 60, CC BY 4.0, trained from scratch | yes, with attribution |
| `en_US-libritts_r-medium` | CC BY 4.0 dataset, but fine-tuned from lessac | no |
| `en_US-lessac-*` | Blizzard 2013 Lessac — research only, non-commercial | no |
| `en_US-amy-medium` | fine-tuned from lessac | no |
| `en_US-ryan-high`, `en_US-hfc_*` | CC BY-NC-SA 4.0 | no |
| `sw_CD-lanfrica-medium` | dataset terms unstated; fine-tuned from lessac | no |

`lessac` and `lanfrica` are development-only. Production English is `en_US-libritts-high`; production
Swahili needs a purpose-recorded voice, as no permissively licensed Swahili Piper voice exists.

Always read the `MODEL_CARD` beside a voice — including its `Training` line, where the fine-tune
lineage hides — before shipping it.

Bible translation text is separately copyrighted. Public-domain translations (KJV, ASV, WEB) are
safe; NIV/ESV/NKJV/NLT require a publisher licence, since narration creates an audio derivative.

## Licence

No licence declared for this repo's own files yet, which means default copyright — all rights
reserved. Add one if you want others to reuse it.

Piper itself is GPL-3.0 and runs as a separate service; it is not vendored or linked here. Running it
as a network service does not trigger GPL distribution obligations; shipping it inside a mobile app
would.
