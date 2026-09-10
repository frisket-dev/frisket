#!/usr/bin/env python3
"""Check declarative Python import boundaries against a source tree."""

from __future__ import annotations

import argparse
import ast
import fnmatch
import importlib.util
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence


@dataclass(frozen=True)
class ImportBoundaryRule:
    """A set of governed modules and the imports they may make.

    Two rule shapes, distinguished by which field is populated:

    * ALLOW-list (``allowed_imports``): the governed modules may import ONLY
      what is listed. Used for the leaf modules that must stay dependency-free
      (the neutral endpoint catalog, the hosted route policy).
    * DENY-list (``forbidden_imports``, and no ``allowed_imports``): the
      governed modules may import anything EXCEPT what is listed. This is how a
      single rule can govern the whole open tree — enumerating everything the
      open backend is allowed to import would be a losing game, while the thing
      it may never touch (the private package) is one line.

    A rule carrying ``forbidden_imports`` and no ``allowed_imports`` is
    therefore evaluated as a deny-list, NOT as "allow nothing".

    ``exclude`` subtracts modules from the governed set, so a rule that governs
    ``frisket.*`` can exempt the private tree from being flagged for importing
    itself.
    """

    id: str
    modules: tuple[str, ...]
    allowed_imports: tuple[str, ...] = ()
    forbidden_imports: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    allow_stdlib: bool = False
    reject_unresolved_dynamic: bool = True

    @property
    def is_deny_list(self) -> bool:
        return bool(self.forbidden_imports) and not self.allowed_imports


@dataclass(frozen=True, order=True)
class ImportViolation:
    rule_id: str
    path: str
    line: int
    reason: str
    imported: str
    expression: str

    def __str__(self) -> str:
        location = f"{self.path}:{self.line}" if self.line else self.path
        return (
            f"{self.rule_id}: {location}: {self.reason}: "
            f"{self.imported} ({self.expression})"
        )


@dataclass(frozen=True)
class _Module:
    name: str
    package: str
    path: Path
    relative_path: str
    source: str
    tree: ast.Module


@dataclass(frozen=True)
class _ImportReference:
    imported: str | None
    line: int
    expression: str
    unresolved_reason: str = "unresolved import"


def _module_name(root: Path, path: Path) -> tuple[str, bool]:
    parts = list(path.relative_to(root).with_suffix("").parts)
    is_package = parts[-1] == "__init__"
    if is_package:
        parts.pop()
    return ".".join(parts), is_package


def _discover_modules(root: Path) -> dict[str, _Module]:
    modules: dict[str, _Module] = {}
    for path in sorted(root.rglob("*.py")):
        name, is_package = _module_name(root, path)
        if not name:
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        package = name if is_package else name.rpartition(".")[0]
        modules[name] = _Module(
            name=name,
            package=package,
            path=path,
            relative_path=path.relative_to(root).as_posix(),
            source=source,
            tree=tree,
        )
    return modules


def _matches(module: str, patterns: tuple[str, ...]) -> bool:
    return any(
        fnmatch.fnmatchcase(module, pattern)
        or ("*" not in pattern and module.startswith(f"{pattern}."))
        for pattern in patterns
    )


def _known_module(candidate: str, modules: dict[str, _Module]) -> bool:
    return candidate in modules or any(
        name.startswith(f"{candidate}.") for name in modules
    )


def _resolve_base(module: _Module, node: ast.ImportFrom) -> str | None:
    if not node.level:
        return node.module
    if not module.package:
        return None
    relative = "." * node.level + (node.module or "")
    try:
        return importlib.util.resolve_name(relative, module.package)
    except (ImportError, ValueError):
        return None


def _static_imports(
    module: _Module, modules: dict[str, _Module]
) -> list[_ImportReference]:
    references: list[_ImportReference] = []
    for node in ast.walk(module.tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                expression = f"import {alias.name}"
                if alias.asname:
                    expression += f" as {alias.asname}"
                references.append(_ImportReference(alias.name, node.lineno, expression))
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_base(module, node)
            prefix = "." * node.level + (node.module or "")
            for alias in node.names:
                expression = f"from {prefix} import {alias.name}"
                if alias.asname:
                    expression += f" as {alias.asname}"
                imported = base
                if base and alias.name != "*":
                    candidate = f"{base}.{alias.name}"
                    if node.module is None or _known_module(candidate, modules):
                        imported = candidate
                references.append(
                    _ImportReference(
                        imported,
                        node.lineno,
                        expression,
                        "unresolved relative import",
                    )
                )
    return references


def _dynamic_loader_names(tree: ast.Module) -> tuple[set[str], set[str]]:
    importlib_aliases: set[str] = set()
    loader_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "importlib":
                    importlib_aliases.add(alias.asname or "importlib")
        elif isinstance(node, ast.ImportFrom) and node.module == "importlib":
            for alias in node.names:
                if alias.name == "import_module":
                    loader_names.add(alias.asname or alias.name)
    return importlib_aliases, loader_names


def _dynamic_loader(
    function: ast.expr, importlib_aliases: set[str], loader_names: set[str]
) -> str | None:
    if isinstance(function, ast.Name):
        if function.id == "__import__":
            return "builtin"
        if function.id in loader_names:
            return "importlib"
        return None
    if (
        isinstance(function, ast.Attribute)
        and function.attr == "import_module"
        and isinstance(function.value, ast.Name)
        and function.value.id in importlib_aliases
    ):
        return "importlib"
    return None


def _literal_package(call: ast.Call, module: _Module) -> str | None:
    package_node: ast.expr | None = call.args[1] if len(call.args) > 1 else None
    for keyword in call.keywords:
        if keyword.arg == "package":
            package_node = keyword.value
    if isinstance(package_node, ast.Constant) and isinstance(package_node.value, str):
        return package_node.value
    if isinstance(package_node, ast.Name) and package_node.id == "__package__":
        return module.package
    return None


def _dynamic_imports(module: _Module) -> list[_ImportReference]:
    importlib_aliases, loader_names = _dynamic_loader_names(module.tree)
    references: list[_ImportReference] = []
    for node in ast.walk(module.tree):
        if not isinstance(node, ast.Call):
            continue
        loader = _dynamic_loader(node.func, importlib_aliases, loader_names)
        if loader is None:
            continue
        expression = ast.get_source_segment(module.source, node) or ast.unparse(node)
        if not node.args or not (
            isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and node.args[0].value
        ):
            references.append(
                _ImportReference(
                    None, node.lineno, expression, "unresolved dynamic import"
                )
            )
            continue
        imported = node.args[0].value
        if loader == "builtin":
            level_node: ast.expr | None = node.args[4] if len(node.args) > 4 else None
            for keyword in node.keywords:
                if keyword.arg == "level":
                    level_node = keyword.value
            level = 0 if level_node is None else None
            if isinstance(level_node, ast.Constant) and isinstance(
                level_node.value, int
            ):
                level = level_node.value
            if level is None or level < 0 or (level and not module.package):
                references.append(
                    _ImportReference(
                        None,
                        node.lineno,
                        expression,
                        "unresolved dynamic import",
                    )
                )
                continue
            if level:
                try:
                    imported = importlib.util.resolve_name(
                        "." * level + imported, module.package
                    )
                except (ImportError, ValueError):
                    references.append(
                        _ImportReference(
                            None,
                            node.lineno,
                            expression,
                            "unresolved dynamic import",
                        )
                    )
                    continue
        elif imported.startswith("."):
            package = _literal_package(node, module)
            if not package:
                references.append(
                    _ImportReference(
                        None,
                        node.lineno,
                        expression,
                        "unresolved dynamic import",
                    )
                )
                continue
            try:
                imported = importlib.util.resolve_name(imported, package)
            except (ImportError, ValueError):
                references.append(
                    _ImportReference(
                        None,
                        node.lineno,
                        expression,
                        "unresolved dynamic import",
                    )
                )
                continue
        references.append(_ImportReference(imported, node.lineno, expression))
    return references


def _is_stdlib(module: str) -> bool:
    root = module.partition(".")[0]
    return root in sys.stdlib_module_names or root in sys.builtin_module_names


def check_import_boundaries(
    source_root: Path, rules: Sequence[ImportBoundaryRule]
) -> tuple[ImportViolation, ...]:
    """Return every violation; an empty tuple means all rules pass."""

    source_root = source_root.resolve()
    modules = _discover_modules(source_root)
    violations: list[ImportViolation] = []
    for rule in rules:
        governed: dict[str, _Module] = {}
        for pattern in rule.modules:
            matches = {
                name: module
                for name, module in modules.items()
                if fnmatch.fnmatchcase(name, pattern)
            }
            if not matches:
                violations.append(
                    ImportViolation(
                        rule.id,
                        "<source-root>",
                        0,
                        "governed module missing",
                        pattern,
                        "module selection",
                    )
                )
            governed.update(matches)

        if rule.exclude:
            governed = {
                name: module
                for name, module in governed.items()
                if not any(
                    fnmatch.fnmatchcase(name, pattern) for pattern in rule.exclude
                )
            }

        for module in governed.values():
            references = _static_imports(module, modules) + _dynamic_imports(module)
            for reference in references:
                if reference.imported is None:
                    if (
                        rule.reject_unresolved_dynamic
                        or not reference.unresolved_reason.startswith(
                            "unresolved dynamic"
                        )
                    ):
                        violations.append(
                            ImportViolation(
                                rule.id,
                                module.relative_path,
                                reference.line,
                                reference.unresolved_reason,
                                "<dynamic>"
                                if "dynamic" in reference.unresolved_reason
                                else "<unresolved>",
                                reference.expression,
                            )
                        )
                    continue
                if rule.is_deny_list:
                    if not _matches(reference.imported, rule.forbidden_imports):
                        continue
                    violations.append(
                        ImportViolation(
                            rule.id,
                            module.relative_path,
                            reference.line,
                            "forbidden import",
                            reference.imported,
                            reference.expression,
                        )
                    )
                    continue
                if _matches(reference.imported, rule.allowed_imports):
                    continue
                if (
                    rule.allow_stdlib
                    and not _known_module(reference.imported, modules)
                    and _is_stdlib(reference.imported)
                ):
                    continue
                violations.append(
                    ImportViolation(
                        rule.id,
                        module.relative_path,
                        reference.line,
                        "forbidden import",
                        reference.imported,
                        reference.expression,
                    )
                )
    return tuple(sorted(set(violations)))


def _rules_from_json(path: Path) -> tuple[ImportBoundaryRule, ...]:
    payload: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("rules"), list):
        raise ValueError("boundary config must contain a rules array")
    return tuple(
        ImportBoundaryRule(
            id=str(item["id"]),
            modules=tuple(item["modules"]),
            allowed_imports=tuple(item.get("allowed_imports", ())),
            forbidden_imports=tuple(item.get("forbidden_imports", ())),
            exclude=tuple(item.get("exclude", ())),
            allow_stdlib=bool(item.get("allow_stdlib", False)),
            reject_unresolved_dynamic=bool(item.get("reject_unresolved_dynamic", True)),
        )
        for item in payload["rules"]
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args(argv)
    violations = check_import_boundaries(
        args.source_root, _rules_from_json(args.config)
    )
    for violation in violations:
        print(violation)
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
