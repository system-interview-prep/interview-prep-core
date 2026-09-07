FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
RUN pip install --upgrade pip && pip install ".[embeddings]"

COPY src ./src
COPY tests ./tests

FROM base AS api

EXPOSE 5000
CMD ["uvicorn", "src.main:asgi_app", "--host", "0.0.0.0", "--port", "5000"]

FROM base AS worker

CMD ["celery", "-A", "src.workers.celery_app:celery_app", "worker", "--loglevel=info", "--concurrency=1"]

FROM base AS test

RUN pip install ".[dev]"
CMD ["pytest", "-q"]
