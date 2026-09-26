#!/bin/bash
# Continua sandbox network fence (house ruling 2026-09-07): allow internet
# HTTP/HTTPS + DNS only; deny EVERYTHING else for uid continua, including
# all local/LAN (localhost, LLM endpoints, control endpoint, Qdrant, RFC1918,
# CGNAT/tailscale, link-local). Idempotent; re-applied at boot via
# continua-sandbox-net.service.
#
# NOTE (bug fixed 2026-09-07): -I OUTPUT 1 in forward order inverted the
# chain — the blanket DROP landed FIRST and blackholed everything. Now the
# rules are inserted in REVERSE so the final chain order is: local drops →
# protocol allows → default drop.
UID_NAME=continua

# flush: delete every fence rule by line number, highest first
while true; do
  LINE=$(sudo iptables -L OUTPUT --line-numbers -n | grep "C-continua-fence" \
         | sort -rn | head -1 | awk '{print $1}')
  [ -z "$LINE" ] && break
  sudo iptables -D OUTPUT "$LINE" || break
done

# desired final chain order (first match wins):
RULES=(
  "-d 127.0.0.0/8 -j DROP"
  "-d 10.0.0.0/8 -j DROP"
  "-d 172.16.0.0/12 -j DROP"
  "-d 127.0.0.1/16 -j DROP"
  "-d 0.0.0.0/8 -j DROP"
  "-d 169.254.0.0/16 -j DROP"
  "-d 100.64.0.0/10 -j DROP"
  "-p tcp --dport 443 -j ACCEPT"
  "-p tcp --dport 80 -j ACCEPT"
  "-p udp --dport 53 -j ACCEPT"
  "-p tcp --dport 53 -j ACCEPT"
  "-j DROP"
)

# insert each at position 1 in REVERSE so the final order matches RULES
for ((i=${#RULES[@]}-1; i>=0; i--)); do
  sudo iptables -I OUTPUT 1 -m owner --uid-owner $UID_NAME \
    -m comment --comment "C-continua-fence" ${RULES[$i]}
done

echo "fence order now:"
sudo iptables -S OUTPUT | grep "C-continua-fence"
