#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "click==8.4.1",
#     "mcp==2.3.0",
#     "pyyaml==6.0.3",
#     "pytest==9.1.1",
# ]
#
# [tool.uv]
# exclude-newer = "2026-10-27T00:00:00Z"
# ///
"""Tests for mcp_bridge: schema and argv mapping offline, then a demo CLI served over stdio."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mcp_bridge as mb  # noqa: E402

# Built line by line so uv does not read the fixture's header as this file's own.
DEMO = "\n".join([
    "#!/usr/bin/env -S uv run --script",
    "# " + "/// script",
    '# requires-python = ">=3.11"',
    '# dependencies = ["click==8.4.1"]',
    "# " + "///",
    '"""Demo CLI."""',
    "import json",
    "import click",
    "",
    "@click.group()",
    "def cli():",
    '    """Demo."""',
    "",
    '@cli.command(epilog="Examples: demo search cats")',
    '@click.option("--json", "as_json", is_flag=True)',
    '@click.option("--limit", type=int, default=3, help="Max hits.")',
    '@click.argument("query")',
    "def search(as_json, limit, query):",
    '    """Search things."""',
    '    click.echo(json.dumps({"query": query, "limit": limit, "json": as_json}))',
    "",
    "@cli.command()",
    '@click.argument("peer")',
    '@click.argument("text")',
    '@click.option("--yes", is_flag=True)',
    "def send(peer, text, yes):",
    '    """Send a message."""',
    '    click.echo(("sent " if yes else "would send ") + f"{text} to {peer}")',
    "",
    "@cli.command()",
    "def login():",
    '    """Interactive login."""',
    "",
    'if __name__ == "__main__":',
    "    cli()",
]) + "\n"


def option(**kw):
    base = {"name": "x", "kind": "option", "type": "text", "choices": None, "required": False, "multiple": False,
            "nargs": 1, "default": None, "help": None, "opts": ["--x"], "secondary_opts": [], "is_flag": False,
            "count": False, "hidden": False, "prompt": False}
    return {**base, **kw}


def command(*params, path=("demo",), json_at=()):
    return {"path": list(path), "help": "", "epilog": "", "hidden": False, "params": list(params), "json_at": list(json_at)}


YES = option(name="yes", opts=["--yes"], is_flag=True)
JSON = option(name="as_json", opts=["--json"], is_flag=True)


@pytest.fixture
def demo(tmp_path):
    repo = tmp_path / "demo-cli"
    repo.mkdir()
    (repo / "demo_cli.py").write_text(DEMO)
    (repo / "SKILL.md").write_text("---\nname: demo-cli\ndescription: >\n  Demo CLI\n  for tests.\n---\n# Demo\n\nUse search.\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    os.symlink(repo / "demo_cli.py", bin_dir / "demo")
    return tmp_path


def serve_params(demo):
    env = {**os.environ, "MCP_BRIDGE_BIN": str(demo / "bin"), "MCP_BRIDGE_CACHE": str(demo / "cache")}
    return StdioServerParameters(command="uv", args=["run", "-q", "--script", str(mb.BRIDGE), "serve", "demo-cli"], env=env)


def with_client(demo, fn):
    async def go():
        async with Client(serve_params(demo)) as client:
            return await fn(client)
    return asyncio.run(go())


@pytest.mark.parametrize(
    ("param", "expected"),
    [
        (option(type="integer"), {"type": "integer"}),
        (option(type="integer range"), {"type": "integer"}),
        (option(type="float"), {"type": "number"}),
        (option(is_flag=True, type="boolean"), {"type": "boolean"}),
        (option(type="choice", choices=["a", "b"]), {"type": "string", "enum": ["a", "b"]}),
        (option(multiple=True), {"type": "array", "items": {"type": "string"}}),
        (option(nargs=2), {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 2}),
        (option(count=True), {"type": "integer", "minimum": 0}),
        (option(type="path"), {"type": "string"}),
    ],
)
def test_json_type(param, expected):
    assert mb.json_type(param) == expected


def test_reserved_flags_are_not_inputs():
    assert mb.input_schema(command(YES, JSON))["properties"] == {
        "confirm": mb.input_schema(command(YES))["properties"]["confirm"]
    }


def test_required_argument_is_required():
    assert mb.input_schema(command(option(name="q", kind="argument", opts=["q"], required=True)))["required"] == ["q"]


def test_argv_puts_options_before_positionals():
    cmd = command(option(name="limit", opts=["-l", "--limit"]), option(name="q", kind="argument", opts=["q"]))
    assert mb.argv(cmd, {"q": "-x", "limit": 5}) == ["demo", "--limit", "5", "--", "-x"]


def test_argv_adds_json_when_accepted():
    assert mb.argv(command(JSON), {}) == ["demo", "--json"]


def test_argv_puts_group_json_before_subcommand():
    assert mb.argv(command(path=("cloud", "pull"), json_at=[0]), {}) == ["--json", "cloud", "pull"]


def test_argv_passes_yes_only_on_confirm():
    assert mb.argv(command(YES), {"confirm": False}) == ["demo"]


def test_argv_confirm_adds_yes():
    assert mb.argv(command(YES), {"confirm": True}) == ["demo", "--yes"]


def test_argv_false_flag_uses_secondary_spelling():
    cmd = command(option(name="names", opts=["--names"], secondary_opts=["--no-names"], is_flag=True))
    assert mb.argv(cmd, {"names": False}) == ["demo", "--no-names"]


def test_argv_repeats_multiple_option():
    assert mb.argv(command(option(name="tag", opts=["--tag"], multiple=True)), {"tag": ["a", "b"]}) == [
        "demo", "--tag", "a", "--tag", "b"]


def test_unknown_arguments_are_reported():
    assert mb.unknown_arguments(command(), {"nope": 1}) == ["nope"]


def test_confirm_is_unknown_on_a_read():
    assert mb.unknown_arguments(command(), {"confirm": True}) == ["confirm"]


@pytest.mark.parametrize(
    ("repo", "cmd", "expected"),
    [
        ("slack-user-cli", command(path=("send",)), "outward"),
        ("slack-user-cli", command(path=("download",)), "local"),
        ("slack-user-cli", command(path=("read",)), "read"),
        ("tg-cli", command(YES, path=("send",)), "dry-run"),
    ],
)
def test_access(repo, cmd, expected):
    assert mb.access(repo, cmd) == expected


def test_outward_write_is_destructive():
    assert mb.tool("slack-user-cli", command(path=("send",))).annotations.destructive_hint is True


def test_read_is_read_only():
    assert mb.tool("slack-user-cli", command(path=("read",))).annotations.read_only_hint is True


def test_interactive_commands_are_not_exposed():
    assert mb.exposed([command(path=("login",)), command(path=("auth", "save")), command(path=("read",))]) == [
        command(path=("read",))]


def test_tool_name_is_mcp_safe():
    assert mb.tool_name(["cloud", "chatgpt-pull"]) == "cloud_chatgpt_pull"


def test_discover_finds_linked_cli(demo):
    assert list(mb.discover(demo / "bin")) == ["demo-cli"]


def test_discover_ignores_repo_without_skill(demo):
    (demo / "demo-cli" / "SKILL.md").unlink()
    assert mb.discover(demo / "bin") == {}


def test_merge_config_keeps_other_servers(demo):
    clis = mb.discover(demo / "bin")
    merged, _, _ = mb.merge_config({"mcpServers": {"hubspot": {"command": "npx"}}}, clis)
    assert set(merged["mcpServers"]) == {"hubspot", "demo-cli"}


def test_merge_config_removes_stale_bridge_server(demo):
    stale = {"command": "uv", "args": ["run", "--script", str(mb.BRIDGE), "serve", "gone-cli"]}
    _, _, removed = mb.merge_config({"mcpServers": {"gone-cli": stale}}, mb.discover(demo / "bin"))
    assert removed == ["gone-cli"]


def test_merge_config_refuses_to_overwrite_foreign_server(demo):
    with pytest.raises(mb.click.ClickException):
        mb.merge_config({"mcpServers": {"demo-cli": {"command": "npx"}}}, mb.discover(demo / "bin"))


def test_skill_reads_description_that_is_not_valid_yaml(demo):
    (demo / "demo-cli" / "SKILL.md").write_text("---\nname: demo-cli\ndescription: Two models: a, b\n---\nBody\n")
    assert mb.skill(mb.discover(demo / "bin")["demo-cli"])[0]["description"] == "Two models: a, b"


def test_render_marks_failure_as_error():
    assert mb.render(1, "", "boom").is_error is True


def test_render_truncates_long_output():
    assert len(mb.render(0, "x" * (mb.MAX_OUTPUT + 10), "").content[0].text) < mb.MAX_OUTPUT + 200


@pytest.mark.parametrize(
    ("requirement", "expected"),
    [
        ("click==8.4.1", "click"),
        ("qrcode[pil]==8.2", "qrcode"),
        ("pyobjc-framework-Contacts==12.1; sys_platform == 'darwin'", "pyobjc-framework-contacts"),
        ("Signal_Protocol==0.2.2", "signal-protocol"),
    ],
)
def test_dist_name(requirement, expected):
    assert mb.dist_name(requirement) == expected


def test_script_header_reads_dependencies(demo):
    assert mb.script_header(demo / "demo-cli" / "demo_cli.py")["dependencies"] == ["click==8.4.1"]


def test_merged_dependencies_keep_the_cli_pin(demo):
    script = demo / "demo-cli" / "demo_cli.py"
    script.write_text(script.read_text().replace("click==8.4.1", "click==8.3.3"))
    deps, _ = mb.merged_dependencies(mb.discover(demo / "bin")["demo-cli"])
    assert [d for d in deps if mb.dist_name(d) == "click"] == ["click==8.3.3"]


def test_merged_dependencies_add_the_bridge_runtime(demo):
    deps, _ = mb.merged_dependencies(mb.discover(demo / "bin")["demo-cli"])
    assert {"mcp", "pyyaml"} <= {mb.dist_name(d) for d in deps}


def test_bundled_cli_runs_with_this_interpreter(demo):
    assert mb.resolve(str(demo / "demo-cli" / "demo_cli.py"), bundled=True).python == sys.executable


def test_bundled_launch_skips_uv(demo):
    cli = mb.resolve(str(demo / "demo-cli" / "demo_cli.py"), bundled=True)
    assert mb.launch(cli) == [sys.executable, str(cli.launcher)]


@pytest.fixture
def staged(demo, monkeypatch):
    repo = demo / "demo-cli"
    for cmd in (["init", "-q"], ["add", "."], ["remote", "add", "origin", "git@github.com:someone/demo-cli.git"]):
        subprocess.run(["git", "-C", str(repo), *cmd], check=True)
    (repo / "mcpb.json").write_text(json.dumps({"display_name": "Demo", "env": {"DEMO_KEY": "${user_config.key}"}}))
    subprocess.run(["git", "-C", str(repo), "add", "mcpb.json"], check=True)
    monkeypatch.setattr(mb, "CACHE_DIR", demo / "cache")
    return mb.stage(mb.discover(demo / "bin")["demo-cli"], demo / "build", "1.2.3")


def test_stage_serves_the_cli_bundled(staged):
    args = json.loads((staged / "manifest.json").read_text())["server"]["mcp_config"]["args"]
    assert args[-3:] == ["serve", "--bundled", "cli/demo-cli/demo_cli.py"]


def test_stage_applies_overrides(staged):
    assert json.loads((staged / "manifest.json").read_text())["display_name"] == "Demo"


def test_stage_maps_settings_to_env(staged):
    env = json.loads((staged / "manifest.json").read_text())["server"]["mcp_config"]["env"]
    assert env == {"DEMO_KEY": "${user_config.key}"}


def test_stage_pins_python(staged):
    assert (staged / ".python-version").read_text() == mb.BUNDLE_PYTHON + "\n"


def test_stage_locks_dependencies(staged):
    assert (staged / "uv.lock").is_file()


def test_stage_leaves_out_overrides_file(staged):
    assert not (staged / "cli" / "demo-cli" / "mcpb.json").exists()


def test_e2e_lists_tools(demo):
    tools = with_client(demo, lambda c: c.list_tools())
    assert sorted(t.name for t in tools.tools) == ["search", "send", "usage_guide"]


def test_e2e_instructions_hold_the_skill_description(demo):
    async def instructions(client):
        return client.instructions
    assert with_client(demo, instructions).startswith("Demo CLI for tests.\n")


def test_e2e_usage_guide_returns_skill_body(demo):
    result = with_client(demo, lambda c: c.call_tool("usage_guide", {}))
    assert result.content[0].text == "# Demo\n\nUse search."


def test_e2e_read_passes_json_and_options(demo):
    result = with_client(demo, lambda c: c.call_tool("search", {"query": "cats", "limit": 7}))
    assert json.loads(result.content[0].text) == {"query": "cats", "limit": 7, "json": True}


def test_e2e_write_is_dry_run_by_default(demo):
    result = with_client(demo, lambda c: c.call_tool("send", {"peer": "bob", "text": "hi"}))
    assert result.content[0].text == "would send hi to bob\n"


def test_e2e_write_with_confirm_runs(demo):
    result = with_client(demo, lambda c: c.call_tool("send", {"peer": "bob", "text": "hi", "confirm": True}))
    assert result.content[0].text == "sent hi to bob\n"


def test_e2e_unknown_argument_is_an_error(demo):
    result = with_client(demo, lambda c: c.call_tool("search", {"query": "x", "bogus": 1}))
    assert result.is_error is True


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
