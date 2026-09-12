# FRRouting node for the emulated-gear test lab (docs/NOS_LAB.md): the
# Layer 3 / Cisco-dialect target, driven over SSH-to-vtysh with netmiko's
# cisco_ios device type. This exact Dockerfile was built and verified live
# on 2026-09-10; do not "simplify" the group memberships or the vtysh.conf
# touch below, both are load-bearing (see the comment on the RUN line).
#
# Pinned 2026-09-12 (issue #783) to the digest that built the running
# nos-test-frr container (FRRouting 8.4_git per `vtysh -c 'show version'`).
# No frrouting/frr version tag (v8.4.0, v8.4.1, ...) matches this digest:
# upstream `latest` tracks a rolling git snapshot with no corresponding
# release tag, so this is a digest-only reference (no tag at all) rather
# than a floating `:latest`. To bump: pull the new `latest`,
# `docker image inspect frrouting/frr:latest --format
# '{{index .RepoDigests 0}}'` for the digest, rebuild, re-verify vtysh
# output and driver wording still match live, then update the digest here.
FROM frrouting/frr@sha256:990e83490108b686fd6df3b1cafa6bdbb2714acb00eedb9a89693946f46f45ce

# netadmin MUST be a member of BOTH frr and frrvty, and /etc/frr/vtysh.conf
# MUST exist, or vtysh fails on login with "failed to connect to any
# daemons" even though sshd and the FRR daemons are both healthy. Do not
# strip either group or the vtysh.conf touch.
#
# openssh pinned 2026-09-12 (issue #783) to the version present in the
# running nos-test-frr container (`apk info -v openssh` inside it), on
# Alpine 3.16.2. To bump: rebuild without the pin first to see what version
# `apk add --no-cache openssh` resolves to on the then-current base image,
# then pin that exact string.
RUN apk add --no-cache openssh=9.0_p1-r5 \
 && ssh-keygen -A \
 && adduser -D -s /usr/bin/vtysh netadmin \
 && echo "netadmin:netadmin" | chpasswd \
 && addgroup netadmin frr \
 && addgroup netadmin frrvty \
 && touch /etc/frr/vtysh.conf \
 && chown frr:frr /etc/frr/vtysh.conf \
 && sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config \
 && sed -i 's/^staticd=.*/staticd=yes/' /etc/frr/daemons \
 && sed -i 's/^zebra=.*/zebra=yes/' /etc/frr/daemons

COPY start.sh /usr/local/bin/start.sh
RUN chmod +x /usr/local/bin/start.sh
ENTRYPOINT ["/usr/local/bin/start.sh"]
