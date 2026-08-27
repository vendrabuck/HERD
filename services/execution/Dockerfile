# Lock-faithful image build (issue #593): the workspace uv.lock is the single
# source of truth for third-party dependency versions, for host installs AND
# for every service image. `uv export --frozen` turns the lock into a plain
# requirements.txt for this service's package alone (no workspace members,
# no dev-only groups), which `uv pip install` then installs verbatim: no
# fresh dependency resolution happens at image build time. The two editable
# installs that follow use `--no-deps`, so they register the local packages
# without touching resolution again.
#
# Practical effect: a Dependabot PR that bumps a pyproject specifier without
# regenerating uv.lock now fails BOTH `uv lock --check` (CI's backend job)
# and this image build (`uv export --frozen` refuses to run against a stale
# lock), so drift between what CI tests and what ships in a container is
# structurally impossible. Regenerate with `uv lock` to fix either failure.
FROM python:3.12-slim

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Workspace lock and pyprojects live under /lock, kept out of /app so they
# never collide with the service's own pyproject.toml-driven package
# discovery below. This layer only invalidates when a dependency actually
# changes, not on every source edit.
WORKDIR /lock
COPY pyproject.toml uv.lock ./
COPY services/common/pyproject.toml services/common/pyproject.toml
COPY services/execution/pyproject.toml services/execution/pyproject.toml

RUN uv export --frozen --package herd-execution --no-hashes --no-emit-workspace \
    --no-default-groups --no-header -o requirements.txt \
    && uv pip install --system -r requirements.txt

WORKDIR /app

COPY services/common/ /app/common/
COPY services/execution/pyproject.toml .
COPY services/execution/alembic.ini .
COPY services/execution/migrations/ migrations/
COPY services/execution/app/ app/

RUN uv pip install --system --no-deps -e /app/common \
    && uv pip install --system --no-deps --no-sources -e .

RUN useradd --no-create-home --shell /bin/false appuser
RUN mkdir -p /data/driver-cache && chown appuser:appuser /data/driver-cache
USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
