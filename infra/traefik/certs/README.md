# Gateway TLS certificate

Traefik loads two files from this directory, as `infra/traefik/dynamic.yml` names them:
`server-chain.crt` (the certificate) and `server.key` (its private key). Neither is
tracked. `make certs` writes them when `server.key` is absent: a ten-year self-signed
certificate with the subject alternative names `DNS:localhost`, `IP:127.0.0.1`, the
`HERD_GATEWAY_BIND` address when one is set, and any `CERT_SANS` entries. `make up`,
`make dev`, `make prod`, and the gates run it first, and it never writes over an
existing key (`CERT_FORCE=1` regenerates both files on purpose).

This README is tracked so the directory exists in every clone; without it, a `docker
compose up` run before `make certs` would make Docker create the directory as root.

How to trust the certificate on a client, and how to replace it with your own: see
`FRESH_SETUP.md` (step 3) and `docs/OPERATIONS.md` (TLS certificate rotation).
