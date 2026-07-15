"""Builtin verifiers and the factory that builds them from a spec."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from hiveloom import ext
from hiveloom.spec.loader import _import_hook
from hiveloom.spec.schema import (
    BuiltinValidatorRef,
    CodeValidatorRef,
    HarnessSpec,
)
from hiveloom.verify.base import VerdictResult, Verifier


class OutputSchemaVerifier(Verifier):
    name = "output_schema"

    def __init__(self, schema_file: str, base: Path):
        self._schema_path = base / schema_file

    def validate(self, run_output: str, run_context: dict[str, Any]) -> VerdictResult:
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

    def validate(self, run_output: str, run_context: dict[str, Any]) -> VerdictResult:
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

    def validate(self, run_output: str, run_context: dict[str, Any]) -> VerdictResult:
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

    def validate(self, run_output: str, run_context: dict[str, Any]) -> VerdictResult:
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

    def validate(self, run_output: str, run_context: dict[str, Any]) -> VerdictResult:
        result = self._func(run_output, run_context)
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


def build_verifiers(spec: HarnessSpec, base_dir: str | Path) -> list[Verifier]:
    """Instantiate verifiers (builtins + code hooks) from a spec."""
    base = Path(base_dir)
    if base.is_file():
        base = base.parent

    verifiers: list[Verifier] = []
    for ref in spec.verify.validators:
        if isinstance(ref, BuiltinValidatorRef):
            verifiers.append(_make_builtin(ref, base))
        elif isinstance(ref, CodeValidatorRef):
            func = _import_hook(ref.code, base)
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


_register_factories()
