# Piper TTS engine, run as an internal HTTP service.
#
# Installs the prebuilt piper-tts wheel rather than compiling libpiper and
# espeak-ng from source as upstream's own Dockerfile does. Identical artifact
# (PyPI publishes it from that same project), but ~1 minute instead of ~10
# minutes of saturated CPU per deploy, which matters because this host also
# runs production services.
FROM python:3.12-slim

ARG PIPER_VERSION=1.7.0

# [http] pulls in Flask, which serves /synthesize, /voices and /info.
RUN pip install --no-cache-dir "piper-tts[http]==${PIPER_VERSION}"

# Voices are not baked into the image: ~63MB each, and several carry licenses
# that forbid redistribution. They live in /data, a named volume, so they
# download once and survive redeploys.
ENV PIPER_VOICES="en_US-lessac-medium sw_CD-lanfrica-medium" \
    PIPER_PORT=5000

EXPOSE 5000

# Fetch any voice missing from the volume, then serve. The first entry in
# PIPER_VOICES is the server default; the rest stay selectable per request via
# the "voice" field of POST /synthesize. exec replaces the shell so signals
# reach Python and the container stops cleanly.
CMD for v in $PIPER_VOICES; do \
      [ -f "/data/$v.onnx" ] && echo "[piper] voice present: $v" \
        || python3 -m piper.download_voices --data-dir /data "$v"; \
    done; \
    exec python3 -m piper.http_server \
      --host 0.0.0.0 \
      --port "$PIPER_PORT" \
      --data-dir /data \
      -m "${PIPER_VOICES%% *}"
