#!/usr/bin/env bash
# Deploy the Psyndica oracle from YOUR machine - never from the VPS or an agent.
#
#   WRANGLER_VERSION=4.x.y ./deploy.sh          first deploy (creates the token)
#   WRANGLER_VERSION=4.x.y ROTATE=1 ./deploy.sh rotate the token
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

# Deploy first: until the token is set the Worker fails closed (HTTP 500).
"${W[@]}" deploy

if [[ "${ROTATE:-0}" == 1 ]] || ! "${W[@]}" secret list 2>/dev/null | grep -q '"ORACLE_TOKEN"'; then
  token=$(python3 -c "import secrets; print(secrets.token_hex(32))")
  printf '%s' "$token" | "${W[@]}" secret put ORACLE_TOKEN
  cat <<MSG

New oracle token. Save it in your password manager as "psyndica-oracle".
Use it as:  Authorization: Bearer <token>

  ${token}

Clear your terminal scrollback afterwards. Don't paste it into chats.
MSG
else
  echo "ORACLE_TOKEN already set; pass ROTATE=1 to replace it."
fi

echo
echo "Check: curl -i https://psyndica-oracle.<your-subdomain>.workers.dev/health"
