#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Audit Channel imports against direct project dependencies."""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
import json
from pathlib import Path
import sys
import tomllib

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


_MODULE_TO_DISTRIBUTION = {
    "aiohttp": "aiohttp",
    "fastapi": "fastapi",
    "lark_oapi": "lark-oapi",
    "telegram": "python-telegram-bot",
    "discord": "discord-py",
    "slack_bolt": "slack-bolt",
    "twilio": "twilio",
}
_PROJECT_ROOT_NAMES = frozenset({"qwenpaw"})
_STDLIB_NAMES = frozenset(sys.stdlib_module_names)
# Feishu includes a bounded compatibility import and supplies a local shim when
# setuptools no longer exposes pkg_resources.  It is not a required package.
_OPTIONAL_COMPAT_IMPORTS = frozenset({"pkg_resources"})


def direct_dependencies(pyproject: Path) -> frozenset[str]:
    """Read canonical direct dependency names from ``pyproject.toml``."""
    data = tomllib.loads(Path(pyproject).read_text(encoding="utf-8"))
    values = data.get("project", {}).get("dependencies", [])
    return frozenset(
        canonicalize_name(Requirement(value).name) for value in values
    )


def import_roots(source_root: Path) -> frozenset[str]:
    """Return top-level absolute imports under one Channel source tree."""
    names: set[str] = set()
    for path in sorted(Path(source_root).rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(
                    alias.name.split(".", 1)[0] for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                if node.module:
                    names.add(node.module.split(".", 1)[0])
    return frozenset(names)


@dataclass(frozen=True)
class DependencyAudit:
    """Static import/dependency comparison for one Channel."""

    channel_key: str
    imported_modules: tuple[str, ...]
    undeclared_distributions: tuple[str, ...]
    optional_compat_imports: tuple[str, ...] = ()

    def to_mapping(self) -> dict[str, object]:
        """Return the JSON report representation."""
        return {
            "channel_key": self.channel_key,
            "imported_modules": list(self.imported_modules),
            "undeclared_distributions": list(self.undeclared_distributions),
            "optional_compat_imports": list(self.optional_compat_imports),
        }


def audit_channel(
    channel_key: str,
    source_root: Path,
    declared: frozenset[str],
) -> DependencyAudit:
    """Compare absolute imports with declared distributions."""
    imports = import_roots(source_root)
    distributions = {
        canonicalize_name(_MODULE_TO_DISTRIBUTION.get(name, name))
        for name in imports
        if (
            name not in _STDLIB_NAMES
            and name not in _PROJECT_ROOT_NAMES
            and name not in _OPTIONAL_COMPAT_IMPORTS
        )
    }
    undeclared = tuple(sorted(distributions - declared))
    optional_compat_imports = tuple(
        sorted(imports & _OPTIONAL_COMPAT_IMPORTS),
    )
    return DependencyAudit(
        channel_key=channel_key,
        imported_modules=tuple(sorted(imports)),
        undeclared_distributions=undeclared,
        optional_compat_imports=optional_compat_imports,
    )


def main() -> int:
    """Print a JSON audit report for one or more Channel roots."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pyproject", type=Path)
    parser.add_argument(
        "--channel",
        nargs=2,
        action="append",
        required=True,
        metavar=("KEY", "ROOT"),
    )
    args = parser.parse_args()
    declared = direct_dependencies(args.pyproject)
    reports = [
        audit_channel(key, Path(root), declared).to_mapping()
        for key, root in args.channel
    ]
    print(json.dumps(reports, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
