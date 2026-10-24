#!/usr/bin/env bash
# Deploy the sats collector from YOUR machine - never from the VPS or an agent.
#
#   WRANGLER_VERSION=4.x.y ./deploy.sh          first deploy (creates the secret)
#   WRANGLER_VERSION=4.x.y ROTATE=1 ./deploy.sh rotate the secret
#
# Pin an exact wrangler release that has been out at least two weeks (SECURITY.md).
set -euo pipefail
cd "$(dirname "$0")"

: "${WRANGLER_VERSION:?set WRANGLER_VERSION to an exact wrangler release, e.g. WRANGLER_VERSION=4.40.0}"
if [[ ! "$WRANGLER_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "WRANGLER_VERSION must be an exact version like 4.40.0" >&2
  exit 1
fi
W=(npx --yes "wrangler@${WRANGLER_VERSION}")

"${W[@]}" whoami >/dev/null 2>&1 || "${W[@]}" login

# Deploy first: until the secret is set the Worker fails closed (HTTP 500).
"${W[@]}" deploy

if [[ "${ROTATE:-0}" == 1 ]] || ! "${W[@]}" secret list 2>/dev/null | grep -q '"COLLECTOR_SECRET"'; then
  secret=$(python3 -c "import secrets; print(secrets.token_hex(32))")
  printf '%s' "$secret" | "${W[@]}" secret put COLLECTOR_SECRET
  cat <<EOF

New collector secret. Save it in your password manager, then on the VPS put
this line in /etc/sats-collector.env and run: sudo systemctl restart sats-scan

  SATS_COLLECTOR_SECRET=${secret}

Clear your terminal scrollback afterwards. Don't paste it into chats.
EOF
else
  echo "COLLECTOR_SECRET already set; pass ROTATE=1 to replace it."
fi

echo
echo "Check: curl -i https://sats-collector.<your-subdomain>.workers.dev/health"
echo "Expect 503 stale until the synced node starts pushing, then 200 ok."
