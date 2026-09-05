"""OS-level confinement of the processes the runtime spawns."""

from __future__ import annotations

import json
import platform
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hiveloom import confine, construct, runner
from hiveloom.cli import app
from hiveloom.confine import ConfinementUnavailable, run_confined
from hiveloom.evolve.evolver import gate
from hiveloom.evolve.proposals import MutationProposal
from hiveloom.logging.journal import read_events
from hiveloom.models.fake import FakeModelProvider, text_response, tool_response
from hiveloom.spec.loader import load_spec
from hiveloom.spec.schema import ConfinementConfig
from hiveloom.tools.builtin import ShellTool
from hiveloom.tools.registry import ToolError
from hiveloom.verify.builtin import CommandSucceedsVerifier

cli = CliRunner()

sandboxed = pytest.mark.skipif(
    confine.available_backend() == "none",
    reason="no OS sandbox backend on this machine (bwrap / sandbox-exec)",
)


# --------------------------------------------------------------------------- #
# The portable baseline — true on every platform, backend or not
# --------------------------------------------------------------------------- #
def test_the_runtimes_own_environment_is_not_inherited(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    result = run_confined(
        ["/bin/sh", "-c", "printenv"], cwd=tmp_path, config=ConfinementConfig()
    )
    assert result.returncode == 0
    assert "sk-should-not-leak" not in result.output
    assert "PATH=" in result.output


def test_named_variables_can_be_passed_through(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BUILD_TOKEN", "wanted")
    monkeypatch.setenv("OTHER_SECRET", "unwanted")
    result = run_confined(
        ["/bin/sh", "-c", "printenv"],
        cwd=tmp_path,
        config=ConfinementConfig(env_passthrough=["BUILD_TOKEN"]),
    )
    assert "wanted" in result.output
    assert "unwanted" not in result.output


def test_a_timeout_kills_the_whole_process_tree(tmp_path: Path):
    marker = tmp_path / "grandchild-survived"
    started = time.monotonic()
    result = run_confined(
        # The backgrounded grandchild outlives its parent unless the kill is
        # group-wide, which is the whole reason the spawn gets its own session.
        ["/bin/sh", "-c", f"(sleep 4; touch {marker}) & sleep 4"],
        cwd=tmp_path,
        config=ConfinementConfig(mode="off", timeout_seconds=1),
    )
    assert result.timed_out
    assert time.monotonic() - started < 3
    time.sleep(4)
    assert not marker.exists()


def test_descendants_holding_pipes_share_the_same_deadline(tmp_path: Path):
    started = time.monotonic()
    result = run_confined(
        ["/bin/sh", "-c", "sleep 5 &"],
        cwd=tmp_path,
        config=ConfinementConfig(mode="off", timeout_seconds=1),
    )

    assert result.timed_out
    assert time.monotonic() - started < 2


def test_runaway_output_is_bounded_in_memory_and_on_disk(tmp_path: Path):
    result = run_confined(
        ["/bin/sh", "-c", "seq 1 500000"],
        cwd=tmp_path,
        config=ConfinementConfig(max_output_bytes=500),
    )
    assert result.truncated
    assert result.discarded_bytes > 0
    assert len(result.stdout.encode()) <= 600  # budget plus the dropped-bytes note
    # Head and tail both survive: the last line of a failing build is the one
    # that says why.
    assert result.stdout.startswith("1\n")
    assert result.stdout.rstrip().endswith("500000")


def test_a_gigabyte_of_output_costs_neither_memory_nor_disk(tmp_path: Path):
    import resource

    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    result = run_confined(
        ["/bin/sh", "-c", "yes flooding | head -c 1000000000"],
        cwd=tmp_path,
        config=ConfinementConfig(max_output_bytes=2000, timeout_seconds=120),
    )
    after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    assert result.discarded_bytes > 999_000_000
    assert len(result.stdout.encode()) <= 2100
    # The excess is drained and dropped, never buffered or spooled to a file.
    assert after - before < 32 * 1024  # KiB on Linux


def test_stdin_is_closed_so_a_command_cannot_wait_on_input(tmp_path: Path):
    result = run_confined(["/bin/cat"], cwd=tmp_path, config=ConfinementConfig())
    assert result.returncode == 0
    assert not result.timed_out


# --------------------------------------------------------------------------- #
# The sandbox layer
# --------------------------------------------------------------------------- #
@sandboxed
def test_the_harness_directory_is_writable_and_the_rest_is_not(tmp_path: Path):
    inside = run_confined(
        ["/bin/sh", "-c", "echo ok > made.txt && cat made.txt"],
        cwd=tmp_path,
        config=ConfinementConfig(),
    )
    assert inside.returncode == 0
    assert (tmp_path / "made.txt").read_text() == "ok\n"

    outside = run_confined(
        ["/bin/sh", "-c", "echo nope > /etc/hiveloom-probe"],
        cwd=tmp_path,
        config=ConfinementConfig(),
    )
    assert outside.returncode != 0
    assert not Path("/etc/hiveloom-probe").exists()


@sandboxed
def test_writable_false_makes_even_the_harness_directory_read_only(tmp_path: Path):
    result = run_confined(
        ["/bin/sh", "-c", "echo nope > blocked.txt"],
        cwd=tmp_path,
        config=ConfinementConfig(writable=False),
    )
    assert result.returncode != 0
    assert not (tmp_path / "blocked.txt").exists()


@sandboxed
def test_the_network_is_unreachable_unless_the_spec_allows_it(tmp_path: Path):
    denied = run_confined(
        ["/bin/sh", "-c", "getent hosts example.com || echo NO-RESOLUTION"],
        cwd=tmp_path,
        config=ConfinementConfig(),
    )
    assert "NO-RESOLUTION" in denied.output


# --------------------------------------------------------------------------- #
# Mode
# --------------------------------------------------------------------------- #
def test_require_refuses_to_spawn_where_nothing_can_confine(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(confine, "available_backend", lambda: "none")
    config = ConfinementConfig(mode="require")

    assert confine.unavailable_reason(config)
    with pytest.raises(ConfinementUnavailable, match="no OS sandbox is available"):
        run_confined(["/bin/true"], cwd=tmp_path, config=config)


def test_auto_falls_back_to_the_baseline_where_nothing_can_confine(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(confine, "available_backend", lambda: "none")
    result = run_confined(["/bin/echo", "hi"], cwd=tmp_path, config=ConfinementConfig())
    assert result.returncode == 0
    assert result.backend == "none"
    assert confine.unavailable_reason(ConfinementConfig()) is None


def test_off_skips_the_sandbox_but_keeps_the_baseline(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    config = ConfinementConfig(mode="off")
    assert confine.resolve_backend(config) == "none"

    result = run_confined(["/bin/sh", "-c", "printenv"], cwd=tmp_path, config=config)
    assert "sk-should-not-leak" not in result.output


# --------------------------------------------------------------------------- #
# The two spawn sites
# --------------------------------------------------------------------------- #
def test_the_shell_tool_reports_a_command_it_had_to_kill(tmp_path: Path):
    tool = ShellTool(
        tmp_path, [{"argv": ["sleep", "5"]}], ConfinementConfig(timeout_seconds=1)
    )
    out = tool.run(command="sleep 5")
    assert "killed after 1s" in out


def test_the_shell_tool_still_runs_an_allowlisted_command(tmp_path: Path):
    tool = ShellTool(tmp_path, [{"argv": ["echo"], "allow_extra_args": True}])
    assert tool.run(command="echo hello").startswith("exit=0\nhello")


def test_the_shell_tool_surfaces_an_unmeetable_policy(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(confine, "available_backend", lambda: "none")
    tool = ShellTool(tmp_path, ["/bin/true"], ConfinementConfig(mode="require"))
    with pytest.raises(ToolError, match="no OS sandbox is available"):
        tool.run(command="/bin/true")


def test_command_succeeds_passes_and_fails_as_before(tmp_path: Path):
    passing = CommandSucceedsVerifier("exit 0", tmp_path)
    failing = CommandSucceedsVerifier("echo boom >&2; exit 3", tmp_path)

    assert passing.validate("out", {}).passed
    verdict = failing.validate("out", {})
    assert not verdict.passed
    assert "exit 3" in verdict.feedback
    assert "boom" in verdict.feedback


def test_a_validator_that_never_returns_fails_instead_of_hanging(tmp_path: Path):
    verifier = CommandSucceedsVerifier("sleep 30", tmp_path, timeout=1)
    verdict = verifier.validate("out", {})
    assert not verdict.passed
    assert "did not finish within 1s" in verdict.feedback


# --------------------------------------------------------------------------- #
# Reporting and freezing
# --------------------------------------------------------------------------- #
def test_the_journal_records_what_actually_confined_the_run(tmp_path: Path):
    directory = tmp_path / "h"
    construct.init_harness(directory, name="confined", task="Do a thing.")
    construct.set_field(directory, "loop.require_verification", "false")
    result = runner.run_harness(
        directory,
        "go",
        provider=FakeModelProvider([text_response("done")]),
        literal_input=True,
        ingest=False,
    )

    started = read_events(result.trace_path)[0]["payload"]["confinement"]
    assert started["mode"] == "auto"
    assert started["backend"] == confine.available_backend()
    assert started["platform"] == platform.system()


def test_evolution_can_never_widen_a_harnesss_confinement(tmp_path: Path):
    directory = tmp_path / "h"
    construct.init_harness(directory, name="confined", task="Do a thing.")
    construct.set_field(directory, "evolution.mutable", '["confinement"]')
    spec = load_spec(directory)

    proposal = MutationProposal(yaml_changes=[{"path": "confinement.mode", "value": "off"}])
    result = gate(spec, proposal)
    assert not result.accepted
    assert result.rejected == [{"path": "confinement.mode", "reason": "frozen path"}]


def test_the_cli_reports_what_this_machine_can_enforce(tmp_path: Path):
    directory = tmp_path / "h"
    construct.init_harness(directory, name="confined", task="Do a thing.")

    result = cli.invoke(app, ["confinement", str(directory), "--json"])
    assert result.exit_code == 0
    assert '"backend"' in result.stdout
    assert '"mode": "auto"' in result.stdout
    assert '"safe_for_untrusted_input": true' in result.stdout


def test_the_human_cli_makes_an_auto_fallback_visible(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(confine, "available_backend", lambda: "none")
    directory = tmp_path / "h"
    construct.init_harness(directory, name="confined", task="Do a thing.")

    result = cli.invoke(app, ["confinement", str(directory)])

    assert result.exit_code == 0
    assert "backend in use" in result.stdout
    assert "no OS sandbox on this host" in result.stdout
    assert "not filesystem isolation" in result.stdout


def test_the_cli_fails_when_a_required_sandbox_is_missing(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(confine, "available_backend", lambda: "none")
    directory = tmp_path / "h"
    construct.init_harness(directory, name="confined", task="Do a thing.")
    construct.set_field(directory, "confinement.mode", "require")

    result = cli.invoke(app, ["confinement", str(directory), "--json"])
    assert result.exit_code != 0
    assert '"ok": false' in result.stdout


def test_require_stops_a_run_before_a_provider_call(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(confine, "available_backend", lambda: "none")
    directory = tmp_path / "h"
    construct.init_harness(directory, name="confined", task="Do a thing.")
    construct.set_field(directory, "confinement.mode", "require")
    provider = FakeModelProvider([text_response("done")])

    with pytest.raises(ConfinementUnavailable, match="no OS sandbox is available"):
        runner.run_harness(
            directory,
            "go",
            provider=provider,
            literal_input=True,
            ingest=False,
        )

    assert provider.calls == []


def test_a_relative_working_directory_still_resolves(tmp_path: Path, monkeypatch):
    # A backend resolves its own arguments against its own cwd, so a relative
    # path handed to run_confined must be made absolute before it is wrapped.
    (tmp_path / "work").mkdir()
    monkeypatch.chdir(tmp_path)
    result = run_confined(
        ["/bin/sh", "-c", "echo made > here.txt"],
        cwd=Path("work"),
        config=ConfinementConfig(),
    )
    assert result.returncode == 0, result.output
    assert (tmp_path / "work" / "here.txt").read_text() == "made\n"


def test_home_points_at_a_private_scratch_directory(tmp_path: Path, monkeypatch):
    home, work = tmp_path / "home", tmp_path / "work"
    (home / ".aws").mkdir(parents=True)
    (home / ".aws" / "credentials").write_text("aws_secret_access_key = nope")
    work.mkdir()
    monkeypatch.setenv("HOME", str(home))

    result = run_confined(
        ["/bin/sh", "-c", "echo HOME=$HOME; ls -a $HOME"],
        cwd=work,
        config=ConfinementConfig(),
    )
    # `~` resolves to scratch, not to the operator's home, on every platform —
    # so a tool that looks for credentials by convention finds nothing.
    assert f"HOME={home}" not in result.output
    assert "credentials" not in result.output
    # The capture files live outside what the command can see or rewrite.
    assert "stdout" not in result.output


@sandboxed
def test_the_sandbox_masks_the_home_directory(tmp_path: Path, monkeypatch):
    """The mask is asserted on the policy, not on a fake home under /tmp.

    A private ``/tmp`` already hides anything a test could put there, so a
    runtime check would pass for the wrong reason. What matters is that the
    generated policy names the operator's home — and stops naming it when a
    build opts out.
    """
    home, work = tmp_path / "home", tmp_path / "work"
    home.mkdir()
    work.mkdir()
    monkeypatch.setenv("HOME", str(home))

    if confine.available_backend() == confine.BWRAP:
        hidden = confine._bwrap_argv(
            ["/bin/true"], cwd=work, config=ConfinementConfig(), scratch=tmp_path, mask=[]
        )
        exposed = confine._bwrap_argv(
            ["/bin/true"],
            cwd=work,
            config=ConfinementConfig(hide_home=False),
            scratch=tmp_path,
            mask=[],
        )
        adjacent = list(zip(hidden, hidden[1:], strict=False))
        assert ("--tmpfs", str(home)) in adjacent
        assert str(home) not in exposed
    else:  # pragma: no cover - macOS only
        hidden = confine._sandbox_profile(work, ConfinementConfig(), tmp_path, [])
        exposed = confine._sandbox_profile(
            work, ConfinementConfig(hide_home=False), tmp_path, []
        )
        assert f'(deny file-read* (subpath "{home}"))' in hidden
        assert str(home) not in exposed


@sandboxed
def test_the_sandboxs_tmp_is_private(tmp_path: Path):
    # Another process's temporary files are not part of the machine a confined
    # command gets to see, even though the rest of the filesystem is readable.
    leftover = Path("/tmp") / "hiveloom-confine-probe"
    leftover.write_text("someone else's scratch")
    try:
        result = run_confined(
            ["/bin/sh", "-c", f"cat {leftover} || echo BLOCKED"],
            cwd=tmp_path,
            config=ConfinementConfig(),
        )
        assert "BLOCKED" in result.output
        assert "someone else" not in result.output
    finally:
        leftover.unlink()


# --------------------------------------------------------------------------- #
# The runtime's own state is not part of the machine a command sees
# --------------------------------------------------------------------------- #
def _shell_harness(tmp_path: Path) -> Path:
    """A harness whose shell may run the file-reading commands, with any args."""
    directory = tmp_path / "leaky"
    construct.init_harness(directory, name="leaky", task="T")
    construct.set_field(directory, "loop.require_verification", "false")
    (directory / "tools").mkdir(exist_ok=True)
    (directory / "tools" / "big.py").write_text(
        "from hiveloom.tools import tool\n\n\n"
        '@tool(description="Emit a large result.")\n'
        'def big(label: str = "") -> str:\n'
        # Assembled at run time so the marker never appears in this file (or
        # its .pyc): a walk of the harness must fail on the *stored result*,
        # not merely on a literal the compiler folded into the tool's source.
        '    marker = "".join(["SECRET", "-FROM-RUN-ONE"])\n'
        '    return marker + " " + ("z" * 40000)\n'
    )
    construct.set_field(
        directory,
        "tools",
        '[{code: "tools/big.py:big", description: "Emit a large result."},'
        " {builtin: shell, commands: ["
        "{argv: [ls], allow_extra_args: true}, {argv: [grep], allow_extra_args: true}]}]",
    )
    return directory


def _spill_a_result(harness: Path) -> None:
    runner.run_harness(
        harness,
        "run one",
        provider=FakeModelProvider(
            [tool_response("big", {}, call_id="c1"), text_response("done")]
        ),
        literal_input=True,
        ingest=False,
    )


def _shell(harness: Path, command: str) -> str:
    provider = FakeModelProvider(
        [tool_response("shell", {"command": command}, call_id="s1"), text_response("done")]
    )
    runner.run_harness(harness, "run two", provider=provider, literal_input=True, ingest=False)
    blocks = provider.calls[1]["messages"][-1]["content"]
    return "".join(b["content"] for b in blocks if b["type"] == "tool_result")


def test_the_shell_cannot_name_the_runtimes_own_state(tmp_path: Path):
    # The reported bypass: an allowlisted `ls`/`grep` walking .hiveloom to find
    # an earlier run's spill handle and read the result it stands for.
    harness = _shell_harness(tmp_path)
    _spill_a_result(harness)

    listing = _shell(harness, "ls -la .hiveloom/traces/spill")
    assert "runtime state" in listing
    assert "tr_" not in listing

    grepped = _shell(harness, "grep -o SECRET-FROM-RUN-ONE -r .hiveloom")
    assert "SECRET-FROM-RUN-ONE" not in grepped


@sandboxed
def test_the_sandbox_hides_runtime_state_from_a_walk_that_never_names_it(tmp_path: Path):
    # Refusing arguments cannot catch `grep -r .` from the harness root; the
    # kernel mask is what makes the directory not be there at all.
    harness = _shell_harness(tmp_path)
    _spill_a_result(harness)

    walked = _shell(harness, "grep -r SECRET-FROM-RUN-ONE .")
    assert "SECRET-FROM-RUN-ONE" not in walked
    assert "exit=" in walked


def test_a_spilled_result_is_not_world_readable(tmp_path: Path):
    harness = _shell_harness(tmp_path)
    _spill_a_result(harness)
    store = harness / ".hiveloom" / "traces" / "spill"

    assert store.stat().st_mode & 0o077 == 0
    for path in store.iterdir():
        assert path.stat().st_mode & 0o077 == 0, path
    journal = next((harness / ".hiveloom" / "traces").glob("run_*.jsonl"))
    assert journal.stat().st_mode & 0o077 == 0


def test_an_ordinary_argument_still_passes(tmp_path: Path):
    # The refusal is about the runtime's state, not about paths in general.
    harness = _shell_harness(tmp_path)
    assert "harness.yaml" in _shell(harness, "ls -1 .")
    assert "exit=0" in _shell(harness, "grep -c name harness.yaml")


def test_an_endless_writer_is_bounded_by_the_timeout_not_by_storage(tmp_path: Path):
    # `yes` never ends. Draining keeps memory and disk flat, and the timeout is
    # what ends the command — the cap governs what is kept, not how long it runs.
    started = time.monotonic()
    result = run_confined(
        ["/bin/sh", "-c", "yes flooding"],
        cwd=tmp_path,
        config=ConfinementConfig(max_output_bytes=2000, timeout_seconds=2),
    )
    assert result.timed_out
    assert result.discarded_bytes > 0
    assert time.monotonic() - started < 10
    assert len(result.stdout.encode()) <= 2100


def test_the_shell_argv_matches_the_platform():
    from hiveloom.confine import shell_argv

    argv = shell_argv("echo hi")
    assert argv[-1] == "echo hi"
    assert argv[0].endswith(("sh", "cmd.exe"))


@sandboxed
def test_a_mask_never_swallows_the_working_directory(tmp_path: Path):
    # A harness that keeps its traces at `.` would otherwise mask the directory
    # the command runs in, and every spawn would fail.
    (tmp_path / "file.txt").write_text("visible")
    result = run_confined(
        ["/bin/sh", "-c", "ls"],
        cwd=tmp_path,
        config=ConfinementConfig(),
        mask=[tmp_path, tmp_path.parent],
    )
    assert result.returncode == 0
    assert "file.txt" in result.stdout


def test_forged_and_leaked_handles_grant_nothing(tmp_path: Path):
    # Even with the store's real layout in hand, a handle is not a capability:
    # a later run resolves only what it minted or was explicitly granted.
    from hiveloom.context.spill import SpillError, SpillStore
    from hiveloom.spec.schema import ToolResultsConfig

    config = ToolResultsConfig(
        max_inline_bytes=100, preview_head_bytes=10, preview_tail_bytes=10
    )
    root = tmp_path / "spill"
    minted = SpillStore(root, run_id="run_one", config=config).spill(
        tool="t", content="s" * 400
    )
    later = SpillStore(root, run_id="run_two", config=config)

    for handle in (minted.handle, "tr_" + "0" * 16, "../run_one/" + minted.handle, ""):
        with pytest.raises(SpillError):
            later.read(handle)


def test_the_journal_states_what_was_enforced_not_where(tmp_path: Path):
    directory = tmp_path / "h"
    construct.init_harness(directory, name="stated", task="Do a thing.")
    construct.set_field(directory, "loop.require_verification", "false")
    result = runner.run_harness(
        directory,
        "go",
        provider=FakeModelProvider([text_response("done")]),
        literal_input=True,
        ingest=False,
    )

    payload = read_events(result.trace_path)[0]["payload"]
    facts = payload["confinement"]
    assert set(facts) >= {
        "backend",
        "filesystem_isolated",
        "runtime_state_hidden",
        "network_isolated",
    }
    assert payload["egress"]["policy"] == "redact"
    boundary = payload["prompt_injection_boundary"]
    assert boundary["safe_for_untrusted_input"]
    assert boundary["provider_egress_active"]
    # A journal is shareable; the layout of the machine that produced it is not
    # part of the run.
    assert str(tmp_path) not in json.dumps(facts)
    assert isinstance(facts["private_paths"], int)


def test_diagnostic_does_not_claim_safety_when_egress_is_disabled(tmp_path: Path):
    directory = tmp_path / "h"
    construct.init_harness(directory, name="unsafe", task="Do a thing.")
    construct.set_field(directory, "egress.mode", '"off"')

    result = cli.invoke(app, ["confinement", str(directory), "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert not payload["provider_egress_active"]
    assert not payload["prompt_injection_boundary"]["safe_for_untrusted_input"]
