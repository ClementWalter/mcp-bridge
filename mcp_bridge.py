#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "click==8.4.1",
#     "mcp==2.3.0",
#     "pyyaml==6.0.3",
# ]
#
# [tool.uv]
# exclude-newer = "2026-10-27T00:00:00Z"
# ///
"""Serve the user's `<name>-cli` tools to MCP clients such as Claude Desktop and Cowork.

One generic stdio MCP server, started once per CLI (`mcp-bridge serve notion-cli`).
It finds the CLIs through their ~/.local/bin links, reads each one's click command
tree inside the CLI's own uv environment (introspect.py), and exposes every leaf
command as a tool whose input schema mirrors the command's options and arguments.
A call runs the CLI as a subprocess with --json, so the CLI stays the single
implementation and the bridge never imports its code.

Commands with a --yes flag are writes: the tool takes `confirm` and stays a dry
run unless it is true. Writes without that flag are declared in writes.toml, so
clients know to ask before running them; every other command is read-only.
Interactive commands (login, pair, listen, ...) are not exposed. `install` registers one server per CLI in Claude Desktop's config.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

import anyio
import click
import mcp.types as types
import yaml
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("mcp-bridge")

BRIDGE = Path(__file__).resolve()
INTROSPECT = BRIDGE.parent / "introspect.py"
BIN_DIR = Path(os.environ.get("MCP_BRIDGE_BIN", Path.home() / ".local" / "bin"))
CACHE_DIR = Path(os.environ.get("MCP_BRIDGE_CACHE", Path.home() / ".cache" / "mcp-bridge"))
DESKTOP_CONFIG = Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
# Commands that wait for a human (QR scan, password, live stream) or never return.
EXCLUDED = frozenset({"login", "logout", "auth", "pair", "listen", "serve", "watch", "poll", "mcp"})
# Handled by the bridge itself rather than exposed as tool inputs.
RESERVED = frozenset({"--help", "--json", "--yes"})
WRITES = tomllib.loads((BRIDGE.parent / "writes.toml").read_text())
# Leaf names that usually mean a write; `audit` flags them when writes.toml misses them.
WRITE_VERBS = re.compile(
    r"^draft$|^(add|append|apply|archive|check|clear|comment|connect|create|delete|download|dump|edit|export|import|"
    r"like|link|mark|move|pdf|post|pull|react|remove|rename|reply|resolve|rewrite|save|send|set|submit|sync|unlike|"
    r"update|upload|write)(-|$)|-(add|delete|edit|remove|sync|upload|write)$"
)
MAX_OUTPUT = 200_000
STDERR_TAIL = 4_000
TIMEOUT = float(os.environ.get("MCP_BRIDGE_TIMEOUT", "600"))


@dataclass(frozen=True)
class Cli:
    name: str
    launcher: Path
    repo: Path
    # Interpreter that already has the CLI's dependencies (a packed extension); None runs it through uv.
    python: str | None = None

    @property
    def writes_key(self) -> str:
        """writes.toml is keyed by repo, shared by a repo's launchers."""
        return self.repo.name


def repo_of(launcher: Path) -> Path:
    return launcher.parent.parent if launcher.parent.name == "bin" else launcher.parent


def discover(bin_dir: Path = BIN_DIR) -> dict[str, Cli]:
    """Every link in bin_dir that resolves into a `<name>-cli` repo with a SKILL.md."""
    by_target: dict[Path, list[str]] = {}
    for link in sorted(bin_dir.iterdir()) if bin_dir.is_dir() else []:
        if not link.is_symlink():
            continue
        target = link.resolve()
        repo = repo_of(target)
        if target.is_file() and repo.name.endswith("-cli") and (repo / "SKILL.md").is_file():
            by_target.setdefault(target, []).append(link.name)
    per_repo: dict[Path, list[Path]] = {}
    for target in by_target:
        per_repo.setdefault(repo_of(target), []).append(target)
    out = {}
    for repo, targets in per_repo.items():
        for target in targets:
            # Aliases share a target; a repo with several distinct launchers gets one server each.
            name = repo.name if len(targets) == 1 else f"{repo.name}-{min(by_target[target], key=len)}"
            out[name] = Cli(name, target, repo)
    return dict(sorted(out.items()))


def resolve(name_or_path: str, bundled: bool = False) -> Cli:
    path = Path(name_or_path).expanduser()
    if path.is_file():
        target = path.resolve()
        return Cli(repo_of(target).name, target, repo_of(target), sys.executable if bundled else None)
    clis = discover()
    if name_or_path not in clis:
        raise click.UsageError(f"unknown CLI {name_or_path!r}; run `mcp-bridge list` to see the known ones")
    return clis[name_or_path]


def source_stamp(repo: Path) -> int:
    """Newest mtime of the repo's Python files: the cache key for its command tree."""
    newest = 0
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in {"__pycache__", "node_modules", "tests"}]
        for f in files:
            if f.endswith(".py") or "." not in f:
                newest = max(newest, os.stat(os.path.join(root, f)).st_mtime_ns)
    return newest


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if proc.returncode:
        tail = proc.stderr.strip()[-STDERR_TAIL:]
        raise click.ClickException(f"`{' '.join(cmd[:4])} …` failed:\n{tail}")
    return proc


def introspect(cli: Cli) -> list[dict]:
    """The CLI's commands, read in its own uv environment and cached until its sources change."""
    cache = CACHE_DIR / f"{cli.name}.json"
    stamp = [str(cli.launcher), source_stamp(cli.repo), INTROSPECT.stat().st_mtime_ns]
    if cache.is_file():
        cached = json.loads(cache.read_text())
        if cached.get("stamp") == stamp:
            return cached["commands"]
    python = cli.python
    if python is None:
        run(["uv", "sync", "-q", "--script", str(cli.launcher)])
        python = run(["uv", "python", "find", "--script", str(cli.launcher)]).stdout.strip()
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "tree.json"
        run([python, "-I", str(INTROSPECT), str(cli.launcher), str(out)])
        commands = json.loads(out.read_text())["commands"]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"stamp": stamp, "commands": commands}))
    return commands


# Sync to the user's own secret vault, which an extension's users do not have.
BUNDLED_EXCLUDED = frozenset({"auth-sync"})


def exposed(commands: list[dict], bundled: bool = False) -> list[dict]:
    excluded = EXCLUDED | BUNDLED_EXCLUDED if bundled else EXCLUDED
    return [c for c in commands if not c["hidden"] and not excluded & set(c["path"])]


def tool_name(path: list[str]) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", "_".join(path))[:64]


def spelled(cmd: dict, spelling: str) -> dict | None:
    return next((p for p in cmd["params"] if spelling in p["opts"]), None)


def is_write(cmd: dict) -> bool:
    """A write the CLI itself guards: dry run unless --yes."""
    return spelled(cmd, "--yes") is not None


def access(repo: str, cmd: dict) -> str:
    """One of dry-run, outward, local or read."""
    if is_write(cmd):
        return "dry-run"
    listed = WRITES.get(repo, {})
    path = " ".join(cmd["path"])
    for kind in ("outward", "local"):
        if path in listed.get(kind, []):
            return kind
    return "read"


def inputs(cmd: dict) -> list[dict]:
    return [p for p in cmd["params"] if not RESERVED & set(p["opts"]) and not p["hidden"] and not p["prompt"]]


def json_type(p: dict) -> dict:
    if p["is_flag"]:
        return {"type": "boolean"}
    if p["count"]:
        return {"type": "integer", "minimum": 0}
    kind = p["type"]
    if p["choices"]:
        item = {"type": "string", "enum": p["choices"]}
    elif kind.startswith("integer"):
        item = {"type": "integer"}
    elif kind.startswith("float"):
        item = {"type": "number"}
    elif kind == "boolean":
        item = {"type": "boolean"}
    else:
        item = {"type": "string"}
    if p["multiple"]:
        return {"type": "array", "items": item}
    if p["nargs"] > 1:
        return {"type": "array", "items": item, "minItems": p["nargs"], "maxItems": p["nargs"]}
    return item


def input_schema(cmd: dict) -> dict:
    props, required = {}, []
    for p in inputs(cmd):
        prop = json_type(p)
        if p["help"]:
            prop["description"] = p["help"]
        if p["default"] not in (None, (), []) and not p["required"]:
            prop["default"] = p["default"]
        props[p["name"]] = prop
        if p["required"]:
            required.append(p["name"])
    if is_write(cmd):
        props["confirm"] = {
            "type": "boolean",
            "default": False,
            "description": "Perform the write. Without it the command is a dry run that only shows what it would do.",
        }
    schema = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return schema


def description(cmd: dict, kind: str) -> str:
    text = cmd["help"].replace("\b", "").strip()
    epilog = cmd["epilog"].replace("\b", "").strip()
    if epilog:
        text += "\n\n" + epilog
    notes = {
        "dry-run": "Write command: dry run unless confirm=true.",
        "outward": "Write command: acts immediately, there is no dry run.",
        "local": "Writes only local files or caches.",
    }
    if kind in notes:
        text += "\n\n" + notes[kind]
    return text


def tool(repo: str, cmd: dict) -> types.Tool:
    kind = access(repo, cmd)
    return types.Tool(
        name=tool_name(cmd["path"]),
        description=description(cmd, kind),
        input_schema=input_schema(cmd),
        annotations=types.ToolAnnotations(
            title=" ".join(cmd["path"]),
            read_only_hint=kind == "read",
            destructive_hint=kind in ("dry-run", "outward"),
            open_world_hint=kind != "local",
        ),
    )


def flag(p: dict) -> str:
    """The option's longest spelling, the most readable in a process listing."""
    return max(p["opts"], key=len)


def argv(cmd: dict, arguments: dict) -> list[str]:
    """CLI arguments for a tool call: subcommand path, --json where accepted, options, then positionals."""
    out = list(cmd["path"])
    for depth in sorted(cmd["json_at"], reverse=True):
        out.insert(depth, "--json")
    if spelled(cmd, "--json"):
        out.append("--json")
    positionals = []
    for p in inputs(cmd):
        if p["name"] not in arguments or arguments[p["name"]] is None:
            continue
        value = arguments[p["name"]]
        values = value if isinstance(value, list) else [value]
        if p["kind"] == "argument":
            positionals += [str(v) for v in values]
        elif p["is_flag"]:
            if value:
                out.append(flag(p))
            elif p["secondary_opts"]:
                out.append(max(p["secondary_opts"], key=len))
        elif p["count"]:
            out += [flag(p)] * int(value)
        elif p["multiple"]:
            for v in values:
                out += [flag(p), str(v)]
        else:
            out += [flag(p), *(str(v) for v in values)]
    if arguments.get("confirm") and is_write(cmd):
        out.append("--yes")
    # "--" keeps a positional that starts with "-" from being read as an option.
    return out + (["--", *positionals] if positionals else [])


def unknown_arguments(cmd: dict, arguments: dict) -> list[str]:
    allowed = {p["name"] for p in inputs(cmd)} | ({"confirm"} if is_write(cmd) else set())
    return sorted(set(arguments) - allowed)


def render(returncode: int, stdout: str, stderr: str) -> types.CallToolResult:
    text = stdout
    if len(text) > MAX_OUTPUT:
        text = text[:MAX_OUTPUT] + f"\n… output truncated at {MAX_OUTPUT} characters; narrow the query."
    tail = stderr.strip()[-STDERR_TAIL:]
    if tail:
        text = f"{text}\n\nstderr:\n{tail}" if text else tail
    if returncode:
        text = f"exit status {returncode}\n{text}"
    return types.CallToolResult(content=[types.TextContent(type="text", text=text or "(no output)")], is_error=bool(returncode))


def launch(cli: Cli) -> list[str]:
    if cli.python:
        return [cli.python, str(cli.launcher)]
    return ["uv", "run", "-q", "--script", str(cli.launcher)]


async def execute(cli: Cli, args: list[str]) -> types.CallToolResult:
    try:
        with anyio.fail_after(TIMEOUT):
            proc = await anyio.run_process(launch(cli) + args, check=False, stdin=subprocess.DEVNULL)
    except TimeoutError:
        return render(124, "", f"timed out after {TIMEOUT:.0f}s (MCP_BRIDGE_TIMEOUT)")
    return render(proc.returncode, proc.stdout.decode(errors="replace"), proc.stderr.decode(errors="replace"))


GUIDE = "usage_guide"


def skill(cli: Cli) -> tuple[dict, str]:
    """SKILL.md split into its frontmatter and its body, the guide agents already follow."""
    text = (cli.repo / "SKILL.md").read_text()
    match = re.match(r"\A---\n(.*?)\n---\n", text, flags=re.S)
    if not match:
        return {}, text.strip()
    body = text[match.end():].strip()
    try:
        return yaml.safe_load(match.group(1)) or {}, body
    except yaml.YAMLError:
        # Skill loaders accept an unquoted one-line value holding ": "; read it as plain text.
        found = re.search(r"^description:\s*(.+)$", match.group(1), flags=re.M)
        return {"description": found.group(1).strip() if found else ""}, body


def instructions(cli: Cli) -> str:
    """Short on purpose: a client loads every server's instructions into each session."""
    summary = str(skill(cli)[0].get("description", "")).strip()
    return f"{summary}\n\nCall `{GUIDE}` once before the first other call: it has the recipes and gotchas."


def guide_tool(cli: Cli) -> types.Tool:
    return types.Tool(
        name=GUIDE,
        description=f"How to use the {cli.name} tools well: recipes, output conventions, gotchas.",
        input_schema={"type": "object", "properties": {}},
        annotations=types.ToolAnnotations(title="usage guide", read_only_hint=True, open_world_hint=False),
    )


def build_server(cli: Cli) -> Server:
    # A CLI command named like the guide would be shadowed by it.
    commands = {
        tool_name(c["path"]): c for c in exposed(introspect(cli), bundled=cli.python is not None)
        if tool_name(c["path"]) != GUIDE
    }

    async def list_tools(ctx, params) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[guide_tool(cli), *(tool(cli.writes_key, c) for c in commands.values())])

    async def call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        if params.name == GUIDE:
            return render(0, skill(cli)[1], "")
        cmd = commands.get(params.name)
        if cmd is None:
            return render(2, "", f"unknown tool {params.name!r}")
        arguments = params.arguments or {}
        extra = unknown_arguments(cmd, arguments)
        if extra:
            return render(2, "", f"unknown arguments {extra}; see the tool's input schema")
        return await execute(cli, argv(cmd, arguments))

    return Server(cli.name, instructions=instructions(cli), on_list_tools=list_tools, on_call_tool=call_tool)


def drop_unfilled_settings() -> None:
    """An optional extension setting left empty reaches us as its literal `${user_config.x}` template."""
    for key, value in list(os.environ.items()):
        if re.fullmatch(r"\$\{user_config\.[^}]*\}", value):
            del os.environ[key]


async def serve_stdio(cli: Cli) -> None:
    drop_unfilled_settings()
    server = build_server(cli)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def desktop_entry(cli: Cli) -> dict:
    uv = shutil.which("uv")
    if not uv:
        raise click.ClickException("uv is not on PATH")
    # Claude Desktop starts servers with a minimal PATH; the CLIs need uv, git, browsers' helpers.
    return {
        "command": uv,
        "args": ["run", "-q", "--script", str(BRIDGE), "serve", cli.name],
        "env": {"PATH": os.environ.get("PATH", "")},
    }


def managed(entry: dict) -> bool:
    return any(Path(a).name == BRIDGE.name for a in entry.get("args", []))


def merge_config(config: dict, clis: dict[str, Cli]) -> tuple[dict, list[str], list[str]]:
    """Config with one bridge server per CLI; other servers are kept untouched."""
    servers = dict(config.get("mcpServers", {}))
    stale = sorted(n for n, e in servers.items() if managed(e) and n not in clis)
    for name in stale:
        del servers[name]
    for name, cli in clis.items():
        if name in servers and not managed(servers[name]):
            raise click.ClickException(f"Claude Desktop already has a server named {name!r} that the bridge does not manage")
        servers[name] = desktop_entry(cli)
    return {**config, "mcpServers": servers}, sorted(clis), stale


MCPB = "@anthropic-ai/mcpb@2.1.2"
# One interpreter for every extension: a fresh uv picks the newest Python, which can
# lack wheels for a pinned dependency and fall back to a source build the user cannot run.
BUNDLE_PYTHON = "3.11"
PRIVACY_POLICY = "https://clementwalter.github.io/claude-extensions/privacy/"
# Not shipped in an extension: dev tooling, CI, tests.
PACK_SKIP = re.compile(r"^(tests/|\.github/|\.pre-commit-config\.yaml$|\.privacy-guard-allow$|\.gitignore$|uv\.lock$|mcpb\.json$)")


def script_header(path: Path) -> dict:
    """The PEP 723 block of a script, parsed."""
    match = re.search(r"^# /// script\n((?:#.*\n)+?)# ///$", path.read_text(), flags=re.M)
    if not match:
        return {}
    return tomllib.loads("".join(line[2:] + "\n" for line in match.group(1).splitlines()))


def dist_name(requirement: str) -> str:
    return re.split(r"[\s\[=<>!~;]", requirement, maxsplit=1)[0].lower().replace("_", "-")


def cli_dependencies(cli: Cli) -> tuple[list[str], str | None]:
    """The CLI's pinned requirements and exclude-newer cutoff, from pyproject.toml or the launcher."""
    pyproject = cli.repo / "pyproject.toml"
    if pyproject.is_file():
        project = tomllib.loads(pyproject.read_text())
        deps = list(project["project"].get("dependencies", []))
        deps += [f"{d}; sys_platform == 'darwin'" if ";" not in d else d
                 for d in project["project"].get("optional-dependencies", {}).get("macos", [])]
        return deps, project.get("tool", {}).get("uv", {}).get("exclude-newer")
    header = script_header(cli.launcher)
    return list(header.get("dependencies", [])), header.get("tool", {}).get("uv", {}).get("exclude-newer")


def merged_dependencies(cli: Cli) -> tuple[list[str], str | None]:
    """The CLI's pins plus the bridge's; the CLI's pin wins for a package both use."""
    cli_deps, cutoff = cli_dependencies(cli)
    taken = {dist_name(d) for d in cli_deps}
    bridge = [d for d in script_header(BRIDGE)["dependencies"] if dist_name(d) not in taken]
    return cli_deps + bridge, cutoff


def build_wheels(requirements: list[str], dest: Path) -> None:
    """Build, for this machine, wheels of requirements PyPI only ships as sdists."""
    dest.mkdir(parents=True, exist_ok=True)
    for req in requirements:
        # uv's own single-architecture Python: a python.org framework build tags the wheel
        # universal2 although it is compiled for this machine only.
        run(["uv", "run", "--no-project", "--managed-python", "--python", BUNDLE_PYTHON, "--with", "pip==25.3", "python", "-m", "pip",
             "wheel", "--no-deps", "--no-binary", dist_name(req), "--wheel-dir", str(dest), req])


def wheel_sources(requirements: list[str], wheels: Path) -> str:
    """[tool.uv.sources] pointing each requirement at its wheel per macOS architecture."""
    out = ""
    for req in requirements:
        name = dist_name(req)
        found = sorted(w for w in wheels.glob("*.whl") if dist_name(w.name.split("-")[0]) == name)
        if not found:
            raise click.ClickException(f"no wheel for {req} in {wheels}")
        entries = []
        for w in found:
            machine = "arm64" if w.name.endswith("_arm64.whl") else "x86_64" if w.name.endswith("_x86_64.whl") else None
            if machine is None:
                raise click.ClickException(f"{w.name}: only macOS arm64 / x86_64 wheels are supported")
            marker = f"sys_platform == 'darwin' and platform_machine == '{machine}'"
            entries.append(f'{{ path = "wheels/{w.name}", marker = "{marker}" }}')
        out += f"{name} = [{', '.join(entries)}]\n"
    return out


def toml_list(items: list[str]) -> str:
    return "[\n" + "".join(f"    {json.dumps(i)},\n" for i in items) + "]"


def manifest(cli: Cli, version: str, rel_launcher: str, overrides: dict) -> dict:
    meta, _ = skill(cli)
    summary = " ".join(str(meta.get("description", "")).split())
    remote = run(["git", "-C", str(cli.repo), "remote", "get-url", "origin"]).stdout.strip()
    url = re.sub(r"^git@github\.com:", "https://github.com/", remote).removesuffix(".git")
    commands = exposed(introspect(cli), bundled=True)
    out = {
        "manifest_version": "0.4",
        "name": cli.repo.name,
        "display_name": overrides.get("display_name", cli.repo.name),
        "version": version,
        "description": overrides.get("description", summary.split(". ")[0][:200]),
        "long_description": overrides.get("long_description", summary),
        "author": {"name": "Clément Walter", "url": "https://github.com/ClementWalter"},
        "repository": {"type": "git", "url": url},
        "homepage": url,
        "privacy_policies": [PRIVACY_POLICY],
        "server": {
            "type": "uv",
            "entry_point": "mcp_bridge.py",
            "mcp_config": {
                "command": "uv",
                # `python mcp_bridge.py`, not `uv run mcp_bridge.py`: the latter would read the
                # script's own PEP 723 header and build a second environment without the CLI.
                "args": ["run", "--directory", "${__dirname}", "--frozen", "python", "mcp_bridge.py",
                         "serve", "--bundled", rel_launcher],
                "env": overrides.get("env", {}),
            },
        },
        "tools": [{"name": GUIDE, "description": "How to use these tools well."}] + [
            {"name": tool_name(c["path"]), "description": (c["help"].strip().splitlines() or [""])[0]} for c in commands
        ],
        "compatibility": {"platforms": overrides.get("platforms", ["darwin"]), "runtimes": {"python": ">=3.11"}},
    }
    if overrides.get("user_config"):
        out["user_config"] = overrides["user_config"]
    if (cli.repo / "LICENSE").is_file():
        out["license"] = overrides.get("license", "MIT")
    return out


def stage(cli: Cli, build: Path, version: str, prebuilt: Path | None = None) -> Path:
    """Lay out an extension directory: bridge, the CLI's tracked files, pyproject, lock, manifest."""
    overrides_file = cli.repo / "mcpb.json"
    overrides = json.loads(overrides_file.read_text()) if overrides_file.is_file() else {}
    root = build / cli.name
    if root.exists():
        shutil.rmtree(root)
    dest = root / "cli" / cli.repo.name
    for rel in run(["git", "-C", str(cli.repo), "ls-files"]).stdout.splitlines():
        # Launcher links are skipped: the extension runs the target file directly, and zips drop links.
        if PACK_SKIP.match(rel) or (cli.repo / rel).is_symlink() or not (cli.repo / rel).is_file():
            continue
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cli.repo / rel, dest / rel)
    # Build products (gitignored, e.g. a compiled helper app) made in the repo, then shipped at the same path.
    build_cfg = overrides.get("build")
    if build_cfg:
        proc = subprocess.run(build_cfg["command"], cwd=cli.repo, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        if proc.returncode:
            raise click.ClickException(f"{cli.repo.name} build failed:\n{proc.stderr.strip()[-STDERR_TAIL:]}")
        for rel in build_cfg.get("include", []):
            src, dst = cli.repo / rel, dest / rel
            if not src.exists():
                raise click.ClickException(f"{cli.repo.name} build did not produce {rel}")
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
    # Files the CLI imports from outside its repo (a sibling checkout); dest is relative to the
    # staged CLI dir, source to the CLI repo.
    for rel_dest, rel_src in overrides.get("include", {}).items():
        src = (cli.repo / rel_src).resolve()
        if not src.is_file():
            raise click.ClickException(f"{cli.repo.name}/mcpb.json include: {rel_src} not found")
        (dest / rel_dest).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest / rel_dest)
    for f in (BRIDGE, INTROSPECT, BRIDGE.parent / "writes.toml"):
        shutil.copy2(f, root / f.name)
    deps, cutoff = merged_dependencies(cli)
    pyproject = f'[project]\nname = "{cli.name}-mcpb"\nversion = "{version}"\nrequires-python = "=={BUNDLE_PYTHON}.*"\n'
    pyproject += f"dependencies = {toml_list(deps)}\n"
    if cutoff:
        pyproject += f'\n[tool.uv]\nexclude-newer = "{cutoff}"\n'
    needs_wheels = overrides.get("build_wheels", [])
    if needs_wheels:
        # Prebuilt wheels (one per architecture, from CI) make one extension serve every Mac;
        # without them, this machine's architecture is built.
        if prebuilt:
            (root / "wheels").mkdir()
            for w in prebuilt.glob("*.whl"):
                shutil.copy2(w, root / "wheels" / w.name)
        else:
            build_wheels(needs_wheels, root / "wheels")
        pyproject += "\n[tool.uv.sources]\n" + wheel_sources(needs_wheels, root / "wheels")
    (root / "pyproject.toml").write_text(pyproject)
    (root / ".python-version").write_text(BUNDLE_PYTHON + "\n")
    run(["uv", "lock", "--directory", str(root)])
    rel_launcher = f"cli/{cli.repo.name}/{cli.launcher.relative_to(cli.repo)}"
    (root / "manifest.json").write_text(json.dumps(manifest(cli, version, rel_launcher, overrides), indent=2, ensure_ascii=False) + "\n")
    icon = overrides.get("icon")
    if icon:
        shutil.copy2(cli.repo / icon, root / Path(icon).name)
    return root


@click.group(epilog="\b\nExamples:\n  mcp-bridge list\n  mcp-bridge tools notion-cli\n  mcp-bridge install --yes")
def cli() -> None:
    """Serve the `<name>-cli` tools over MCP for Claude Desktop and Cowork."""


@cli.command("list", epilog="\b\nExamples:\n  mcp-bridge list\n  mcp-bridge list --json")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def list_cmd(as_json: bool) -> None:
    """CLIs the bridge can serve, with their tool counts."""
    rows = []
    for name, c in discover().items():
        try:
            cmds = exposed(introspect(c))
            kinds = [access(c.writes_key, x) for x in cmds]
            rows.append({"name": name, "launcher": str(c.launcher), "tools": len(cmds),
                         "writes": sum(k in ("dry-run", "outward") for k in kinds), "error": None})
        except click.ClickException as e:
            rows.append({"name": name, "launcher": str(c.launcher), "tools": 0, "writes": 0, "error": e.message.splitlines()[-1]})
    if as_json:
        click.echo(json.dumps(rows, indent=2))
        return
    for r in rows:
        status = f"ERROR {r['error']}" if r["error"] else f"{r['tools']} tools ({r['writes']} writes)"
        click.echo(f"{r['name']:28} {status}")


@cli.command(epilog="\b\nExamples:\n  mcp-bridge audit")
def audit() -> None:
    """Check writes.toml against the CLIs: stale entries, and write-looking commands it misses."""
    problems = 0
    by_repo: dict[str, set[str]] = {}
    for c in discover().values():
        paths = by_repo.setdefault(c.writes_key, set())
        for cmd in exposed(introspect(c)):
            path = " ".join(cmd["path"])
            paths.add(path)
            reviewed = path in WRITES.get(c.writes_key, {}).get("read", [])
            if access(c.writes_key, cmd) == "read" and not reviewed and WRITE_VERBS.search(cmd["path"][-1]):
                problems += 1
                click.echo(f"unlisted  {c.writes_key}: {path}")
    for repo, kinds in WRITES.items():
        for kind, paths in kinds.items():
            for path in sorted(set(paths) - by_repo.get(repo, set())):
                problems += 1
                click.echo(f"stale     {repo}: {path} ({kind})")
    if problems:
        raise SystemExit(1)
    click.echo("writes.toml matches the CLIs")


@cli.command(epilog="\b\nExamples:\n  mcp-bridge tools notion-cli\n  mcp-bridge tools whatsapp-cli-wa --json")
@click.argument("name")
@click.option("--json", "as_json", is_flag=True, help="Full tool definitions as MCP sends them.")
def tools(name: str, as_json: bool) -> None:
    """The tools one CLI exposes."""
    target = resolve(name)
    defs = [tool(target.writes_key, c) for c in exposed(introspect(target))]
    if as_json:
        click.echo(json.dumps([t.model_dump(by_alias=True, exclude_none=True) for t in defs], indent=2))
        return
    for t in defs:
        kind = "read" if t.annotations.read_only_hint else "write" if t.annotations.destructive_hint else "local"
        click.echo(f"{kind:5} {t.name:32} {', '.join(t.input_schema['properties'])}")


@cli.command(epilog="\b\nExamples:\n  mcp-bridge call tg-cli chats '{\"limit\": 5}'\n  mcp-bridge call tg-cli send '{\"peer\": \"me\", \"text\": \"hi\"}'")
@click.argument("name")
@click.argument("tool_name_")
@click.argument("arguments", default="{}")
def call(name: str, tool_name_: str, arguments: str) -> None:
    """Run one tool exactly as the MCP server would, to debug a call."""
    target = resolve(name)
    commands = {tool_name(c["path"]): c for c in exposed(introspect(target))}
    if tool_name_ not in commands:
        raise click.UsageError(f"unknown tool {tool_name_!r}; run `mcp-bridge tools {name}`")
    args = json.loads(arguments)
    extra = unknown_arguments(commands[tool_name_], args)
    if extra:
        raise click.UsageError(f"unknown arguments {extra}")
    result = asyncio.run(execute(target, argv(commands[tool_name_], args)))
    click.echo(result.content[0].text)
    if result.is_error:
        raise SystemExit(1)


@cli.command(epilog="\b\nExamples:\n  mcp-bridge serve notion-cli\n  mcp-bridge serve ~/Documents/tg-cli/tg_cli.py")
@click.argument("name")
@click.option("--bundled", is_flag=True, help="The CLI's dependencies are in this interpreter (a packed extension).")
def serve(name: str, bundled: bool) -> None:
    """Run the stdio MCP server for one CLI (what Claude Desktop launches)."""
    asyncio.run(serve_stdio(resolve(name, bundled)))


@cli.command(epilog="\b\nExamples:\n  mcp-bridge pack imessage-cli --version 0.1.0\n  mcp-bridge pack whatsapp-cli-wa --version 0.1.0 --out dist")
@click.argument("name")
@click.option("--version", "version", required=True, help="Extension version (semver).")
@click.option("--out", type=click.Path(file_okay=False, path_type=Path), default=Path("dist"), show_default=True)
@click.option("--wheels", "prebuilt", type=click.Path(exists=True, file_okay=False, path_type=Path),
              help="Prebuilt wheels for mcpb.json build_wheels (all architectures); default builds for this Mac.")
def pack(name: str, version: str, out: Path, prebuilt: Path | None) -> None:
    """Build a Claude Desktop extension (.mcpb) that serves one CLI, installable without a terminal."""
    target = resolve(name)
    click.echo(str(build_bundle(target, version, out / f"{target.name}-{version}.mcpb", prebuilt)))


def build_bundle(target: Cli, version: str, bundle: Path, prebuilt: Path | None = None) -> Path:
    root = stage(target, bundle.parent / "build", version, prebuilt)
    run(["npx", "-y", MCPB, "validate", str(root / "manifest.json")])
    run(["npx", "-y", MCPB, "pack", str(root), str(bundle)])
    return bundle


def github_slug(repo: Path) -> str:
    """`owner/name` of the repo's GitHub origin."""
    remote = run(["git", "-C", str(repo), "remote", "get-url", "origin"]).stdout.strip()
    match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", remote)
    if not match:
        raise click.ClickException(f"{repo.name}: origin {remote!r} is not a GitHub repo")
    return match.group(1)


def tag_commit(repo: Path, tag: str) -> str | None:
    proc = subprocess.run(["git", "-C", str(repo), "rev-parse", "-q", "--verify", f"refs/tags/{tag}^{{commit}}"],
                          capture_output=True, text=True, stdin=subprocess.DEVNULL)
    return proc.stdout.strip() or None


@cli.command(epilog="\b\nExamples:\n  mcp-bridge release tg-cli --version 0.1.1\n"
             "  CODESIGN_IDENTITY=... NOTARY_KEYCHAIN_PROFILE=... mcp-bridge release imessage-cli --version 0.3.1 --yes")
@click.argument("name")
@click.option("--version", "version", required=True, help="Release version (semver); the tag is v<version>.")
@click.option("--out", type=click.Path(file_okay=False, path_type=Path), default=Path("dist"), show_default=True)
@click.option("--wheels", "prebuilt", type=click.Path(exists=True, file_okay=False, path_type=Path),
              help="Prebuilt wheels for mcpb.json build_wheels (all architectures); default builds for this Mac.")
@click.option("--yes", is_flag=True, help="Tag, push and publish. Without it, only pack and show the plan.")
def release(name: str, version: str, out: Path, prebuilt: Path | None, yes: bool) -> None:
    """Pack a CLI as <repo>.mcpb and publish it as the asset of GitHub release v<version>.

    The asset name never changes, so releases/latest/download/<repo>.mcpb always serves
    the newest extension. The tag must not exist yet or already point at HEAD (CI runs
    this on the tag it was triggered by); the tree must be clean so the tag matches the
    packed files. Build steps in mcpb.json inherit the environment, e.g. CODESIGN_IDENTITY.
    """
    target = resolve(name)
    repo, tag = target.repo, f"v{version}"
    if run(["git", "-C", str(repo), "status", "--porcelain"]).stdout.strip():
        raise click.ClickException(f"{repo.name} has uncommitted changes; commit them so {tag} matches the release")
    head = run(["git", "-C", str(repo), "rev-parse", "HEAD"]).stdout.strip()
    tagged = tag_commit(repo, tag)
    if tagged and tagged != head:
        raise click.ClickException(f"{tag} already points at {tagged[:12]}, not HEAD {head[:12]}")
    slug = github_slug(repo)
    bundle = build_bundle(target, version, out / f"{repo.name}.mcpb", prebuilt)
    exists = subprocess.run(["gh", "release", "view", tag, "--repo", slug], capture_output=True,
                            stdin=subprocess.DEVNULL).returncode == 0 if yes else None
    steps = [] if tagged else [["git", "-C", str(repo), "tag", "-a", tag, "-m", tag],
                               ["git", "-C", str(repo), "push", "origin", f"refs/tags/{tag}"]]
    if exists:
        steps.append(["gh", "release", "upload", tag, str(bundle), "--clobber", "--repo", slug])
    else:
        steps.append(["gh", "release", "create", tag, str(bundle), "--repo", slug, "--verify-tag", "--title", tag,
                      "--notes", f"Claude Desktop extension: download {bundle.name}, double-click it, click Install."])
    click.echo(str(bundle))
    if not yes:
        for step in steps:
            click.echo("would run: " + " ".join(step))
        click.echo("dry run: pass --yes to tag and publish")
        return
    for step in steps:
        run(step)
    click.echo(f"https://github.com/{slug}/releases/tag/{tag}")


@cli.command("wheels", epilog="\b\nExamples:\n  mcp-bridge wheels whatsapp-cli-wa --out wheels/")
@click.argument("name")
@click.option("--out", type=click.Path(file_okay=False, path_type=Path), required=True)
def wheels_cmd(name: str, out: Path) -> None:
    """Build, for this Mac's architecture, the wheels a CLI's mcpb.json lists in build_wheels."""
    target = resolve(name)
    overrides_file = target.repo / "mcpb.json"
    needed = json.loads(overrides_file.read_text()).get("build_wheels", []) if overrides_file.is_file() else []
    build_wheels(needed, out)
    for w in sorted(out.glob("*.whl")):
        click.echo(str(w))


@cli.command(epilog="\b\nExamples:\n  mcp-bridge install\n  mcp-bridge install --yes")
@click.option("--config", "config_path", type=click.Path(path_type=Path), default=DESKTOP_CONFIG, show_default=True)
@click.option("--yes", is_flag=True, help="Write the config. Without it, only show the change.")
def install(config_path: Path, yes: bool) -> None:
    """Register one MCP server per CLI in Claude Desktop (restart Desktop afterwards)."""
    clis = discover()
    for c in clis.values():
        try:
            introspect(c)
        except click.ClickException as e:
            log.warning("%s: cannot read its commands, its server will fail to start: %s", c.name, e.message.splitlines()[-1])
    config = json.loads(config_path.read_text()) if config_path.is_file() else {}
    merged, added, stale = merge_config(config, clis)
    click.echo(f"servers: {', '.join(added)}")
    if stale:
        click.echo(f"removed: {', '.join(stale)}")
    if not yes:
        click.echo("dry run: pass --yes to write " + str(config_path))
        return
    if config_path.is_file():
        shutil.copy2(config_path, config_path.with_suffix(".json.bak"))
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(merged, indent=2) + "\n")
    click.echo(f"wrote {config_path}; quit and reopen Claude Desktop to load the servers")


if __name__ == "__main__":
    cli()
