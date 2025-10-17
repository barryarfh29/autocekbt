FROM python:3.11-slim

# Terima build-arg kalau panel masih menyuntik (biar builder tidak error)
ARG API_ID
ARG API_HASH
ARG SESSION_STRING
ARG STORAGE
ARG MONGO_URI
ARG MONGO_DB
ARG GIT_SHA
ARG BUILD_TS=now

WORKDIR /app
COPY . .

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DEFAULT_TIMEOUT=180 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Install dependencies SAAT CONTAINER RUN (bukan saat build)
CMD bash -lc "python -m pip install --upgrade pip && \
              pip install --no-cache-dir --prefer-binary -i https://pypi.org/simple -r requirements.txt && \
              python -u userbot.py"
