FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd -m app && chown -R app /app
USER app
ENV PORT=8080
# One worker: analysis jobs live in memory. Threads handle concurrent requests.
CMD gunicorn app:app --workers 1 --threads 8 --timeout 120 --bind 0.0.0.0:${PORT}
