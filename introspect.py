"""Dump a click CLI's command tree as JSON, run with the target CLI's own interpreter.

Usage: python introspect.py <launcher> <out.json>. The launcher is loaded as a module (not as
__main__, so it does not run), then every click group defined in a module under
the launcher's repo is collected and the root one is walked. Needs only the
stdlib and the click the target already depends on.
"""

import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path

import click


def load(launcher: Path) -> None:
    """Import the launcher; extensionless shims need an explicit source loader."""
    sys.path.insert(0, str(launcher.parent))
    loader = importlib.machinery.SourceFileLoader("_mcp_bridge_target", str(launcher))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    # Registered first so dataclasses and pickling can resolve the module by name.
    sys.modules[loader.name] = module
    loader.exec_module(module)


def root_group(repo: Path) -> click.Group:
    """The repo's group that no other group lists as a subcommand, largest first."""
    groups = {}
    for module in list(sys.modules.values()):
        path = getattr(module, "__file__", None)
        if not path or not Path(path).resolve().is_relative_to(repo):
            continue
        for value in vars(module).values():
            if isinstance(value, click.Group):
                groups[id(value)] = value
    children = {id(sub) for g in groups.values() for sub in g.commands.values()}
    roots = [g for k, g in groups.items() if k not in children]
    if not roots:
        raise SystemExit(f"no click group found under {repo}")
    return max(roots, key=lambda g: len(g.commands))


def jsonable(value):
    try:
        json.dumps(value)
    except TypeError:
        return None
    return value


def param(p: click.Parameter) -> dict:
    kind = p.type
    return {
        "name": p.name,
        "kind": "argument" if isinstance(p, click.Argument) else "option",
        "type": kind.name if not isinstance(kind, click.Choice) else "choice",
        "choices": [str(c) for c in kind.choices] if isinstance(kind, click.Choice) else None,
        "required": bool(p.required),
        "multiple": bool(getattr(p, "multiple", False)) or p.nargs == -1,
        "nargs": p.nargs,
        "default": jsonable(p.default) if not callable(p.default) else None,
        "help": getattr(p, "help", None),
        "opts": list(p.opts),
        "secondary_opts": list(p.secondary_opts),
        "is_flag": bool(getattr(p, "is_flag", False)),
        "count": bool(getattr(p, "count", False)),
        "hidden": bool(getattr(p, "hidden", False)),
        "prompt": bool(getattr(p, "prompt", None)),
    }


def walk(cmd: click.Command, path: list[str], group_json: list[int]):
    spellings = {o for p in cmd.params for o in p.opts}
    if isinstance(cmd, click.Group):
        # A --json flag on a group must be passed before the subcommand.
        here = group_json + [len(path)] if "--json" in spellings else group_json
        for name, sub in cmd.commands.items():
            yield from walk(sub, path + [name], here)
        return
    yield {
        "path": path,
        "help": cmd.help or "",
        "epilog": cmd.epilog or "",
        "hidden": bool(cmd.hidden),
        "params": [param(p) for p in cmd.params],
        "json_at": group_json,
    }


def main() -> None:
    launcher = Path(sys.argv[1]).resolve()
    repo = launcher.parent.parent if launcher.parent.name == "bin" else launcher.parent
    load(launcher)
    commands = list(walk(root_group(repo), [], []))
    # A file, not stdout: the target may print while it is imported.
    Path(sys.argv[2]).write_text(json.dumps({"commands": commands}))


if __name__ == "__main__":
    main()
