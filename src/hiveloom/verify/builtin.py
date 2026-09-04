"""Builtin verifiers and the factory that builds them from a spec."""

from __future__ import annotations

import inspect
import json
import re
import subprocess
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from hiveloom import ext
from hiveloom.json_path import extract_json_path, parse_json_path
from hiveloom.spec.loader import import_hook
from hiveloom.spec.schema import (
    BuiltinValidatorRef,
    CodeValidatorRef,
    HarnessSpec,
)
from hiveloom.verify.base import VerdictResult, VerificationContext, Verifier


class OutputSchemaVerifier(Verifier):
    name = "output_schema"

    def __init__(self, schema_file: str, base: Path):
        self._schema_path = base / schema_file

    def validate(
        self,
        run_output: str,
        run_context: dict[str, Any],
        verification_context: VerificationContext | None = None,
    ) -> VerdictResult:
        del run_context, verification_context
        import jsonschema

        if not self._schema_path.exists():
            return VerdictResult(
                passed=False,
                feedback=f"schema file not found: {self._schema_path}",
                verifier=self.name,
            )
        schema = json.loads(self._schema_path.read_text(encoding="utf-8"))
        try:
            data = json.loads(run_output)
        except json.JSONDecodeError as exc:
            return VerdictResult(
                passed=False,
                feedback=f"output is not valid JSON: {exc}. Emit only a JSON object.",
                verifier=self.name,
            )
        try:
            jsonschema.validate(data, schema)
        except jsonschema.ValidationError as exc:
            return VerdictResult(
                passed=False,
                feedback=f"output does not match schema: {exc.message}",
                verifier=self.name,
            )
        return VerdictResult(passed=True, verifier=self.name)


class RegexMatchVerifier(Verifier):
    name = "regex_match"

    def __init__(self, pattern: str):
        self._pattern = re.compile(pattern)

    def validate(
        self,
        run_output: str,
        run_context: dict[str, Any],
        verification_context: VerificationContext | None = None,
    ) -> VerdictResult:
        del run_context, verification_context
        if self._pattern.search(run_output):
            return VerdictResult(passed=True, verifier=self.name)
        return VerdictResult(
            passed=False,
            feedback=f"output must match /{self._pattern.pattern}/",
            verifier=self.name,
        )


class FileExistsVerifier(Verifier):
    name = "file_exists"

    def __init__(self, path: str, base: Path):
        self._path = base / path
        self._rel = path

    def validate(
        self,
        run_output: str,
        run_context: dict[str, Any],
        verification_context: VerificationContext | None = None,
    ) -> VerdictResult:
        del run_output, run_context, verification_context
        if self._path.exists():
            return VerdictResult(passed=True, verifier=self.name)
        return VerdictResult(
            passed=False, feedback=f"expected file '{self._rel}' to exist", verifier=self.name
        )


class CommandSucceedsVerifier(Verifier):
    name = "command_succeeds"

    def __init__(self, command: str, base: Path):
        self._command = command
        self._base = base

    def validate(
        self,
        run_output: str,
        run_context: dict[str, Any],
        verification_context: VerificationContext | None = None,
    ) -> VerdictResult:
        del run_output, run_context, verification_context
        proc = subprocess.run(
            self._command,
            shell=True,  # noqa: S602 - command is spec-authored, not model output
            cwd=self._base,
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            return VerdictResult(passed=True, verifier=self.name)
        return VerdictResult(
            passed=False,
            feedback=f"command failed (exit {proc.returncode}): {proc.stdout}{proc.stderr}",
            verifier=self.name,
        )


class CodeVerifier(Verifier):
    """Wraps a user code-hook validator."""

    def __init__(self, func, name: str):
        self._func = func
        self.name = name
        parameters = list(inspect.signature(func).parameters.values())
        self._accepts_verification_context = len(parameters) >= 3 or any(
            parameter.kind in {parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD}
            for parameter in parameters
        )

    def validate(
        self,
        run_output: str,
        run_context: dict[str, Any],
        verification_context: VerificationContext | None = None,
    ) -> VerdictResult:
        result = (
            self._func(run_output, run_context, verification_context)
            if self._accepts_verification_context
            else self._func(run_output, run_context)
        )
        if isinstance(result, VerdictResult):
            result.verifier = result.verifier or self.name
            return result
        if isinstance(result, dict):
            return VerdictResult(
                passed=bool(result.get("passed", False)),
                feedback=str(result.get("feedback", "")),
                verifier=self.name,
            )
        return VerdictResult(passed=bool(result), verifier=self.name)


class EvidencePath(BaseModel):
    """A tool result and JSON path allowed to support output references."""

    model_config = ConfigDict(extra="forbid")

    tool: str
    path: str

    @field_validator("tool")
    @classmethod
    def _tool_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("evidence tool must not be blank")
        return value

    @field_validator("path")
    @classmethod
    def _valid_path(cls, value: str) -> str:
        parse_json_path(value)
        return value


def _normalize_reference(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return str(value)
    return None


class GroundedReferencesVerifier(Verifier):
    """Require every selected scalar reference to occur in allowed tool evidence."""

    name = "grounded_references"

    def __init__(
        self,
        output_path: str,
        evidence_paths: list[dict[str, Any]],
        normalize: str = "string",
    ):
        if normalize != "string":
            raise ValueError("grounded_references normalize must be 'string'")
        parse_json_path(output_path)
        self._output_path = output_path
        self._evidence_paths = [EvidencePath.model_validate(item) for item in evidence_paths]

    def validate(
        self,
        run_output: str,
        run_context: dict[str, Any],
        verification_context: VerificationContext | None = None,
    ) -> VerdictResult:
        del run_context
        try:
            output = json.loads(run_output)
        except json.JSONDecodeError as exc:
            return VerdictResult(
                passed=False,
                feedback=f"output is not valid JSON: {exc}",
                verifier=self.name,
            )
        selected = {
            normalized
            for value in extract_json_path(output, self._output_path)
            if (normalized := _normalize_reference(value)) is not None
        }
        evidence: set[str] = set()
        if verification_context is not None:
            for path in self._evidence_paths:
                for record in verification_context.tool_calls:
                    if record.name != path.tool or record.is_error:
                        continue
                    evidence.update(
                        normalized
                        for value in extract_json_path(record.result, path.path)
                        if (normalized := _normalize_reference(value)) is not None
                    )
        missing = sorted(selected - evidence)
        if not missing:
            return VerdictResult(passed=True, verifier=self.name)
        displayed = missing[:50]
        suffix = f" (+{len(missing) - len(displayed)} more)" if len(missing) > 50 else ""
        feedback = "selected references absent from approved tool evidence: " + ", ".join(
            json.dumps(value) for value in displayed
        )
        return VerdictResult(
            passed=False,
            feedback=(feedback + suffix)[:2000],
            verifier=self.name,
        )


def build_verifiers(spec: HarnessSpec, base_dir: str | Path) -> list[Verifier]:
    """Instantiate verifiers (builtins + code hooks) from a spec."""
    return build_verifiers_from_refs(spec.verify.validators, base_dir)


def build_verifiers_from_refs(refs: list[Any], base_dir: str | Path) -> list[Verifier]:
    """Instantiate verifiers from validator refs.

    Split out of :func:`build_verifiers` so a playbook's mode-scoped
    validators are built through exactly the same path as the spec's.
    """
    base = Path(base_dir)
    if base.is_file():
        base = base.parent

    verifiers: list[Verifier] = []
    for ref in refs:
        if isinstance(ref, BuiltinValidatorRef):
            verifiers.append(_make_builtin(ref, base))
        elif isinstance(ref, CodeValidatorRef):
            func = import_hook(ref.code, base)
            _, func_name = ref.code.rsplit(":", 1)
            verifiers.append(CodeVerifier(func, name=func_name))
    return verifiers


def _make_builtin(ref: BuiltinValidatorRef, base: Path) -> Verifier:
    return ext.build("validators", ref.builtin, ref.params(), ext.BuildContext(base=base))


def _register_factories() -> None:
    ext.register_builtin_factory(
        "validators",
        "output_schema",
        lambda p, ctx: OutputSchemaVerifier(p["schema_file"], ctx.base),
    )
    ext.register_builtin_factory(
        "validators", "regex_match", lambda p, _c: RegexMatchVerifier(p["pattern"])
    )
    ext.register_builtin_factory(
        "validators", "file_exists", lambda p, ctx: FileExistsVerifier(p["path"], ctx.base)
    )
    ext.register_builtin_factory(
        "validators",
        "command_succeeds",
        lambda p, ctx: CommandSucceedsVerifier(p["command"], ctx.base),
    )
    ext.register_builtin_factory(
        "validators",
        "grounded_references",
        lambda p, _ctx: GroundedReferencesVerifier(
            p["output_path"], p["evidence_paths"], p.get("normalize", "string")
        ),
    )


_register_factories()
