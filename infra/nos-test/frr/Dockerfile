# FRRouting node for the emulated-gear test lab (docs/NOS_LAB.md): the
# Layer 3 / Cisco-dialect target, driven over SSH-to-vtysh with netmiko's
# cisco_ios device type. This exact Dockerfile was built and verified live
# on 2026-09-10; do not "simplify" the group memberships or the vtysh.conf
# touch below, both are load-bearing (see the comment on the RUN line).
FROM frrouting/frr:latest

# netadmin MUST be a member of BOTH frr and frrvty, and /etc/frr/vtysh.conf
# MUST exist, or vtysh fails on login with "failed to connect to any
# daemons" even though sshd and the FRR daemons are both healthy. Do not
# strip either group or the vtysh.conf touch.
RUN apk add --no-cache openssh \
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
