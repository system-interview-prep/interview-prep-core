FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DEFAULT_TIMEOUT=600 \
    PIP_RETRIES=10

WORKDIR /app
COPY pyproject.toml README.md ./
RUN pip install ".[embeddings,voice-realtime]"

COPY src ./src
COPY tests ./tests

FROM base AS api
EXPOSE 5000
CMD ["uvicorn", "src.main:asgi_app", "--host", "0.0.0.0", "--port", "5000"]

FROM base AS worker

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 libgomp1 libreoffice-writer \
    && rm -rf /var/lib/apt/lists/*
RUN pip install "paddlepaddle>=3.0,<4" \
    && pip install "paddleocr[doc-parser]>=3.0,<4" "pyyaml>=6.0.3"
RUN python -c "import paddle; assert not paddle.is_compiled_with_cuda(), 'CPU image unexpectedly contains GPU PaddlePaddle'; print('PaddlePaddle CPU import OK:', paddle.__version__)"

CMD ["celery", "-A", "src.workers.celery_app:celery_app", "worker", "--loglevel=info", "--concurrency=1"]

FROM nvidia/cuda:12.6.3-cudnn-runtime-ubuntu24.04 AS worker-gpu

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DEFAULT_TIMEOUT=600 \
    PIP_RETRIES=10

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.12 python3.12-dev python3-pip \
    libgl1 libglib2.0-0 libgomp1 libreoffice-writer \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
RUN python3.12 -m pip install --break-system-packages "." \
    && python3.12 -m pip install --break-system-packages \
      "paddlepaddle-gpu==3.3.0" -i https://www.paddlepaddle.org.cn/packages/stable/cu126/ \
    && python3.12 -m pip install --break-system-packages \
      "paddleocr[doc-parser]>=3.0,<4" "pyyaml>=6.0.3"

COPY src ./src
COPY tests ./tests
RUN python3.12 -c "import paddle; assert paddle.is_compiled_with_cuda(), 'GPU PaddlePaddle was not installed'; print('PaddlePaddle GPU import OK:', paddle.__version__)"

CMD ["python3.12", "-m", "celery", "-A", "src.workers.celery_app:celery_app", "worker", "--loglevel=info", "--concurrency=1"]

FROM base AS test
RUN pip install ".[dev,matching]"
CMD ["pytest", "-q"]
