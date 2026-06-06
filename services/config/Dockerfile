FROM python:3.12-slim

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY services/config/pyproject.toml .
COPY services/config/app/ app/

RUN uv pip install --system --no-sources -e .

RUN mkdir -p /data/herd-config

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
