FROM python:3.10-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/

WORKDIR /app

COPY . /app

RUN uv sync --frozen

WORKDIR /app/web

EXPOSE 8000

CMD ["uv", "run", "server.py"]
