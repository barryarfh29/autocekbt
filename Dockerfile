FROM python:3.11-slim

# (opsional) user non-root
RUN useradd -m appuser
WORKDIR /app

# env untuk python & pip
ENV PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# install deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# copy source code
COPY . .
RUN chmod +x start.sh

USER appuser
CMD ["./start.sh"]
