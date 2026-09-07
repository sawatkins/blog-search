FROM python:3.14.7-slim-trixie@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6

RUN python -m pip install --no-cache-dir uv==0.12.10

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

COPY pyproject.toml uv.lock README.md /app/

RUN uv sync --frozen --no-dev --no-install-project

COPY web /app/web
COPY scraper /app/scraper
COPY db /app/db

RUN groupadd --gid 10001 blogsearch \
    && useradd --uid 10001 --gid blogsearch --no-create-home blogsearch \
    && mkdir -p /app/data/crawler \
    && chown blogsearch:blogsearch /app/data/crawler

USER blogsearch

WORKDIR /app/web

EXPOSE 8000

CMD ["/app/.venv/bin/uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
