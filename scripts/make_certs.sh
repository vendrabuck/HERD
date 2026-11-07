#!/bin/sh
# Generate the gateway's self-signed TLS certificate when it has no key
# (issue #1239). `make certs` runs this, and `make up`, `dev`, `prod`, and the
# gate's stack start depend on it, so a fresh clone is served this certificate
# instead of Traefik's built-in `TRAEFIK DEFAULT CERT`.
#
# Usage: make_certs.sh <certs directory>
#
# Writes the two files infra/traefik/dynamic.yml names, in the directory given:
# server.key (RSA 2048, mode 0600) and server-chain.crt (a self-signed X.509
# certificate valid for 3650 days, no CA chain: the "chain" holds the one
# certificate). Its subject alternative names are DNS:localhost and
# IP:127.0.0.1, then IP:<HERD_CERT_BIND> when that is a set address other than
# 0.0.0.0 or :: (the Makefile passes the HERD_GATEWAY_BIND value it reads from
# the environment or .env), then each entry of CERT_SANS, a comma-separated list
# of DNS:<name> and IP:<address> entries. Repeated entries are written once.
#
# It never writes over an existing key:
# - key and certificate both present: prints one line, runs no openssl, and
#   changes nothing;
# - key present, certificate missing (a checkout that supplied its own key
#   for the certificates the repository used to track): issues a new
#   self-signed certificate for that key and leaves the key unchanged;
# - no key: generates a new key and certificate (an old certificate without
#   its key is unusable and is replaced).
# CERT_FORCE set to anything but empty, 0, false, or no regenerates both files
# and says so; clients that trusted the old certificate must trust the new one.
#
# Exits 0 on success, 2 on a usage error or a malformed CERT_SANS entry, and
# openssl's own status when it fails. Nothing is moved into the directory until
# both files are written, so a failed run leaves the directory as it was.
set -eu

if [ $# -ne 1 ] || [ -z "$1" ]; then
	echo "usage: make_certs.sh <certs directory>" >&2
	exit 2
fi

dir=$1
key="$dir/server.key"
crt="$dir/server-chain.crt"
days=3650

case "${CERT_FORCE:-}" in
	"" | 0 | false | no) force=0 ;;
	*) force=1 ;;
esac

if [ "$force" = 0 ] && [ -s "$key" ] && [ -s "$crt" ]; then
	echo "TLS certificate present: $key and $crt exist, nothing generated (CERT_FORCE=1 regenerates both)."
	exit 0
fi

sans="DNS:localhost,IP:127.0.0.1"
bind=$(printf '%s' "${HERD_CERT_BIND:-}" | tr -d ' \t[]')
case "$bind" in
	"" | 0.0.0.0 | ::) ;;
	*) sans="$sans,IP:$bind" ;;
esac

old_ifs=$IFS
IFS=,
set -f
for entry in ${CERT_SANS:-}; do
	entry=$(printf '%s' "$entry" | tr -d ' \t')
	case "$entry" in
		"") ;;
		DNS:?* | IP:?*) sans="$sans,$entry" ;;
		*)
			echo "CERT_SANS entry '$entry' is not DNS:<name> or IP:<address>; nothing generated." >&2
			exit 2
			;;
	esac
done
set +f
IFS=$old_ifs

# Drop repeated entries, keeping the first of each.
sans=$(printf '%s' "$sans" | tr ',' '\n' | awk '!seen[$0]++' | paste -sd, -)

mkdir -p "$dir"
work=$(mktemp -d "$dir/.make-certs.XXXXXX")
# A signal exits, so the EXIT trap removes the work directory then too.
trap 'rm -rf "$work"' EXIT
trap 'exit 1' HUP INT PIPE TERM

cat > "$work/openssl.cnf" <<CNF
[req]
distinguished_name = dn
prompt = no
x509_extensions = v3

[dn]
CN = HERD dev gateway

[v3]
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature,keyEncipherment
extendedKeyUsage = serverAuth
subjectKeyIdentifier = hash
subjectAltName = $sans
CNF

if [ "$force" = 0 ] && [ -s "$key" ]; then
	openssl req -x509 -new -key "$key" -sha256 -days "$days" \
		-config "$work/openssl.cnf" -out "$work/server-chain.crt" 2>"$work/err" \
		|| { cat "$work/err" >&2; exit 1; }
	mv "$work/server-chain.crt" "$crt"
	echo "$key exists but $crt was missing: issued a new self-signed certificate for the existing key (the key is unchanged)."
else
	umask 077
	openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days "$days" \
		-config "$work/openssl.cnf" -keyout "$work/server.key" \
		-out "$work/server-chain.crt" 2>"$work/err" \
		|| { cat "$work/err" >&2; exit 1; }
	chmod 600 "$work/server.key"
	chmod 644 "$work/server-chain.crt"
	if [ "$force" = 1 ] && [ -s "$key" ]; then
		echo "CERT_FORCE: regenerating the gateway key and certificate over the existing $key."
	fi
	mv "$work/server-chain.crt" "$crt"
	mv "$work/server.key" "$key"
	echo "Generated a self-signed TLS certificate and key for the gateway: $crt and $key."
fi

abs_crt=$(cd "$dir" && pwd)/server-chain.crt
echo "Valid for $days days. Subject alternative names: $(printf '%s' "$sans" | sed 's/,/, /g')."
echo "Trust it by hand on each client (FRESH_SETUP.md step 3); nothing here runs these:"
echo "  Ubuntu:  sudo cp $abs_crt /usr/local/share/ca-certificates/herd-dev.crt && sudo update-ca-certificates"
echo "  Windows: import server-chain.crt into Trusted Root Certification Authorities (certutil -addstore Root server-chain.crt, as administrator)"
echo "A Traefik that is already running serves the new certificate only once recreated: docker compose up -d --no-deps --force-recreate traefik"
