# Piper TTS engine service.
#
# Installs the prebuilt piper-tts wheel rather than compiling libpiper/espeak-ng
# from source. Upstream's own Dockerfile builds from source, which produces the
# identical wheel but costs ~10min of saturated CPU per deploy — unacceptable on
# a host shared with production services.
FROM python:3.12-slim

ARG PIPER_VERSION=1.7.0

# [http] pulls in Flask, which serves /synthesize, /voices and /info.
RUN pip install --no-cache-dir "piper-tts[http]==${PIPER_VERSION}"

# Voices are NOT baked into the image: they are large, some carry restrictive
# licenses, and baking them would rebuild the image on every voice change.
# The entrypoint populates /data (a persistent volume) on first boot.
ENV PIPER_DATA_DIR=/data \
    PIPER_VOICES="en_US-lessac-medium" \
    PIPER_PORT=5000

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 5000

ENTRYPOINT ["/entrypoint.sh"]
