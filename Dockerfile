# Bible TTS API.
FROM python:3.12-slim

# ffmpeg transcodes generated WAV into AAC/M4A for delivery.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY data/ ./data/

# Runs unprivileged. The audio volume is chowned in compose-managed storage.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data/audio \
    && chown -R appuser:appuser /data /app
USER appuser

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
