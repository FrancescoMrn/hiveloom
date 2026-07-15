"""Derive the annotated YAML template and field docs from the pydantic schema.

Everything here reads from :mod:`hiveloom.spec.schema` field metadata, so the
``hiveloom schema --annotated`` template and ``hiveloom explain`` output can
never drift from the actual contract. The annotated template is a *valid* spec
(placeholder values), so it round-trips through the loader.
"""

from __future__ import annotations

import inspect
import typing
from typing import Any, Union, get_args, get_origin

import yaml
from pydantic import BaseModel
from pydantic.fields import FieldInfo

from hiveloom.spec import schema as S
from hiveloom.spec.loader import _SpecDumper


def json_schema() -> dict[str, Any]:
    """Return the JSON schema for :class:`HarnessSpec`."""
    return S.HarnessSpec.model_json_schema()


def _placeholder_spec() -> S.HarnessSpec:
    """A complete, valid spec with placeholder values, used for the template."""
    return S.HarnessSpec(
        name="example-harness",
        description="One-line description of the task this harness performs.",
        system_prompt=(
            "You are a focused agent. Do the task, use tools when needed,\n"
            "and stop when the work is verified. The evolver may rewrite this.\n"
        ),
        tools=[S.BuiltinToolRef(builtin="file_read")],
        guardrails=[
            S.BuiltinGuardrailRef(builtin="max_cost_usd", value=0.50),
            S.BuiltinGuardrailRef(builtin="max_wall_clock_seconds", value=300),
            S.BuiltinGuardrailRef(builtin="tool_allowlist"),
        ],
        verify=S.VerifyConfig(
            validators=[
                S.BuiltinValidatorRef(
                    builtin="output_schema", schema_file="./schemas/output.json"
                )
            ]
        ),
    )


def _strip_annotated(annotation: Any) -> Any:
    """Unwrap ``Annotated[T, ...]`` (used for the discriminated ref unions) to T."""
    while hasattr(annotation, "__metadata__"):
        annotation = annotation.__origin__
    return annotation


def _unwrap(annotation: Any) -> Any:
    """Strip Annotated + Optional/Union noise to the primary type for display."""
    annotation = _strip_annotated(annotation)
    origin = get_origin(annotation)
    if origin is Union or (origin is not None and origin.__name__ == "UnionType"):
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return _strip_annotated(args[0])
    return annotation


def _is_model(annotation: Any) -> bool:
    return inspect.isclass(annotation) and issubclass(annotation, BaseModel)


def _type_label(annotation: Any) -> str:
    annotation = _strip_annotated(annotation)
    origin = get_origin(annotation)
    if origin in (list,):
        args = get_args(annotation)
        inner = args[0] if args else Any
        return f"list[{_type_label(inner)}]"
    if origin is Union or (origin is not None and getattr(origin, "__name__", "") == "UnionType"):
        return " | ".join(
            _type_label(a) for a in get_args(annotation) if a is not type(None)
        )
    if inspect.isclass(annotation):
        return annotation.__name__
    return str(annotation).replace("typing.", "")


def _comment(text: str, indent: int) -> str:
    single_line = " ".join(text.split())
    return f"{'  ' * indent}# {single_line}"


def _emit_model(model_cls: type[BaseModel], data: dict[str, Any], indent: int) -> list[str]:
    lines: list[str] = []
    for name, field in model_cls.model_fields.items():
        field: FieldInfo
        desc = field.description or ""
        annotation = _unwrap(field.annotation)
        value = data.get(name)

        if _is_model(annotation) and isinstance(value, dict):
            if desc:
                lines.append(_comment(desc, indent))
            lines.append(f"{'  ' * indent}{name}:")
            lines.extend(_emit_model(annotation, value, indent + 1))
        else:
            label = _type_label(field.annotation)
            header = f"{desc} ({label})" if desc else label
            lines.append(_comment(header, indent))
            block = yaml.dump(
                {name: value},
                Dumper=_SpecDumper,
                sort_keys=False,
                default_flow_style=False,
                allow_unicode=True,
                width=100,
            ).rstrip("\n")
            lines.extend(f"{'  ' * indent}{line}" for line in block.splitlines())
        lines.append("")  # blank line between fields for readability
    # drop trailing blank
    while lines and lines[-1] == "":
        lines.pop()
    return lines


def annotated_template() -> str:
    """Return an annotated YAML template derived from field descriptions.

    The output is a valid spec (placeholder values) and round-trips through the
    loader once comments are stripped by the YAML parser.
    """
    spec = _placeholder_spec()
    data = spec.model_dump(mode="json", exclude_none=True)
    header = [
        "# hiveloom harness spec — annotated template.",
        "# Every section below is required. Replace placeholder values.",
        "# Generated from the pydantic schema; do not hand-maintain.",
        "",
    ]
    body = _emit_model(S.HarnessSpec, data, indent=0)
    return "\n".join(header + body) + "\n"


def explain(path: str) -> dict[str, Any]:
    """Return field-level documentation for a dotted spec path.

    Example: ``explain("context.compaction")`` -> description, type, default,
    and sub-fields of the compaction config.
    """
    parts = [p for p in path.split(".") if p]
    if not parts:
        raise KeyError("empty path")

    cls: Any = S.HarnessSpec
    field: FieldInfo | None = None
    walked: list[str] = []
    for part in parts:
        if not _is_model(cls):
            raise KeyError(
                f"'{'.'.join(walked)}' is not a nested object; cannot descend to '{part}'"
            )
        fields = cls.model_fields
        if part not in fields:
            available = ", ".join(sorted(fields))
            raise KeyError(
                f"unknown field '{part}' at '{'.'.join(walked) or '<root>'}' "
                f"(available: {available})"
            )
        field = fields[part]
        cls = _unwrap(field.annotation)
        walked.append(part)

    assert field is not None
    default = _describe_default(field)
    info: dict[str, Any] = {
        "path": ".".join(walked),
        "type": _type_label(field.annotation),
        "description": field.description or "",
        "required": field.is_required(),
        "default": default,
    }
    if _is_model(cls):
        info["fields"] = {
            sub_name: (sub.description or "")
            for sub_name, sub in cls.model_fields.items()
        }
    literal_values = _literal_values(field.annotation)
    if literal_values is not None:
        info["choices"] = literal_values
    return info


def _jsonify(value: Any) -> Any:
    """Convert pydantic models / nested structures to JSON-safe primitives."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {k: _jsonify(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonify(v) for v in value]
    return value


def _describe_default(field: FieldInfo) -> Any:
    if field.default_factory is not None:
        try:
            return _jsonify(field.default_factory())  # type: ignore[call-arg]
        except TypeError:
            return None
    if field.is_required():
        return None
    return _jsonify(field.default)


def _literal_values(annotation: Any) -> list[Any] | None:
    ann = _unwrap(annotation)
    if get_origin(ann) is typing.Literal:
        return list(get_args(ann))
    return None
