# The restarter (issue #1326, ADR 0017): the one HERD process that holds the Docker
# socket. Standard library only, so the image is the services' Python base (tag and
# digest, pinned equal by tests/unit/test_uv_pins.py) and one file; no package is
# installed.
#
# It runs as root, by decision: the host socket is owned by root (mode 0660, group
# docker), so uid 0 opens it with no capability, and a non-root user with the docker
# group would hold the same root-equivalent API while adding a host group id knob.
# docker-compose.yml runs it read-only, with every capability dropped and
# no-new-privileges, on the internal herd-ctl network only.
# tests/unit/test_dockerfile_users.py records this exemption by name.
FROM python:3.12.16-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

WORKDIR /app

COPY restarter.py /app/restarter.py

# Build identifier (issue #846), as in the service images: the last layer, so a
# changed build string invalidates nothing above it.
ARG HERD_BUILD=dev
ARG HERD_BUILD_DATE=
ENV HERD_BUILD=${HERD_BUILD} HERD_BUILD_DATE=${HERD_BUILD_DATE}

EXPOSE 8000

CMD ["python", "-I", "-B", "/app/restarter.py"]
