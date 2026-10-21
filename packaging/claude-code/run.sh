#!/usr/bin/env bash
# Claude Code's entry point for an installed energycore-outages bundle (see bin/install-claude-code).
# bin/install-claude-code copies this file to the root of each install.
#
# Database credentials come from ${TIOS_CONFIG_DIR:-~/.config/tios}/outages.env (mode 600), as
# KEY=value lines: TIOS_DB_USERNAME and TIOS_DB_PASSWORD, optionally TIOS_DB_HOST, TIOS_DB_PORT
# and TIOS_DB_DATABASE. They're kept there rather than in ~/.claude.json, which would hold them
# in plain text. There is no config/mysql.yml in an install, so lib/db.py reads these instead.
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
creds="${TIOS_CONFIG_DIR:-$HOME/.config/tios}/outages.env"
[[ -r "$creds" ]] || { echo "missing $creds; run bin/install-claude-code in tios-regression-tool" >&2; exit 1; }

# Read literally rather than sourcing, so a password with $, quotes or spaces arrives intact.
while IFS= read -r line || [[ -n "$line" ]]; do
  [[ "$line" =~ ^(TIOS_DB_(USERNAME|PASSWORD|HOST|PORT|DATABASE))=(.*)$ ]] && export "${BASH_REMATCH[1]}=${BASH_REMATCH[3]}"
done < "$creds"

exec uv --directory "$DIR" run --frozen --quiet mcp_server.py
