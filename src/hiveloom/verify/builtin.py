"""Builtin verifiers and the factory that builds them from a spec."""

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from hiveloom import ext
from hiveloom.confine import ConfinementUnavailable, run_confined, shell_argv
from hiveloom.json_path import extract_json_path, parse_json_path
from hiveloom.package import resolve_trace_dir
from hiveloom.private import RunBoundary, runtime_private_paths
from hiveloom.spec.loader import import_hook
from hiveloom.spec.schema import (
    BuiltinValidatorRef,
    CodeValidatorRef,
    ConfinementConfig,
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

    def __init__(
        self,
        command: str,
        base: Path,
        *,
        timeout: int = 600,
        confinement: Any = None,
        trace_root: Path | None = None,
        private_paths: list[Path] | None = None,
        run_boundary: RunBoundary | None = None,
    ):
        self._command = command
        self._base = base
        self._timeout = timeout
        self._confinement = confinement or ConfinementConfig()
        # Same masking the shell tool gets. A validator command is authored in
        # the spec rather than by the model, but `verify.validators` is a
        # mutable field, and a check has no business reading the journal that
        # records its own verdict. A code validator remains the way to inspect
        # run state — it is Python in the hiveloom process, not a spawn.
        self._masked = list(private_paths or [base / ".hiveloom"])
        self._run_boundary = run_boundary
        if trace_root is not None and trace_root not in self._masked:
            self._masked.append(trace_root)

    def validate(
        self,
        run_output: str,
        run_context: dict[str, Any],
        verification_context: VerificationContext | None = None,
    ) -> VerdictResult:
        del run_output, run_context, verification_context
        # Still a shell string — the command is spec-authored and often a
        # pipeline — but the shell now runs inside the confinement policy like
        # any other spawn, and under a timeout: a validator that never returns
        # used to hang the run with no verdict at all.
        try:
            result = run_confined(
                shell_argv(self._command),
                cwd=self._base,
                config=self._confinement,
                timeout=self._timeout,
                mask=(
                    self._run_boundary.private_paths()
                    if self._run_boundary is not None
                    else self._masked
                ),
            )
        except ConfinementUnavailable as exc:
            return VerdictResult(passed=False, feedback=str(exc), verifier=self.name)
        except OSError as exc:
            return VerdictResult(
                passed=False,
                feedback=f"command could not be started: {exc}",
                verifier=self.name,
            )
        if result.timed_out:
            return VerdictResult(
                passed=False,
                feedback=f"command did not finish within {self._timeout}s",
                verifier=self.name,
            )
        if result.returncode == 0:
            return VerdictResult(passed=True, verifier=self.name)
        return VerdictResult(
            passed=False,
            feedback=f"command failed (exit {result.returncode}): {result.output}",
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


def build_verifiers(
    spec: HarnessSpec,
    base_dir: str | Path,
    *,
    run_boundary: RunBoundary | None = None,
) -> list[Verifier]:
    """Instantiate verifiers (builtins + code hooks) from a spec."""
    base = Path(base_dir)
    return build_verifiers_from_refs(
        spec.verify.validators,
        base_dir,
        confinement=spec.confinement,
        trace_root=(
            run_boundary.trace_dir
            if run_boundary is not None
            else resolve_trace_dir(
                base.parent if base.is_file() else base, spec.logging.trace_dir
            )
        ),
        private_paths=(
            run_boundary.private_paths()
            if run_boundary is not None
            else runtime_private_paths(base.parent if base.is_file() else base, spec)
        ),
        run_boundary=run_boundary,
    )


def build_verifiers_from_refs(
    refs: list[Any],
    base_dir: str | Path,
    *,
    confinement: Any = None,
    trace_root: Path | None = None,
    private_paths: list[Path] | None = None,
    run_boundary: RunBoundary | None = None,
) -> list[Verifier]:
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
            verifiers.append(
                _make_builtin(
                    ref,
                    base,
                    confinement,
                    trace_root,
                    private_paths,
                    run_boundary,
                )
            )
        elif isinstance(ref, CodeValidatorRef):
            func = import_hook(ref.code, base)
            _, func_name = ref.code.rsplit(":", 1)
            verifiers.append(CodeVerifier(func, name=func_name))
    return verifiers


def _make_builtin(
    ref: BuiltinValidatorRef,
    base: Path,
    confinement: Any = None,
    trace_root: Path | None = None,
    private_paths: list[Path] | None = None,
    run_boundary: RunBoundary | None = None,
) -> Verifier:
    return ext.build(
        "validators",
        ref.builtin,
        ref.params(),
        ext.BuildContext(
            base=base,
            confinement=confinement,
            trace_root=trace_root,
            private_paths=list(private_paths or []),
            run_boundary=run_boundary,
        ),
    )


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
        lambda p, ctx: CommandSucceedsVerifier(
            p["command"],
            ctx.base,
            timeout=p.get("timeout", 600),
            confinement=ctx.confinement,
            trace_root=ctx.trace_root,
            private_paths=list(ctx.private_paths or []),
            run_boundary=ctx.run_boundary,
        ),
    )
    ext.register_builtin_factory(
        "validators",
        "grounded_references",
        lambda p, _ctx: GroundedReferencesVerifier(
            p["output_path"], p["evidence_paths"], p.get("normalize", "string")
        ),
    )


_register_factories()
