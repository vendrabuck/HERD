FROM python:3.12-slim

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY services/common/ /app/common/
COPY services/reservations/pyproject.toml .
COPY services/reservations/app/ app/

RUN uv pip install --system -e /app/common && uv pip install --system -e .

RUN useradd --no-create-home --shell /bin/false appuser
USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
