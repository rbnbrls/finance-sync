# Wealthfolio HTTPS proxy — deployed via Coolify (finance-sync project).
#
# Serves the self-hosted Wealthfolio PWA (LXC 104, 192.168.3.50:8080) behind
# the Coolify Traefik entrypoint so every client (desktop + mobile PWA) uses
# one stable HTTPS URL: https://wealthfolio.7rb.nl
#
# Traffic path:
#   browser -> Cloudflare (TLS) -> cloudflared tunnel "proxmox"
#   -> Traefik (Coolify, *.7rb.nl wildcard cert) -> nginx -> Wealthfolio :8080
#
# The Wealthfolio port is never published to the internet directly — it is
# only reachable over the tunnel/proxy chain (see
# docs/wealthfolio-multi-device-access.md).
FROM nginx:1.27-alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf

# Port exposed to the Coolify/Traefik network.
EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1/ || exit 1
