FROM python:3.11-slim

# --- terima build-args kalau panel masih menyuntik (kita abaikan saja)
ARG API_ID
ARG API_HASH
ARG SESSION_STRING
ARG STORAGE
ARG MONGO_URI
ARG MONGO_DB
ARG GIT_SHA
ARG BUILD_TS=now

# --- paket yang sering dibutuhkan saat pip install
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# salin requirements lebih dulu agar layer cache efektif
COPY requirements.txt .
# pakai prefer-binary + index resmi (kadang panel punya masalah CA)
RUN python -m pip install --upgrade pip \
 && pip install --no-cache-dir --prefer-binary -i https://pypi.org/simple -r requirements.txt

# baru salin source
COPY . .

# direktori data (untuk mode files + volume)
RUN mkdir -p /data

CMD ["python", "-u", "userbot.py"]
