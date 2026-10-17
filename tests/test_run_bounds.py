"""Run bounds: the HIVELOOM_HOME rule, ``model.max_retries`` and the run deadline.

A deadline has to bound a model call from the inside — each attempt's timeout
and every backoff wait — because the wall-clock guardrail only looks before a
call starts. These tests use monkeypatched ``urlopen``, tiny budgets and fake
providers; none sleeps for more than a fraction of a second.
"""

from __future__ import annotations

import json
import threading
import time

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from hiveloom import construct, deadline, ext, paths, runner, trust
from hiveloom.cli import app
from hiveloom.errors import ExitCode, SpecError
from hiveloom.logging.hive import default_db_path
from hiveloom.loop.control import RunControl
from hiveloom.models import openai_compat
from hiveloom.models.fake import FakeModelProvider, text_response, tool_response
from hiveloom.models.openai_compat import OpenAICompatProvider
from hiveloom.models.provider import ModelConfig, ModelProvider
from hiveloom.spec.schema import ModelConfig as SpecModelConfig

cli = CliRunner()


# ─── HIVELOOM_HOME / HIVELOOM_DB ─────────────────────────────────────────────

def test_empty_home_is_unset(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HIVELOOM_HOME", "")
    assert paths.hiveloom_home() == (tmp_path / ".hiveloom").resolve()


def test_relative_home_is_made_absolute(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HIVELOOM_HOME", "state")
    home = paths.hiveloom_home()
    assert home.is_absolute()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert home == (tmp_path / "state").resolve()


def test_trust_store_follows_the_configured_home(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HIVELOOM_HOME", "")
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    assert trust.trust_store_path() == (tmp_path / "user" / ".hiveloom" / "trust.json").resolve()
    monkeypatch.setenv("HIVELOOM_HOME", str(tmp_path / "custom"))
    assert trust.trust_store_path().parent == (tmp_path / "custom").resolve()


def test_empty_db_override_is_unset(monkeypatch, tmp_path):
    monkeypatch.setenv("HIVELOOM_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HIVELOOM_DB", "")
    assert default_db_path() == (tmp_path / "home").resolve() / "hive.db"


# ─── model.max_retries ───────────────────────────────────────────────────────

def test_max_retries_is_optional_and_bounded():
    assert SpecModelConfig().max_retries is None
    assert SpecModelConfig(max_retries=0).max_retries == 0
    for bad in (-1, 11):
        with pytest.raises(ValidationError):
            SpecModelConfig(max_retries=bad)


def test_max_retries_reaches_the_provider_config(harness_dir):
    construct.set_value(harness_dir, "model.max_retries", 2)
    seen: list[ModelConfig] = []

    class Recording(FakeModelProvider):
        def complete(self, **kwargs):
            seen.append(kwargs["config"])
            return super().complete(**kwargs)

    runner.run_harness(
        harness_dir, "hi", provider=Recording([text_response("done")]), ingest=False
    )
    assert seen and seen[0].max_retries == 2


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(
            {"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]}
        ).encode()


def _complete(provider, **config):
    return provider.complete(
        system="s",
        messages=[{"role": "user", "content": "x"}],
        tools=[],
        config=ModelConfig(id="m", **config),
    )


def _failing_urlopen(monkeypatch, timeouts: list[float]):
    def urlopen(request, timeout=0):
        timeouts.append(timeout)
        raise TimeoutError("slow")

    monkeypatch.setattr("hiveloom.models.openai_compat.urlrequest.urlopen", urlopen)


@pytest.mark.parametrize(("retries", "attempts"), [(None, 4), (0, 1), (1, 2)])
def test_openai_compat_attempts_follow_max_retries(monkeypatch, retries, attempts):
    timeouts: list[float] = []
    _failing_urlopen(monkeypatch, timeouts)
    provider = OpenAICompatProvider("http://localhost:9", sleep=lambda _s: None)
    with pytest.raises(RuntimeError):
        _complete(provider, max_retries=retries)
    assert len(timeouts) == attempts


# ─── run deadline: provider attempts and backoff ─────────────────────────────

def test_no_deadline_leaves_timeouts_and_sleeps_alone(monkeypatch):
    timeouts: list[float] = []
    sleeps: list[float] = []
    _failing_urlopen(monkeypatch, timeouts)
    provider = OpenAICompatProvider("http://localhost:9", timeout=7, sleep=sleeps.append)
    with pytest.raises(RuntimeError):
        _complete(provider)
    assert timeouts == [7, 7, 7, 7]
    assert sleeps == [1.0, 2.0, 4.0]


def test_retries_share_one_deadline(monkeypatch):
    timeouts: list[float] = []

    def urlopen(request, timeout=0):
        timeouts.append(timeout)
        time.sleep(0.05)
        raise TimeoutError("slow")

    monkeypatch.setattr("hiveloom.models.openai_compat.urlrequest.urlopen", urlopen)
    monkeypatch.setattr(openai_compat, "_BASE_DELAY", 0.01)
    provider = OpenAICompatProvider("http://localhost:9", timeout=120)
    started = time.monotonic()
    with deadline.run_bounds(deadline=started + 0.3):
        with pytest.raises(deadline.DeadlineExceeded):
            _complete(provider, max_retries=10)
    assert time.monotonic() - started < 1.0
    assert all(t <= 0.3 for t in timeouts)
    assert timeouts == sorted(timeouts, reverse=True), "each attempt gets less time"
    assert len(timeouts) > 2


def test_backoff_is_cut_short_by_the_deadline(monkeypatch):
    timeouts: list[float] = []
    _failing_urlopen(monkeypatch, timeouts)
    provider = OpenAICompatProvider("http://localhost:9")
    started = time.monotonic()
    with deadline.run_bounds(deadline=started + 0.2, stop=threading.Event()):
        with pytest.raises(deadline.DeadlineExceeded):
            _complete(provider)
    assert time.monotonic() - started < 1.0, "the 1s backoff must not run in full"
    assert len(timeouts) == 1


def test_backoff_is_cut_short_by_a_stop_request(monkeypatch):
    timeouts: list[float] = []
    _failing_urlopen(monkeypatch, timeouts)
    stop = threading.Event()
    timer = threading.Timer(0.1, stop.set)
    provider = OpenAICompatProvider("http://localhost:9")
    started = time.monotonic()
    timer.start()
    with deadline.run_bounds(stop=stop):
        with pytest.raises(deadline.RunStopped):
            _complete(provider)
    assert time.monotonic() - started < 0.8
    assert len(timeouts) == 1


def test_a_stop_blocks_the_next_attempt(monkeypatch):
    timeouts: list[float] = []
    _failing_urlopen(monkeypatch, timeouts)
    stop = threading.Event()
    stop.set()
    with deadline.run_bounds(stop=stop):
        with pytest.raises(deadline.RunStopped):
            _complete(OpenAICompatProvider("http://localhost:9"))
    assert timeouts == []


def test_remaining_seconds_contract():
    assert deadline.remaining_seconds(5.0) == 5.0
    with deadline.run_bounds(deadline=time.monotonic() + 60):
        assert 0 < deadline.remaining_seconds(5.0) <= 5.0
        assert deadline.remaining_seconds(1000.0) <= 60
    with deadline.run_bounds(deadline=time.monotonic() - 1):
        with pytest.raises(deadline.DeadlineExceeded):
            deadline.remaining_seconds(5.0)
    assert ext.remaining_seconds is deadline.remaining_seconds


def test_nested_bounds_only_tighten():
    now = time.monotonic()
    with deadline.run_bounds(deadline=now + 10):
        with deadline.run_bounds(deadline=now + 100) as inner:
            assert inner.deadline == now + 10
        with deadline.run_bounds(deadline=now + 1) as inner:
            assert inner.deadline == now + 1


def test_a_parent_stop_reaches_a_nested_run():
    parent_stop, child_stop = threading.Event(), threading.Event()
    with deadline.run_bounds(stop=parent_stop):
        with deadline.run_bounds(stop=child_stop):
            threading.Timer(0.05, parent_stop.set).start()
            started = time.monotonic()
            with pytest.raises(deadline.RunStopped):
                deadline.wait_backoff(5)
            assert time.monotonic() - started < 1.0


def test_an_injected_sleep_is_honoured_under_a_stop_event():
    slept: list[float] = []
    with deadline.run_bounds(deadline=time.monotonic() + 60, stop=threading.Event()):
        deadline.wait_backoff(2.0, slept.append)
    assert slept == [2.0]


# ─── run deadline: the runner ────────────────────────────────────────────────

class _BackoffProvider(ModelProvider):
    """A model call that is only ever 'retrying': it waits as a provider would."""

    def complete(self, **_):
        deadline.wait_backoff(30)
        raise AssertionError("the backoff should have ended the run")


def test_caller_deadline_ends_the_run_as_a_guardrail_halt(harness_dir):
    started = time.monotonic()
    result = runner.run_harness(
        harness_dir, "hi", provider=_BackoffProvider(), ingest=False, deadline_seconds=0.2
    )
    assert time.monotonic() - started < 2.0
    assert result.status == "guardrail_halt"
    assert "wall clock" in result.reason
    assert result.runtime_config["requested"]["deadline_seconds"] == 0.2


def test_caller_deadline_cannot_extend_the_guardrail(harness_dir):
    construct.add_guardrail(harness_dir, "max_wall_clock_seconds", 1)
    started = time.monotonic()
    result = runner.run_harness(
        harness_dir, "hi", provider=_BackoffProvider(), ingest=False, deadline_seconds=600
    )
    assert time.monotonic() - started < 3.0
    assert result.status == "guardrail_halt"
    assert result.reason == "wall clock exceeded 1s"


def test_caller_deadline_tightens_the_guardrail(harness_dir):
    construct.add_guardrail(harness_dir, "max_wall_clock_seconds", 600)
    started = time.monotonic()
    result = runner.run_harness(
        harness_dir, "hi", provider=_BackoffProvider(), ingest=False, deadline_seconds=0.2
    )
    assert time.monotonic() - started < 2.0
    assert result.reason == "wall clock deadline exceeded 0.2s"


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan")])
def test_deadline_must_be_positive_and_finite(harness_dir, value):
    with pytest.raises(SpecError):
        runner.run_harness(
            harness_dir, "hi", provider=FakeModelProvider([]), deadline_seconds=value
        )


def test_no_deadline_leaves_the_run_unchanged(harness_dir):
    result = runner.run_harness(
        harness_dir, "hi", provider=FakeModelProvider([text_response("done")]), ingest=False
    )
    assert result.status == "success"
    assert "deadline_seconds" not in result.runtime_config["requested"]


def test_stop_during_backoff_ends_the_run_as_stopped(harness_dir):
    control = RunControl()
    threading.Timer(0.1, control.request_stop, args=("host heartbeat",)).start()
    started = time.monotonic()
    result = runner.run_harness(
        harness_dir, "hi", provider=_BackoffProvider(), ingest=False, control=control
    )
    assert time.monotonic() - started < 2.0
    assert result.status == "stopped"
    assert result.reason == "host heartbeat"


def test_deadline_works_off_the_main_thread(harness_dir):
    box: dict[str, object] = {}

    def target():
        box["result"] = runner.run_harness(
            harness_dir, "hi", provider=_BackoffProvider(), ingest=False, deadline_seconds=0.2
        )

    thread = threading.Thread(target=target)
    thread.start()
    thread.join(5)
    assert not thread.is_alive()
    assert box["result"].status == "guardrail_halt"


def test_deadline_does_not_leak_into_later_runs(harness_dir):
    runner.run_harness(
        harness_dir, "hi", provider=_BackoffProvider(), ingest=False, deadline_seconds=0.1
    )
    assert deadline.remaining_seconds(5.0) == 5.0


def test_cli_deadline_expiry_exits_like_a_guardrail_halt(harness_dir):
    api = ext.ExtensionAPI(source="test:deadline-cli")
    api.register_provider("slowgw", lambda _ctx: _BackoffProvider(), open_catalog=True)
    result = cli.invoke(
        app,
        [
            "run", str(harness_dir), "--input-text", "hi", "--provider", "slowgw",
            "--model", "m", "--deadline", "0.2", "--json",
        ],
    )
    assert result.exit_code == ExitCode.GUARDRAIL_HALT, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "guardrail_halt"
    assert "wall clock" in payload["reason"]
    assert payload["runtime_config"]["requested"]["deadline_seconds"] == 0.2


def test_cli_rejects_a_non_positive_deadline(harness_dir):
    result = cli.invoke(
        app, ["run", str(harness_dir), "--input-text", "hi", "--deadline", "0", "--json"]
    )
    assert result.exit_code == ExitCode.SPEC_ERROR


# ─── tools ───────────────────────────────────────────────────────────────────

def test_http_get_timeout_follows_the_deadline(monkeypatch, tmp_path):
    from hiveloom.tools.builtin import HttpGetTool

    seen: list[float] = []

    class Opener:
        def open(self, request, timeout=0):
            seen.append(timeout)
            raise TimeoutError

    monkeypatch.setattr("hiveloom.tools.builtin.urlrequest.build_opener", lambda *_: Opener())
    monkeypatch.setattr("hiveloom.tools.builtin._validate_public_http_url", lambda *_: None)
    tool = HttpGetTool(tmp_path)
    with deadline.run_bounds(deadline=time.monotonic() + 5):
        with pytest.raises(TimeoutError):
            tool.run(url="https://example.com")
    assert 0 < seen[0] <= 5
    with pytest.raises(TimeoutError):
        tool.run(url="https://example.com")
    assert seen[1] == 30


# ─── Claude provider ─────────────────────────────────────────────────────────

def _claude_with_failing_sdk(monkeypatch):
    import sys
    import types

    seen: dict[str, list] = {"options": [], "attempts": []}
    module = types.ModuleType("anthropic")

    class _Transient(Exception):
        pass

    for name in ("BadRequestError", "RateLimitError", "InternalServerError"):
        setattr(module, name, type(name, (Exception,), {}))
    module.APIConnectionError = _Transient

    class Anthropic:
        def __init__(self, api_key=None):
            self.messages = types.SimpleNamespace(create=self._create)

        def with_options(self, **options):
            seen["options"].append(options)
            return self

        def _create(self, **kwargs):
            seen["attempts"].append(kwargs)
            raise _Transient("down")

    module.Anthropic = Anthropic
    monkeypatch.setitem(sys.modules, "anthropic", module)
    from hiveloom.models.claude import ClaudeProvider

    return ClaudeProvider(api_key="test", sleep=lambda _s: None), seen


def test_claude_max_retries_sets_attempts_and_silences_sdk_retries(monkeypatch):
    provider, seen = _claude_with_failing_sdk(monkeypatch)
    with pytest.raises(RuntimeError):
        _complete(provider, max_retries=1)
    assert len(seen["attempts"]) == 2
    assert all(options == {"max_retries": 0} for options in seen["options"])


def test_claude_leaves_the_sdk_alone_without_retries_or_deadline(monkeypatch):
    provider, seen = _claude_with_failing_sdk(monkeypatch)
    with pytest.raises(RuntimeError):
        _complete(provider)
    assert len(seen["attempts"]) == 4
    assert seen["options"] == []


def test_claude_attempt_timeout_follows_the_deadline(monkeypatch):
    provider, seen = _claude_with_failing_sdk(monkeypatch)
    with deadline.run_bounds(deadline=time.monotonic() + 30):
        with pytest.raises(RuntimeError):
            _complete(provider, max_retries=0)
    (options,) = seen["options"]
    assert options["max_retries"] == 0
    assert 0 < options["timeout"] <= 30


def test_deadline_reached_inside_a_tool_halts_the_run(harness_dir):
    (harness_dir / "tools" / "slow.py").write_text(
        "import time\n"
        "from hiveloom import ext\n\n"
        "def slow(seconds: float) -> str:\n"
        "    time.sleep(seconds)\n"
        "    ext.remaining_seconds(5.0)\n"
        "    return 'done'\n"
    )
    construct.add_tool(harness_dir, code="tools/slow.py:slow", description="Slow.")
    provider = FakeModelProvider(
        [tool_response("slow", {"seconds": 0.3}), text_response("unreachable")]
    )
    result = runner.run_harness(
        harness_dir, "hi", provider=provider, ingest=False, deadline_seconds=0.2
    )
    assert result.status == "guardrail_halt"
    assert result.reason == "wall clock deadline exceeded 0.2s"
    events = [json.loads(line)["type"] for line in open(result.trace_path)]
    assert "tool_result" not in events, "the expiry is not handed to the model as a tool error"
