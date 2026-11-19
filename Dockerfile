FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno
WORKDIR /srv
RUN pip install --no-cache-dir fastapi==0.165.0 "uvicorn[standard]==0.54.0" jinja2 python-multipart httpx markdown-it-py faster-whisper
ARG YTDLP_REFRESH=1
RUN pip install --no-cache-dir -U "yt-dlp[default]" bgutil-ytdlp-pot-provider
COPY app ./app
ENV DATA_DIR=/data HF_HOME=/data/hf PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
