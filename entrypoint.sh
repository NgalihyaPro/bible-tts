#!/bin/sh
# Ensure every configured voice exists in the data volume, then serve.
#
# PIPER_VOICES is a space-separated list. The first entry becomes the server
# default (-m); the rest are still selectable per-request via the "voice" field
# of POST /synthesize, because the server resolves voices out of --data-dir.
set -eu

DATA_DIR="${PIPER_DATA_DIR:-/data}"
VOICES="${PIPER_VOICES:-en_US-lessac-medium}"
PORT="${PIPER_PORT:-5000}"

mkdir -p "${DATA_DIR}"

for voice in ${VOICES}; do
  if [ -f "${DATA_DIR}/${voice}.onnx" ]; then
    echo "[entrypoint] voice present: ${voice}"
  else
    echo "[entrypoint] downloading voice: ${voice}"
    python3 -m piper.download_voices --data-dir "${DATA_DIR}" "${voice}"
  fi
done

# First voice in the list is the default.
DEFAULT_VOICE=$(echo "${VOICES}" | awk '{print $1}')
echo "[entrypoint] serving on 0.0.0.0:${PORT}, default voice ${DEFAULT_VOICE}"

exec python3 -m piper.http_server \
  --host 0.0.0.0 \
  --port "${PORT}" \
  --data-dir "${DATA_DIR}" \
  -m "${DEFAULT_VOICE}"
