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
# The uv binary comes from a named stage, not `COPY --from=<image>`: Dependabot's
# docker ecosystem reads only FROM lines, so this is the form it keeps current
# (issue #1300). Tag and digest are bumped together, by Dependabot or by hand.
FROM ghcr.io/astral-sh/uv:0.13.8@sha256:5cc1805e2426484abca4ad46ea783bab281fdb225fd82d8ce6d66a81254bac85 AS uv

FROM python:3.12.16-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

WORKDIR /app

COPY --from=uv /uv /usr/local/bin/uv

# Workspace lock and pyprojects live under /lock, kept out of /app so they
# never collide with the service's own pyproject.toml-driven package
# discovery below. This layer only invalidates when a dependency actually
# changes, not on every source edit.
WORKDIR /lock
COPY pyproject.toml uv.lock ./
COPY services/config/pyproject.toml services/config/pyproject.toml

RUN uv export --frozen --package herd-config --no-hashes --no-emit-workspace \
    --no-default-groups --no-header -o requirements.txt \
    && uv pip install --system -r requirements.txt

WORKDIR /app

COPY services/config/pyproject.toml .
COPY services/config/app/ app/

RUN uv pip install --system --no-deps --no-sources -e .

RUN mkdir -p /data/herd-config

# Build identifier (issue #846), stamped in as late as possible so a changed
# build string invalidates only this metadata layer, never the uv export /
# pip install or source COPY layers above. Computed on the host by the
# Makefile and passed in as build args; the ENV line is what makes the value
# visible to the running process, since an ARG does not persist on its own.
ARG HERD_BUILD=dev
ARG HERD_BUILD_DATE=
ENV HERD_BUILD=${HERD_BUILD} HERD_BUILD_DATE=${HERD_BUILD_DATE}

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
