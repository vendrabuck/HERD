FROM python:3.12-slim

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml .
COPY app/ app/
COPY conftest.py .
COPY tests/ tests/

# Install with optional dev dependencies (needed for `make test`)
RUN uv pip install --system -e ".[dev]"

RUN useradd --no-create-home --shell /bin/false appuser
USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
