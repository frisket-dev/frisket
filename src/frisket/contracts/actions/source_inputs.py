"""One typed source contract shared by action catalogs and runtimes."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, Protocol


def _ordered(values: Any, *, name: str) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        raise ValueError(f"{name} must be a sequence of values, not a string")
    if isinstance(values, (set, frozenset)):
        raise ValueError(f"{name} must be ordered, not a set")
    result = tuple(str(value) for value in values)
    if any(not value for value in result):
        raise ValueError(f"{name} values must be non-empty")
    if len(result) != len(set(result)):
        raise ValueError(f"{name} values must be unique")
    return result


def _value(params: Any, name: str, default: Any = None) -> Any:
    return (
        params.get(name, default)
        if isinstance(params, Mapping)
        else getattr(params, name, default)
    )


class InputSelectionError(ValueError):
    """A descriptor could not select one valid source shape."""

    def __init__(self, message: str, *, field: str, source: str | None = None) -> None:
        super().__init__(message)
        self.field = field
        self.source = source


@dataclass(frozen=True)
class RequiredAiInput:
    """One selected source that must be backed by an AI-generated column."""

    column_name: str
    error_field: str


@dataclass(frozen=True)
class SelectedSource:
    """A selected source tree retaining each binding's contract and order."""

    source: SourceInput
    columns: tuple[str, ...] = ()
    canonical_params: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({})
    )
    children: tuple[tuple[str, SelectedSource], ...] = ()
    required_ai_inputs: tuple[RequiredAiInput, ...] = ()

    def leaves(self) -> tuple[tuple[str | None, SelectedSource], ...]:
        if not self.children:
            return ((None, self),)
        leaves: list[tuple[str | None, SelectedSource]] = []
        for binding, child in self.children:
            leaves.extend((binding or nested, leaf) for nested, leaf in child.leaves())
        return tuple(leaves)

    def names(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(name for _, leaf in self.leaves() for name in leaf.columns)
        )

    def params(self) -> Mapping[str, Any]:
        canonical = dict(self.canonical_params)
        for _, child in self.children:
            canonical.update(child.params())
        return MappingProxyType(canonical)

    def field(self) -> str:
        leaves = self.leaves()
        return leaves[0][1].source.error_field if len(leaves) == 1 else "params"


class SourceInputDescriptor(Protocol):
    """Neutral source contract consumed by catalogs and action validation."""

    def select(self, params: Any) -> SelectedSource: ...

    def catalog_requirements(self) -> list[dict[str, Any]]: ...

    def owned_params(self) -> frozenset[str]: ...


def _validate_cardinality(minimum: int, maximum: int | None) -> None:
    if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 0:
        raise ValueError("source min must be a non-negative integer")
    if maximum is not None and (
        isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < minimum
    ):
        raise ValueError("source max must be an integer greater than or equal to min")


def _check_cardinality(names: tuple[str, ...], source: SourceInput) -> None:
    if len(names) < source.min or (source.max is not None and len(names) > source.max):
        raise InputSelectionError(
            "source column cardinality is invalid", field=source.error_field
        )


def _explicit_columns(params: Any, param: str) -> tuple[str, ...]:
    raw = _value(params, param, ()) or ()
    if isinstance(raw, str):
        raise InputSelectionError(
            "source columns must be a list", field=f"params.{param}"
        )
    names = tuple(str(value) for value in raw)
    if any(not name for name in names) or len(names) != len(set(names)):
        raise InputSelectionError(
            "source column names must be non-empty and unique", field=f"params.{param}"
        )
    return names


_TemplateColumns = Literal["exact", "union"]


@dataclass(frozen=True)
class SourceInput:
    """A normalized source node interpreted identically for runtime and catalog."""

    mode: str
    param: str | None = None
    template_param: str | None = None
    template_columns: _TemplateColumns = "union"
    min: int = 1
    max: int | None = None
    accepted_types: tuple[str, ...] | None = None
    id: str | None = None
    label: str | None = None
    cell_kinds: tuple[str, ...] = ()
    message: str | None = None
    next_steps: tuple[str, ...] = ()
    includes_ai: bool = False
    error_field: str = "params"
    owned: frozenset[str] = frozenset()
    required_ai_params: tuple[str, ...] = ()
    children: tuple[tuple[str, SourceInput], ...] = ()
    distinct: bool = False
    group: str | None = None
    default: str | None = None
    sheet_param: str | None = None
    resolver: Callable[[Any], Any] | None = field(default=None, repr=False)

    def active(self, params: Any) -> bool:
        if self.mode == "fixed_column" or self.resolver is not None:
            return True
        if self.mode == "column":
            assert self.param is not None
            return bool(str(_value(params, self.param, "") or "").strip())
        if self.mode in {"columns", "template"}:
            return bool(
                (self.param != self.template_param and _value(params, self.param, ()))
                or (
                    self.template_param
                    and str(_value(params, self.template_param, "") or "").strip()
                )
                or any(_value(params, name) for name in self.required_ai_params)
            )
        if self.mode in {"fields", "all"}:
            return all(child.active(params) for _, child in self.children)
        return len(self._active_children(params)) == 1

    def select(self, params: Any) -> SelectedSource:
        if self.mode == "fixed_column":
            assert self.id is not None
            return self._selection((self.id,), params)
        if self.resolver is not None:
            names = tuple(str(name) for name in self.resolver(params))
            _check_cardinality(names, self)
            return self._selection(names, params)
        if self.mode == "column":
            assert self.param is not None
            raw = _value(params, self.param)
            names = (str(raw),) if raw else ()
            _check_cardinality(names, self)
            return self._selection(names, params)
        if self.mode in {"columns", "template"}:
            return self._select_columns(params)
        if self.mode in {"fields", "all"}:
            return self._select_group(params)
        return self._active_children(params)[0].select(params)

    def _selection(
        self,
        names: tuple[str, ...],
        params: Any,
        canonical: Mapping[str, Any] | None = None,
    ) -> SelectedSource:
        required: list[RequiredAiInput] = []
        for param in self.required_ai_params:
            name = str(_value(params, param, "") or "").strip()
            if not name:
                raise InputSelectionError(
                    "required AI source must be non-empty", field=f"params.{param}"
                )
            required.append(RequiredAiInput(name, f"params.{param}"))
        return SelectedSource(
            source=self,
            columns=names,
            canonical_params=MappingProxyType(dict(canonical or {})),
            required_ai_inputs=tuple(required),
        )

    def _select_columns(self, params: Any) -> SelectedSource:
        columns_param = self.param if self.param != self.template_param else None
        explicit = _explicit_columns(params, columns_param) if columns_param else ()
        template = (
            str(_value(params, self.template_param, "") or "").strip()
            if self.template_param
            else ""
        )
        if self.mode == "template" and not template:
            assert self.template_param is not None
            raise InputSelectionError(
                "source template must be non-empty",
                field=f"params.{self.template_param}",
            )
        names = explicit
        if template:
            from frisket.authoring.templates import column_template_names

            referenced = tuple(column_template_names(template))
            if self.template_columns == "exact":
                if explicit and set(explicit) != set(referenced):
                    raise InputSelectionError(
                        "template columns do not match its references",
                        field=self.error_field,
                    )
                names = referenced
            else:
                names = tuple(dict.fromkeys((*explicit, *referenced)))
        canonical: dict[str, Any] = {}
        if columns_param and self.template_param:
            canonical[columns_param] = list(names)
        # Required AI scalars augment a valid base selection. This preserves
        # the base cardinality contract while canonicalizing one source list.
        _check_cardinality(names, self)
        required_ai = (
            str(_value(params, name, "") or "").strip()
            for name in self.required_ai_params
        )
        names = tuple(dict.fromkeys((*names, *(name for name in required_ai if name))))
        if columns_param and self.required_ai_params:
            canonical[columns_param] = list(names)
        return self._selection(names, params, canonical)

    def _select_group(self, params: Any) -> SelectedSource:
        selected: list[tuple[str, SelectedSource]] = []
        for binding, child in self.children:
            try:
                selected.append((binding, child.select(params)))
            except InputSelectionError as exc:
                raise InputSelectionError(
                    str(exc), field=exc.field, source=exc.source or binding or None
                ) from exc
        selected_tuple = tuple(selected)
        if self.distinct:
            seen: set[tuple[str, str]] = set()
            for binding, item in selected_tuple:
                for nested, leaf in item.leaves():
                    source = binding or nested
                    scope_param = leaf.source.sheet_param or "sheet_id"
                    scope = str(_value(params, scope_param, scope_param))
                    for name in leaf.columns:
                        key = (scope, name)
                        if key in seen:
                            raise InputSelectionError(
                                "field source columns must be distinct",
                                field=leaf.source.error_field,
                                source=source,
                            )
                        seen.add(key)
        return SelectedSource(self, children=selected_tuple)

    def _active_children(self, params: Any) -> tuple[SourceInput, ...]:
        active = tuple(child for _, child in self.children if child.active(params))
        if len(active) == 1:
            return active
        field = "params"
        if len(active) > 1:
            conflicting = next(
                (
                    child
                    for child in active
                    if self.default
                    not in {row["id"] for row in child.catalog_requirements()}
                ),
                active[-1],
            )
            field = (
                f"params.{conflicting.template_param}"
                if conflicting.mode == "template"
                else conflicting.error_field
            )
        raise InputSelectionError(
            "exactly one source alternative must be active", field=field
        )

    def catalog_requirements(self) -> list[dict[str, Any]]:
        if self.mode == "one_of":
            rows: list[dict[str, Any]] = []
            for _, child in self.children:
                for row in child.catalog_requirements():
                    row["one_of_group"] = self.group
                    if row["id"] == self.default:
                        row["one_of_default"] = True
                    rows.append(row)
            return rows
        if self.mode in {"fields", "all"}:
            rows = []
            for role, child in self.children:
                for row in child.catalog_requirements():
                    if self.mode == "fields":
                        row["role"] = role
                        if child.mode == "column":
                            row["mode"] = "field"
                    rows.append(row)
            return rows
        assert self.id is not None and self.label is not None
        row: dict[str, Any] = {
            "id": self.id,
            "mode": self.mode,
            "label": self.label,
            "min": self.min,
        }
        if self.param is not None:
            row["param"] = self.param
        if self.sheet_param is not None:
            row["sheet_param"] = self.sheet_param
        if self.mode == "fixed_column":
            row["column_name"] = self.id
        if self.max is not None:
            row["max"] = self.max
        if self.accepted_types is not None:
            row["accepted_column_types"] = list(self.accepted_types)
        if self.cell_kinds:
            row["accepted_cell_kinds"] = list(self.cell_kinds)
        catalog_template = (
            self.template_param if self.param != self.template_param else None
        )
        if catalog_template is not None:
            row["template_param"] = catalog_template
        if self.mode == "template" or catalog_template is not None:
            row["template_columns"] = self.template_columns
        if self.message is not None:
            row["message"] = self.message
        if self.next_steps:
            row["next_steps"] = list(self.next_steps)
        if self.includes_ai:
            row["unset_fallback_includes_ai_generated"] = True
        if self.required_ai_params:
            row["source_union_params"] = list(self.required_ai_params)
        return [row]

    def owned_params(self) -> frozenset[str]:
        if self.children:
            return frozenset().union(
                *(child.owned_params() for _, child in self.children)
            )
        return self.owned


@dataclass(frozen=True)
class ResolvedSource:
    """Project column identities projected from one selected source tree."""

    by_source: dict[str, int | list[int]]
    column_ids: dict[str, int]
    column_types: dict[str, str]
    column_types_by_source: dict[str, str | list[str]] = field(default_factory=dict)


class SourceResolutionError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        kind: Literal["missing", "type", "required_ai"],
        field: str,
        source: str | None,
        details: dict[str, Any],
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.field = field
        self.source = source
        self.details = details


def resolve_source_columns(
    project: Any,
    *,
    sheet_id: int,
    selected: SelectedSource,
    params: Any | None = None,
) -> ResolvedSource:
    """Resolve visible columns once, preserving keyed groups and flat order.

    Most leaves use ``sheet_id``. A leaf with ``sheet_param`` resolves that
    parameter from the same validated params used for source selection.
    """

    columns_by_sheet: dict[int, dict[str, Any]] = {}
    by_source: dict[str, int | list[int]] = {}
    column_ids: dict[str, int] = {}
    column_types: dict[str, str] = {}
    column_types_by_source: dict[str, str | list[str]] = {}
    for binding, leaf in selected.leaves():
        leaf_sheet_id = sheet_id
        if leaf.source.sheet_param is not None:
            scoped_sheet_id = _value(params, leaf.source.sheet_param)
            if type(scoped_sheet_id) is not int or scoped_sheet_id <= 0:
                raise SourceResolutionError(
                    "source sheet reference is invalid",
                    kind="missing",
                    field=f"params.{leaf.source.sheet_param}",
                    source=binding,
                    details={"sheet_param": leaf.source.sheet_param},
                )
            leaf_sheet_id = scoped_sheet_id
        if leaf_sheet_id not in columns_by_sheet:
            columns_by_sheet[leaf_sheet_id] = {
                str(column["name"]): column for column in project.columns(leaf_sheet_id)
            }
        by_name = columns_by_sheet[leaf_sheet_id]
        missing = [name for name in leaf.columns if name not in by_name]
        if missing:
            raise SourceResolutionError(
                "source column does not exist",
                kind="missing",
                field=leaf.source.error_field,
                source=binding,
                details={"missing": missing},
            )
        invalid = [
            {"name": name, "type": str(by_name[name]["type"])}
            for name in leaf.columns
            if leaf.source.accepted_types is not None
            and str(by_name[name]["type"]) not in leaf.source.accepted_types
        ]
        if invalid:
            raise SourceResolutionError(
                "source column type is incompatible",
                kind="type",
                field=leaf.source.error_field,
                source=binding,
                details={
                    "columns": invalid,
                    "accepted_column_types": list(leaf.source.accepted_types or ()),
                },
            )
        for required in leaf.required_ai_inputs:
            column = by_name.get(required.column_name)
            if column is None or not bool(column["ai_generated"]):
                raise SourceResolutionError(
                    "source column must be AI-generated",
                    kind="required_ai",
                    field=required.error_field,
                    source=binding,
                    details={"column": required.column_name},
                )
        ids = [int(by_name[name]["id"]) for name in leaf.columns]
        if binding is not None:
            by_source[binding] = (
                ids[0] if leaf.source.mode in {"column", "fixed_column"} else ids
            )
            types = [str(by_name[name]["type"]) for name in leaf.columns]
            column_types_by_source[binding] = (
                types[0] if leaf.source.mode in {"column", "fixed_column"} else types
            )
        for name, column_id in zip(leaf.columns, ids, strict=True):
            column_ids.setdefault(name, column_id)
            column_types.setdefault(name, str(by_name[name]["type"]))
    return ResolvedSource(
        by_source,
        column_ids,
        column_types,
        column_types_by_source,
    )


def _leaf(
    mode: str,
    param: str,
    *,
    columns_param: str | None = None,
    template_param: str | None = None,
    min: int = 1,
    max: int | None = None,
    types: Any = None,
    template_columns: _TemplateColumns = "union",
    id: str | None = None,
    label: str | None = None,
    cell_kinds: Any = (),
    message: str | None = None,
    next_steps: Any = (),
    default_includes_ai_generated: bool = False,
    error_field: str | None = None,
    owned_params: Any = None,
    required_ai_params: Any = (),
    resolver: Callable[[Any], Any] | None = None,
    sheet_param: str | None = None,
) -> SourceInput:
    _validate_cardinality(min, max)
    if not param:
        raise ValueError("source parameter must be non-empty")
    if template_param is not None and not template_param:
        raise ValueError("template_param must be non-empty")
    if sheet_param is not None and not sheet_param:
        raise ValueError("sheet_param must be non-empty")
    if template_columns not in ("exact", "union"):
        raise ValueError("template_columns must be 'exact' or 'union'")
    required_ai = _ordered(required_ai_params, name="required_ai_params")
    owned = (
        _ordered(owned_params, name="owned_params")
        if owned_params is not None
        else tuple(
            name for name in (columns_param, template_param, *required_ai) if name
        )
    )
    if sheet_param is not None:
        owned = tuple(dict.fromkeys((*owned, sheet_param)))
    return SourceInput(
        mode=mode,
        param=None if mode == "fixed_column" else param,
        template_param=template_param,
        template_columns=template_columns,
        min=min,
        max=max,
        accepted_types=None if types is None else _ordered(types, name="types"),
        id=id or param,
        label=label or param.replace("_", " ").capitalize(),
        cell_kinds=_ordered(cell_kinds, name="cell_kinds"),
        message=message,
        next_steps=_ordered(next_steps, name="next_steps"),
        includes_ai=default_includes_ai_generated,
        error_field=error_field or f"params.{param}",
        owned=frozenset(owned),
        required_ai_params=required_ai,
        sheet_param=sheet_param,
        resolver=resolver,
    )


def FixedColumn(name: str, **metadata: Any) -> SourceInput:
    return _leaf(
        "fixed_column",
        name,
        min=1,
        max=1,
        error_field="params.sheet_id",
        owned_params=(),
        **metadata,
    )


def Column(param: str, **metadata: Any) -> SourceInput:
    return _leaf("column", param, columns_param=param, min=1, max=1, **metadata)


def Columns(param: str, **metadata: Any) -> SourceInput:
    return _leaf("columns", param, columns_param=param, **metadata)


def Template(
    template_param: str, *, columns: str | None = None, **metadata: Any
) -> SourceInput:
    if not template_param:
        raise ValueError("template_param must be non-empty")
    if columns is not None and not columns:
        raise ValueError("columns must be non-empty")
    param = columns or template_param
    return _leaf(
        "template",
        param,
        columns_param=columns,
        template_param=template_param,
        error_field=f"params.{param}",
        **metadata,
    )


def Fields(*, distinct: bool = False, **roles: SourceInput) -> SourceInput:
    if not roles:
        raise ValueError("Fields requires at least one role=column")
    for role, source in roles.items():
        if not role:
            raise ValueError("field role must be non-empty")
        if not isinstance(source, SourceInput) or source.mode not in {
            "column",
            "columns",
        }:
            raise TypeError("Fields roles must be Column or Columns descriptors")
    return SourceInput(mode="fields", children=tuple(roles.items()), distinct=distinct)


def _all_of(*sources: SourceInput) -> SourceInput:
    """Combine independent source declarations without assigning roles."""
    if not sources:
        raise ValueError("source group requires at least one source")
    return SourceInput(mode="all", children=tuple(("", source) for source in sources))


def OneOf(group: str, *modes: SourceInput, default: str | None = None) -> SourceInput:
    if not group:
        raise ValueError("OneOf group must be non-empty")
    if len(modes) < 2:
        raise ValueError("OneOf requires at least two modes")
    ids = [row["id"] for mode in modes for row in mode.catalog_requirements()]
    if len(ids) != len(set(ids)):
        raise ValueError("OneOf requirement ids must be unique")
    chosen = default or ids[0]
    if chosen not in ids:
        raise ValueError("OneOf default must identify one branch")
    return SourceInput(
        mode="one_of",
        children=tuple(("", mode) for mode in modes),
        group=group,
        default=chosen,
    )


def Computed(
    resolver: Callable[[Any], Any],
    *,
    owned_params: Any,
    error_field: str,
    mode: str,
    param: str,
    **metadata: Any,
) -> SourceInput:
    """Declare columns computed from arbitrary validated action parameters."""
    return _leaf(
        mode,
        param,
        min=0,
        error_field=error_field,
        owned_params=owned_params,
        resolver=resolver,
        **metadata,
    )


__all__ = [
    "Column",
    "Columns",
    "Computed",
    "Fields",
    "FixedColumn",
    "InputSelectionError",
    "OneOf",
    "RequiredAiInput",
    "ResolvedSource",
    "SelectedSource",
    "SourceInput",
    "SourceInputDescriptor",
    "SourceResolutionError",
    "Template",
    "resolve_source_columns",
]
