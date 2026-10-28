# mcp-bridge

**Visibility: public.**

Serves every `<name>-cli` (notion-cli, slack-user-cli, tg-cli, ...) as a local
MCP server, so MCP clients such as Claude Desktop and Cowork can call them. No
CLI changes: the bridge reads each CLI's click command tree and runs the CLI
itself for every call.

## How it works

- **Discovery.** Every link in `~/.local/bin` that resolves into a `<name>-cli`
  repo with a `SKILL.md` is a CLI. A repo with two distinct launchers (whatsapp-cli
  ships `wa` and `signal-cli`) gets one server per launcher.
- **Tools.** `introspect.py` runs with the CLI's own uv interpreter, imports the
  launcher and dumps its click tree. Each leaf command becomes a tool whose input
  schema mirrors its options and arguments. The tree is cached in
  `~/.cache/mcp-bridge/` until the CLI's sources change.
- **Calls.** A call runs `uv run --script <launcher> <command> --json ...` and
  returns stdout, plus the end of stderr, as text.
- **Guide.** The server's instructions are the SKILL.md description, and a
  `usage_guide` tool returns the full SKILL.md, so a client does not load every
  guide into every session.
- **Writes.** A command with `--yes` takes `confirm` and stays a dry run unless it
  is true. Writes without that flag are listed in [`writes.toml`](writes.toml):
  `outward` ones are annotated destructive so clients ask first, `local` ones
  (downloads, caches) are marked not read-only. Everything else is read-only.
- **Not exposed.** Interactive or never-ending commands: `login`, `logout`,
  `auth`, `pair`, `listen`, `serve`, `watch`, `poll`, `mcp`, plus hidden commands
  and options that prompt.

## Usage

```bash
mcp-bridge list                       # CLIs, tool and write counts
mcp-bridge tools notion-cli           # one CLI's tools, read/write/local
mcp-bridge call tg-cli chats '{"limit": 5}'   # run a tool as the server would
mcp-bridge audit                      # writes.toml vs the CLIs' commands
mcp-bridge install                    # dry run: servers it would register
mcp-bridge install --yes              # write Claude Desktop's config
```

`install --yes` adds one entry per CLI to
`~/Library/Application Support/Claude/claude_desktop_config.json`, keeps any
other server, removes bridge servers whose CLI is gone, and saves the previous
file as `claude_desktop_config.json.bak`. Quit and reopen Claude Desktop to load
them; each server can then be switched off on its own in Settings.

Run `mcp-bridge install --yes` again after adding a CLI, and `mcp-bridge audit`
after adding a write command to one.

## Extensions for people without a terminal

`mcp-bridge pack <cli> --version X.Y.Z` builds a Claude Desktop extension
(`.mcpb`): double-click it, click Install, and the CLI's tools are in Claude.
Nothing else needs installing: Claude Desktop brings `uv`, which installs
Python and the locked dependencies.

The extension holds the bridge, the CLI's tracked files (no tests, no CI), a
`pyproject.toml` merging the CLI's exact pins with the bridge's, its `uv.lock`,
and a `manifest.json` (server type `uv`). Every extension runs Python
3.11 (`.python-version`), so a fresh install cannot pick a newer Python that
some pinned dependency has no wheel for.

A CLI customizes its extension with an optional `mcpb.json`:

| Key                | Purpose                                                       |
| ------------------ | ------------------------------------------------------------- |
| `display_name`     | Name shown in Claude Desktop                                  |
| `description`      | One-line summary                                              |
| `long_description` | Setup steps shown on the extension page                       |
| `platforms`        | `darwin`, `linux`, `win32` (default `darwin`)                 |
| `user_config`      | Settings fields (MCPB format); `sensitive` ones are masked    |
| `env`              | Environment for the CLI, e.g. `"${user_config.api_hash}"`     |
| `build_wheels`     | Requirements PyPI has no wheel for; built here and shipped    |
| `icon`             | Path to a PNG in the repo                                     |
| `include`          | `{"dest": "../source"}`: files from outside the repo to ship  |

`build_wheels` compiles for this machine's architecture only (whatsapp-cli's
`signal-protocol` needs Rust and `PROTOC` pointing at a working `protoc`).

## Configuration

| Variable             | Default                | Purpose                      |
| -------------------- | ---------------------- | ---------------------------- |
| `MCP_BRIDGE_BIN`     | `~/.local/bin`         | Where CLI links are found    |
| `MCP_BRIDGE_CACHE`   | `~/.cache/mcp-bridge`  | Command tree cache           |
| `MCP_BRIDGE_TIMEOUT` | `600`                  | Seconds before a call is cut |

## Tests

```bash
uv run --script tests/test_mcp_bridge.py
```

Offline: schema and argument mapping, then a demo CLI served over stdio and
driven by the MCP client.
