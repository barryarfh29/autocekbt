FROM python:3.11-slim

RUN useradd -m appuser
WORKDIR /app

ENV PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Siapkan direktori data (kalau mode files/volume dipakai)
RUN mkdir -p /data && chown -R appuser:appuser /data
USER appuser

CMD ["python", "-u", "userbot.py"]
