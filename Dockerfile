FROM python:3.11-slim

# Buat user non-root
RUN useradd -m appuser
WORKDIR /app

# Environment Python
ENV PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy semua file bot
COPY . .

# Siapkan direktori data (kalau pakai mode file)
RUN mkdir -p /data && chown -R appuser:appuser /data
USER appuser

# Jalankan bot
CMD ["python", "-u", "userbot.py"]
