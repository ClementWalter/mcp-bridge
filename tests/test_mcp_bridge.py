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
from click.testing import CliRunner
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


def test_vault_sync_is_hidden_in_extensions():
    assert mb.exposed([command(path=("auth-sync",))], bundled=True) == []


def test_vault_sync_is_served_locally():
    assert mb.exposed([command(path=("auth-sync",))]) == [command(path=("auth-sync",))]


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


def test_unfilled_setting_is_dropped(monkeypatch):
    monkeypatch.setenv("DEMO_KEY", "${user_config.key}")
    mb.drop_unfilled_settings()
    assert "DEMO_KEY" not in os.environ


def test_filled_setting_is_kept(monkeypatch):
    monkeypatch.setenv("DEMO_KEY", "abc")
    mb.drop_unfilled_settings()
    assert os.environ["DEMO_KEY"] == "abc"


def test_introspect_prefers_the_launchers_group_over_a_bigger_sibling(demo, monkeypatch):
    repo = demo / "demo-cli"
    (repo / "sibling.py").write_text(
        "\n".join(["import click", "", "@click.group()", "def cli():", "    pass", ""])
        + "".join("\n".join(["", "@cli.command()", f"def c{i}():", "    pass", ""]) for i in range(5))
    )
    script = repo / "demo_cli.py"
    script.write_text(script.read_text().replace("import click\n", "import click\nimport sibling  # noqa: F401\n", 1))
    monkeypatch.setattr(mb, "CACHE_DIR", demo / "cache")
    commands = mb.introspect(mb.discover(demo / "bin")["demo-cli"])
    assert sorted(c["path"][0] for c in commands) == ["login", "search", "send"]


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


def test_stage_copies_included_sibling_file(demo, monkeypatch):
    repo = demo / "demo-cli"
    (demo / "sibling.py").write_text("X = 1\n")
    for cmd in (["init", "-q"], ["add", "."], ["remote", "add", "origin", "git@github.com:someone/demo-cli.git"]):
        subprocess.run(["git", "-C", str(repo), *cmd], check=True)
    (repo / "mcpb.json").write_text(json.dumps({"include": {"sibling.py": "../sibling.py"}}))
    monkeypatch.setattr(mb, "CACHE_DIR", demo / "cache")
    root = mb.stage(mb.discover(demo / "bin")["demo-cli"], demo / "build", "1.0.0")
    assert (root / "cli" / "demo-cli" / "sibling.py").read_text() == "X = 1\n"


def test_stage_ships_build_outputs(demo, monkeypatch):
    repo = demo / "demo-cli"
    for cmd in (["init", "-q"], ["add", "."], ["remote", "add", "origin", "git@github.com:someone/demo-cli.git"]):
        subprocess.run(["git", "-C", str(repo), *cmd], check=True)
    build = {"command": [sys.executable, "-c", "import pathlib; pathlib.Path('dist').mkdir(); pathlib.Path('dist/app.bin').write_text('built')"],
             "include": ["dist"]}
    (repo / "mcpb.json").write_text(json.dumps({"build": build}))
    monkeypatch.setattr(mb, "CACHE_DIR", demo / "cache")
    root = mb.stage(mb.discover(demo / "bin")["demo-cli"], demo / "build", "1.0.0")
    assert (root / "cli" / "demo-cli" / "dist" / "app.bin").read_text() == "built"


def test_stage_links_the_privacy_policy(staged):
    assert json.loads((staged / "manifest.json").read_text())["privacy_policies"] == [mb.PRIVACY_POLICY]


@pytest.fixture
def two_arch_wheels(tmp_path):
    for arch in ("arm64", "x86_64"):
        (tmp_path / f"signal_protocol-0.2.2-cp311-cp311-macosx_11_0_{arch}.whl").write_bytes(b"")
    return tmp_path


def test_wheel_sources_cover_both_macs(two_arch_wheels):
    out = mb.wheel_sources(["signal-protocol==0.2.2"], two_arch_wheels)
    assert out.count("platform_machine") == 2


def test_wheel_sources_require_a_wheel(tmp_path):
    with pytest.raises(mb.click.ClickException):
        mb.wheel_sources(["signal-protocol==0.2.2"], tmp_path)


def test_build_wheels_uses_a_single_architecture_python(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(mb, "run", calls.append)
    mb.build_wheels(["signal-protocol==0.2.2"], tmp_path)
    assert "--managed-python" in calls[0]


def test_stage_leaves_out_overrides_file(staged):
    assert not (staged / "cli" / "demo-cli" / "mcpb.json").exists()


def stage_with(demo, monkeypatch, overrides: dict) -> Path:
    repo = demo / "demo-cli"
    for cmd in (["init", "-q"], ["add", "."], ["remote", "add", "origin", "git@github.com:someone/demo-cli.git"]):
        subprocess.run(["git", "-C", str(repo), *cmd], check=True)
    (repo / "mcpb.json").write_text(json.dumps(overrides))
    monkeypatch.setattr(mb, "CACHE_DIR", demo / "cache")
    return mb.stage(mb.discover(demo / "bin")["demo-cli"], demo / "build", "1.0.0")


def test_stage_pins_the_overridden_python(demo, monkeypatch):
    root = stage_with(demo, monkeypatch, {"python": "3.12"})
    assert (root / ".python-version").read_text() == "3.12\n"


def test_stage_declares_the_overridden_runtime(demo, monkeypatch):
    root = stage_with(demo, monkeypatch, {"python": "3.12"})
    assert json.loads((root / "manifest.json").read_text())["compatibility"]["runtimes"] == {"python": ">=3.12"}


def test_stage_lists_only_allowed_tools(demo, monkeypatch):
    root = stage_with(demo, monkeypatch, {"tools": ["search"]})
    assert [t["name"] for t in json.loads((root / "manifest.json").read_text())["tools"]] == [mb.GUIDE, "search"]


def test_stage_rejects_a_tool_path_it_does_not_serve(demo, monkeypatch):
    with pytest.raises(mb.click.ClickException, match="'nope'"):
        stage_with(demo, monkeypatch, {"tools": ["nope"]})


def test_bundled_server_serves_only_manifest_tools(demo, monkeypatch):
    root = stage_with(demo, monkeypatch, {"tools": ["search"]})
    env = {**os.environ, "MCP_BRIDGE_CACHE": str(demo / "cache")}
    params = StdioServerParameters(command=sys.executable, cwd=str(root), env=env,
                                   args=[str(root / "mcp_bridge.py"), "serve", "--bundled", "cli/demo-cli/demo_cli.py"])

    async def go():
        async with Client(params) as client:
            return await client.list_tools()
    assert sorted(t.name for t in asyncio.run(go()).tools) == ["search", "usage_guide"]


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


# Logs every call, and answers `release view` as "not found" unless GH_STUB_RELEASE_EXISTS is set.
GH_STUB = "\n".join([
    "import os, sys",
    "with open(os.environ['GH_STUB_LOG'], 'a') as f:",
    "    f.write(' '.join(sys.argv[1:]) + '\\n')",
    "sys.exit(1 if sys.argv[1:3] == ['release', 'view'] and not os.environ.get('GH_STUB_RELEASE_EXISTS') else 0)",
]) + "\n"


@pytest.fixture
def releasable(demo, monkeypatch):
    """The demo CLI committed, origin on GitHub for reads but pushing to a local bare repo, gh stubbed."""
    repo = demo / "demo-cli"
    for key in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{key}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{key}_EMAIL", "test@example.com")
    subprocess.run(["git", "init", "-q", "--bare", str(demo / "origin.git")], check=True)
    for cmd in (["init", "-q"], ["add", "."], ["commit", "-q", "-m", "init"],
                ["remote", "add", "origin", "git@github.com:someone/demo-cli.git"],
                ["remote", "set-url", "--push", "origin", str(demo / "origin.git")]):
        subprocess.run(["git", "-C", str(repo), *cmd], check=True)
    stub_bin = demo / "stub-bin"
    stub_bin.mkdir()
    (stub_bin / "gh").write_text(f"#!{sys.executable}\n" + GH_STUB)
    (stub_bin / "gh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{stub_bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GH_STUB_LOG", str(demo / "gh.log"))
    (demo / "gh.log").write_text("")

    def build_bundle(target, version, bundle, prebuilt=None):
        bundle.parent.mkdir(parents=True, exist_ok=True)
        bundle.write_bytes(b"zip")
        return bundle

    monkeypatch.setattr(mb, "build_bundle", build_bundle)
    return demo


def release(demo, *extra):
    args = ["release", str(demo / "demo-cli" / "demo_cli.py"), "--version", "0.1.1", "--out", str(demo / "dist"), *extra]
    return CliRunner().invoke(mb.cli, args)


def head_of(repo):
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()


def test_release_names_the_asset_after_the_repo(releasable):
    assert release(releasable).output.splitlines()[0] == str(releasable / "dist" / "demo-cli.mcpb")


def test_release_dry_run_creates_no_tag(releasable):
    release(releasable)
    assert mb.tag_commit(releasable / "demo-cli", "v0.1.1") is None


def test_release_dry_run_does_not_call_gh(releasable):
    release(releasable)
    assert (releasable / "gh.log").read_text() == ""


def test_release_tags_head(releasable):
    release(releasable, "--yes")
    assert mb.tag_commit(releasable / "demo-cli", "v0.1.1") == head_of(releasable / "demo-cli")


def test_release_pushes_the_tag(releasable):
    release(releasable, "--yes")
    assert mb.tag_commit(releasable / "origin.git", "v0.1.1") == head_of(releasable / "demo-cli")


def test_release_creates_the_github_release_with_the_asset(releasable):
    release(releasable, "--yes")
    created = (releasable / "gh.log").read_text().splitlines()[-1]
    assert created.startswith(f"release create v0.1.1 {releasable / 'dist' / 'demo-cli.mcpb'} --repo someone/demo-cli")


def test_release_replaces_the_asset_of_an_existing_release(releasable, monkeypatch):
    monkeypatch.setenv("GH_STUB_RELEASE_EXISTS", "1")
    release(releasable, "--yes")
    assert (releasable / "gh.log").read_text().splitlines()[-1] == (
        f"release upload v0.1.1 {releasable / 'dist' / 'demo-cli.mcpb'} --clobber --repo someone/demo-cli")


def test_release_reuses_a_tag_at_head(releasable):
    subprocess.run(["git", "-C", str(releasable / "demo-cli"), "tag", "-a", "v0.1.1", "-m", "v0.1.1"], check=True)
    assert release(releasable, "--yes").exit_code == 0


def test_release_refuses_a_tag_elsewhere(releasable):
    repo = releasable / "demo-cli"
    subprocess.run(["git", "-C", str(repo), "tag", "-a", "v0.1.1", "-m", "v0.1.1"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "next"], check=True)
    assert "already points at" in release(releasable, "--yes").output


def test_release_refuses_a_dirty_tree(releasable):
    (releasable / "demo-cli" / "demo_cli.py").write_text(DEMO + "# edited\n")
    assert "uncommitted changes" in release(releasable, "--yes").output


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        ("git@github.com:someone/demo-cli.git", "someone/demo-cli"),
        ("https://github.com/someone/demo-cli", "someone/demo-cli"),
        ("https://github.com/someone/demo-cli.git", "someone/demo-cli"),
    ],
)
def test_github_slug(demo, remote, expected):
    repo = demo / "demo-cli"
    for cmd in (["init", "-q"], ["remote", "add", "origin", remote]):
        subprocess.run(["git", "-C", str(repo), *cmd], check=True)
    assert mb.github_slug(repo) == expected


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
