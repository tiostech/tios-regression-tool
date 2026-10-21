# Shared by bin/package-desktop and bin/install-claude-code (sourced, not run).
#
# stage_bundle SRC STAGE copies the MCP server out of the tree at SRC into STAGE, from an
# allowlist, so config/mysql.yml and anything else with credentials can never end up in it.
# Both the Claude Desktop .mcpb and the Claude Code install are built from this, so they always
# run the same files with the same pinned dependencies.

stage_bundle() {
  local src="$1" stage="$2"

  cp "$src/packaging/claude-desktop/manifest.json" "$src/packaging/claude-desktop/pyproject.toml" "$stage/"
  cp "$src/.python-version" "$src/mcp_server.py" "$stage/"
  mkdir -p "$stage/lib" "$stage/prompts"
  cp "$src"/lib/*.py "$stage/lib/"
  cp "$src/prompts/outage_briefing.md" "$src/prompts/binding_drivers.md" "$stage/prompts/"

  # Pin the dependency versions inside the bundle so every install gets the same set.
  if command -v uv >/dev/null 2>&1; then
    (cd "$stage" && uv lock --quiet)
  else
    echo "⚠ uv not found; packaging without uv.lock (installs will resolve latest versions)."
  fi
}
