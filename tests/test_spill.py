"""Retrievable tool-output spilling: previews in context, the whole thing on disk."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hiveloom import construct, runner
from hiveloom import fork as fork_mod
from hiveloom.context.spill import HANDLE_RE, SpillError, SpillStore
from hiveloom.logging.journal import read_events
from hiveloom.models.fake import FakeModelProvider, text_response, tool_response
from hiveloom.spec.schema import ToolResultsConfig

# A tool whose output is far bigger than the inline budget, with a distinctive
# needle in the middle — the part plain truncation would destroy.
BIG_TOOL = '''
from hiveloom.tools import tool


@tool(description="Emit a large report.")
def report(marker: str = "NEEDLE") -> str:
    body = "\\n".join(f"line {i} filler filler filler" for i in range(4000))
    return f"REPORT HEAD\\n{body}\\n{marker} the answer is 42\\n{body}\\nREPORT TAIL"
'''

SECRET_TOOL = '''
from hiveloom.tools import tool


@tool(description="Emit a large report containing a secret.")
def leaky(label: str = "report") -> str:
    filler = "x" * 40000
    return f"{filler}\\ntoken=sk-live-abcdef\\n{filler}"
'''


class HandleAwareProvider(FakeModelProvider):
    """A scripted provider that can aim a call at values only known at runtime.

    A handle is minted during the run, so a fixed script cannot contain one.
    ``{{handle}}`` in a scripted tool input is replaced with the handle quoted
    in the conversation so far, and ``{{match_offset}}`` with the first byte
    offset a search reported — which is exactly how a model would chain the
    two retrieval tools.
    """

    def complete(self, **kwargs):
        response = super().complete(**kwargs)
        seen = _tool_results_in(kwargs["messages"])
        for call in response.tool_calls:
            call.input = {
                key: self._resolve(value, seen) for key, value in call.input.items()
            }
        return response

    @staticmethod
    def _resolve(value, seen: str):
        if value == "{{handle}}":
            match = HANDLE_RE.search(seen)
            return match.group(0) if match else value
        if value == "{{match_offset}}":
            return int(seen.split("byte ")[1].split(":")[0])
        return value


def _tool_results_in(messages) -> str:
    return "".join(
        block["content"]
        for message in messages
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if block.get("type") == "tool_result"
    )


def _harness(tmp_path: Path, source: str, func: str, **fields: str) -> Path:
    directory = tmp_path / "h"
    construct.init_harness(directory, name="spill-harness", task="Produce a summary.")
    (directory / "tools").mkdir(exist_ok=True)
    (directory / "tools" / f"{func}.py").write_text(source)
    construct.add_tool(
        directory, code=f"tools/{func}.py:{func}", description=f"The {func} tool."
    )
    construct.set_field(directory, "loop.require_verification", "false")
    for path, value in fields.items():
        construct.set_field(directory, path.replace("__", "."), value)
    return directory


def _tool_result_text(provider: FakeModelProvider, call_index: int = 1) -> str:
    """The tool_result content the model saw on its ``call_index``-th call."""
    blocks = provider.calls[call_index]["messages"][-1]["content"]
    return "".join(b["content"] for b in blocks if b["type"] == "tool_result")


# --------------------------------------------------------------------------- #
# The store
# --------------------------------------------------------------------------- #
def _store(tmp_path: Path, **overrides) -> SpillStore:
    config = ToolResultsConfig(
        max_inline_bytes=overrides.pop("max_inline_bytes", 200),
        preview_head_bytes=overrides.pop("preview_head_bytes", 20),
        preview_tail_bytes=overrides.pop("preview_tail_bytes", 10),
    )
    run_id = overrides.pop("run_id", "run_1")
    return SpillStore(tmp_path / "spill", run_id=run_id, config=config, **overrides)


def _stored_objects(trace_path) -> list[Path]:
    """Every spilled object under a run's trace directory, in any run folder."""
    return sorted((Path(trace_path).parent / "spill").rglob("*.txt"))


def test_a_result_within_budget_is_not_spilled(tmp_path: Path):
    assert _store(tmp_path).spill(tool="t", content="small") is None


def test_a_preview_keeps_the_head_and_the_tail(tmp_path: Path):
    store = _store(tmp_path)
    content = "HEAD" + ("m" * 500) + "TAIL"
    record = store.spill(tool="t", content=content)

    assert record is not None
    assert record.preview.startswith("HEAD")
    assert record.preview.endswith("TAIL")
    assert HANDLE_RE.fullmatch(record.handle)
    assert record.handle in record.preview
    assert record.total_bytes == len(content)
    # Everything not in the head or the tail is accounted for as omitted.
    assert record.omitted_bytes == len(content) - 20 - 10
    assert str(record.omitted_bytes) in record.preview


def test_the_whole_result_is_stored_and_readable(tmp_path: Path):
    store = _store(tmp_path)
    content = "".join(f"{i:05d}" for i in range(200))
    record = store.spill(tool="t", content=content)

    read = store.read(record.handle, offset=100, limit=50)
    assert content[100:150] in read
    assert f"bytes 100-150 of {len(content)}" in read
    assert "continue at offset 150" in read


def test_reading_past_the_end_says_so_instead_of_failing(tmp_path: Path):
    store = _store(tmp_path)
    record = store.spill(tool="t", content="z" * 500)
    assert "past the end" in store.read(record.handle, offset=9999)


def test_a_read_is_capped_at_the_inline_budget(tmp_path: Path):
    store = _store(tmp_path, max_inline_bytes=200)
    record = store.spill(tool="t", content="z" * 5000)
    body = store.read(record.handle, offset=0, limit=99999)
    assert "bytes 0-200 of 5000" in body


def test_search_reports_byte_offsets_a_read_can_use(tmp_path: Path):
    store = _store(tmp_path)
    content = ("a" * 1000) + "FINDME" + ("b" * 1000)
    record = store.spill(tool="t", content=content)

    found = store.search(record.handle, "findme")
    assert "1 match(es)" in found
    assert "byte 1000" in found
    assert "FINDME" in store.read(record.handle, offset=1000, limit=6)


def test_search_without_a_match_points_back_at_reading(tmp_path: Path):
    store = _store(tmp_path)
    record = store.spill(tool="t", content="z" * 500)
    assert "no match" in store.search(record.handle, "absent")


def test_a_handle_from_another_run_is_not_readable(tmp_path: Path):
    producer = _store(tmp_path, run_id="run_one")
    record = producer.spill(tool="t", content="z" * 500)

    other = _store(tmp_path, run_id="run_two")  # same storage root, different run
    with pytest.raises(SpillError, match="unknown handle"):
        other.read(record.handle)


def test_quoting_a_handle_in_the_conversation_grants_nothing(tmp_path: Path):
    # The transcript is model-visible text. If mentioning a handle authorized
    # it, a model that once saw one could recover the object in any later fork
    # by quoting it back.
    producer = _store(tmp_path, run_id="run_one")
    record = producer.spill(tool="t", content="z" * 500)
    later = _store(tmp_path, run_id="run_two")

    assert not hasattr(later, "admit_from_messages")
    assert later.handles == []
    with pytest.raises(SpillError, match="grants no access"):
        later.read(record.handle)


def test_inheritance_is_an_explicit_grant_against_real_files(tmp_path: Path):
    producer = _store(tmp_path, run_id="run_one")
    record = producer.spill(tool="t", content="z" * 500)
    heir = _store(tmp_path, run_id="run_two")

    # A grant naming a handle with no object behind it is not a grant.
    assert heir.inherit([record.handle], tmp_path / "spill" / "nowhere") == []
    assert heir.inherit(["tr_0000000000000000"], tmp_path / "spill" / "run_one") == []
    assert heir.inherit([record.handle], tmp_path / "spill" / "run_one") == [record.handle]
    assert "z" in heir.read(record.handle)


def test_inherited_bytes_are_rechecked_after_authorization(tmp_path: Path):
    producer = _store(tmp_path, run_id="run_one")
    record = producer.spill(tool="t", content="z" * 500)
    heir = _store(tmp_path, run_id="run_two")
    manifest = {
        "handle": record.handle,
        "sha256": record.sha256,
        "bytes": record.total_bytes,
    }
    source = tmp_path / "spill" / "run_one"
    assert heir.inherit([manifest], source) == [record.handle]
    (source / f"{record.handle}.txt").write_text("substituted", encoding="utf-8")

    with pytest.raises(SpillError, match="no longer matches"):
        heir.read(record.handle)


def test_an_invented_handle_is_refused(tmp_path: Path):
    store = _store(tmp_path)
    store.spill(tool="t", content="z" * 500)
    with pytest.raises(SpillError, match="unknown handle"):
        store.read("tr_0000000000000000")


def test_a_failed_write_keeps_the_result_inline(tmp_path: Path):
    # An unwritable root: storage is best effort, and losing the result would
    # be a far worse failure than losing the context saving.
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory")
    store = SpillStore(
        blocker / "spill",
        run_id="run_1",
        config=ToolResultsConfig(
            max_inline_bytes=10, preview_head_bytes=4, preview_tail_bytes=4
        ),
    )
    assert store.spill(tool="t", content="z" * 500) is None


def test_redaction_applies_before_the_write(tmp_path: Path):
    store = _store(tmp_path, redact=lambda text: text.replace("sk-live", "[REDACTED]"))
    record = store.spill(tool="t", content=("z" * 500) + "sk-live-abc")

    assert "sk-live" not in record.path.read_text(encoding="utf-8")
    assert "sk-live" not in store.read(record.handle, offset=400, limit=200)


# --------------------------------------------------------------------------- #
# In a run
# --------------------------------------------------------------------------- #
def test_a_large_tool_result_reaches_the_model_as_a_preview(tmp_path: Path):
    harness = _harness(tmp_path, BIG_TOOL, "report")
    provider = FakeModelProvider(
        [tool_response("report", {}, call_id="c1"), text_response("done")]
    )
    result = runner.run_harness(
        harness, "go", provider=provider, literal_input=True, ingest=False
    )

    assert result.status == "success"
    seen = _tool_result_text(provider)
    assert "[hiveloom spill]" in seen
    assert seen.startswith("REPORT HEAD")
    # The tail survives, which plain leading-character truncation destroyed.
    assert seen.rstrip().endswith("REPORT TAIL")
    assert len(seen) < 20000

    handle = HANDLE_RE.search(seen).group(0)
    stored = (
        Path(result.trace_path).parent / "spill" / result.run_id / f"{handle}.txt"
    ).read_text()
    assert "NEEDLE the answer is 42" in stored


def test_the_journal_keeps_the_whole_result(tmp_path: Path):
    harness = _harness(tmp_path, BIG_TOOL, "report")
    result = runner.run_harness(
        harness,
        "go",
        provider=FakeModelProvider(
            [tool_response("report", {}, call_id="c1"), text_response("done")]
        ),
        literal_input=True,
        ingest=False,
    )

    events = read_events(result.trace_path)
    tool_result = next(e for e in events if e["type"] == "tool_result")
    assert "NEEDLE the answer is 42" in tool_result["payload"]["content"]

    spilled = next(e for e in events if e["type"] == "tool_spilled")
    assert spilled["payload"]["name"] == "report"
    assert spilled["payload"]["omitted_bytes"] > 0


def test_the_readers_appear_only_once_something_has_spilled(tmp_path: Path):
    harness = _harness(tmp_path, BIG_TOOL, "report")
    provider = FakeModelProvider(
        [tool_response("report", {}, call_id="c1"), text_response("done")]
    )
    runner.run_harness(harness, "go", provider=provider, literal_input=True, ingest=False)

    before = {t["name"] for t in provider.calls[0]["tools"]}
    after = {t["name"] for t in provider.calls[1]["tools"]}
    assert "read_tool_result" not in before
    assert {"read_tool_result", "search_tool_result"} <= after


def test_the_model_can_read_the_omitted_bytes_back(tmp_path: Path):
    harness = _harness(tmp_path, BIG_TOOL, "report")
    provider = HandleAwareProvider(
        [
            tool_response("report", {}, call_id="c1"),
            tool_response(
                "search_tool_result",
                {"handle": "{{handle}}", "query": "NEEDLE"},
                call_id="c2",
            ),
            tool_response(
                "read_tool_result",
                {"handle": "{{handle}}", "offset": "{{match_offset}}", "limit": 40},
                call_id="c3",
            ),
            text_response("done"),
        ]
    )
    result = runner.run_harness(
        harness, "go", provider=provider, literal_input=True, ingest=False
    )

    assert result.status == "success"
    searched = _tool_result_text(provider, 2)
    assert "1 match(es) for 'NEEDLE'" in searched
    # The read lands on what the search pointed at: content the preview omitted.
    read_back = _tool_result_text(provider, 3)
    assert "NEEDLE the answer is 42" in read_back
    assert "NEEDLE the answer is 42" not in _tool_result_text(provider, 1)


def test_retrieval_results_are_never_themselves_spilled(tmp_path: Path):
    # A read that spilled would hand back a second handle and the model would
    # loop, so retrieval output is exempt however large the read.
    harness = _harness(tmp_path, BIG_TOOL, "report")
    provider = HandleAwareProvider(
        [
            tool_response("report", {}, call_id="c1"),
            tool_response(
                "read_tool_result",
                {"handle": "{{handle}}", "offset": 0, "limit": 999_999},
                call_id="c2",
            ),
            text_response("done"),
        ]
    )
    result = runner.run_harness(
        harness, "go", provider=provider, literal_input=True, ingest=False
    )

    assert len(_stored_objects(result.trace_path)) == 1
    read_back = _tool_result_text(provider, 2)
    assert "[hiveloom spill]" not in read_back
    # ...and it stays bounded by the same budget that caused the spill.
    assert len(read_back.encode("utf-8")) < 20000


def test_spilling_can_be_switched_off(tmp_path: Path):
    harness = _harness(
        tmp_path, BIG_TOOL, "report", context__tool_results__max_inline_bytes="0"
    )
    provider = FakeModelProvider(
        [tool_response("report", {}, call_id="c1"), text_response("done")]
    )
    result = runner.run_harness(
        harness, "go", provider=provider, literal_input=True, ingest=False
    )

    seen = _tool_result_text(provider)
    assert "[hiveloom spill]" not in seen
    assert not (Path(result.trace_path).parent / "spill").exists()
    assert "read_tool_result" not in {t["name"] for t in provider.calls[1]["tools"]}


def test_a_spilled_object_is_redacted_like_the_journal(tmp_path: Path):
    harness = _harness(tmp_path, SECRET_TOOL, "leaky")
    construct.set_field(harness, "logging.redact", '["sk-live-[a-z]+"]')
    provider = FakeModelProvider(
        [tool_response("leaky", {}, call_id="c1"), text_response("done")]
    )
    result = runner.run_harness(
        harness, "go", provider=provider, literal_input=True, ingest=False
    )

    stored = _stored_objects(result.trace_path)[0].read_text(encoding="utf-8")
    assert "sk-live" not in stored
    assert "[REDACTED]" in stored


def test_the_object_records_which_tool_produced_it(tmp_path: Path):
    harness = _harness(tmp_path, BIG_TOOL, "report")
    result = runner.run_harness(
        harness,
        "go",
        provider=FakeModelProvider(
            [tool_response("report", {}, call_id="c1"), text_response("done")]
        ),
        literal_input=True,
        ingest=False,
    )

    meta = json.loads(
        next((Path(result.trace_path).parent / "spill").rglob("*.json")).read_text()
    )
    assert meta["tool"] == "report"
    assert meta["run_id"] == result.run_id
    assert meta["bytes"] > 0


# --------------------------------------------------------------------------- #
# Forking
# --------------------------------------------------------------------------- #
def test_a_fork_carries_the_objects_its_context_quotes(tmp_path: Path):
    harness = _harness(tmp_path, BIG_TOOL, "report")
    parent = runner.run_harness(
        harness,
        "go",
        provider=FakeModelProvider(
            [tool_response("report", {}, call_id="c1"), text_response("done")]
        ),
        literal_input=True,
        ingest=False,
    )

    forked = fork_mod.create_fork(parent.trace_path, tmp_path / "fork")
    seeded = fork_mod.load_fork_context(forked.directory)
    handle = HANDLE_RE.search(json.dumps(seeded)).group(0)
    inherited_dir = forked.directory / ".hiveloom" / "traces" / "spill" / "inherited"
    assert (inherited_dir / f"{handle}.txt").is_file()
    # The grant is recorded in the fork record, written from the verified
    # parent journal — not inferred from the transcript.
    record = fork_mod.load_fork(forked.directory)
    assert record["spill_handles"] == [handle]
    assert record["spill_manifest"][0]["handle"] == handle

    resumed = runner.run_harness(
        forked.directory,
        resume_messages=seeded,
        lineage={
            "parent_run_id": parent.run_id,
            "forked_at_seq": forked.at_seq,
            "spill_handles": record["spill_handles"],
        },
        provider=FakeModelProvider([text_response("done")]),
        ingest=False,
    )

    assert resumed.status == "success"
    inherited = next(
        e for e in read_events(resumed.trace_path) if e["type"] == "spill_inherited"
    )
    assert handle in inherited["payload"]["handles"]
    # The inherited handle is readable, so the resumed run is not stranded with
    # a preview it can never expand.
    tools = {t["name"] for t in _first_tools(resumed.trace_path)}
    assert "read_tool_result" in tools


def test_an_unchained_journal_cannot_authorize_spill_inheritance(tmp_path: Path):
    harness = _harness(tmp_path, BIG_TOOL, "report")
    parent = runner.run_harness(
        harness,
        "go",
        provider=FakeModelProvider(
            [tool_response("report", {}, call_id="c1"), text_response("done")]
        ),
        literal_input=True,
        ingest=False,
    )
    lines = []
    trace_path = Path(parent.trace_path)
    for line in trace_path.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        event.pop("prev", None)
        lines.append(json.dumps(event))
    trace_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    forked = fork_mod.create_fork(parent.trace_path, tmp_path / "unchained-fork")
    record = fork_mod.load_fork(forked.directory)

    assert record["spill_handles"] == []
    assert record["spill_manifest"] == []
    assert any("unchained" in warning for warning in forked.warnings)


def _first_tools(trace_path) -> list[dict]:
    for event in read_events(trace_path):
        if event["type"] == "context_tools":
            return event["payload"]["tools"]
    return []


def test_the_store_is_out_of_reach_of_the_harness_file_tools(tmp_path: Path):
    # A handle is the only way in: spilled objects live under the trace dir,
    # which file_read refuses like the rest of `.hiveloom`.
    from hiveloom.tools.builtin import FileReadTool
    from hiveloom.tools.registry import ToolError

    harness = _harness(tmp_path, BIG_TOOL, "report")
    result = runner.run_harness(
        harness,
        "go",
        provider=FakeModelProvider(
            [tool_response("report", {}, call_id="c1"), text_response("done")]
        ),
        literal_input=True,
        ingest=False,
    )
    stored = _stored_objects(result.trace_path)[0]
    relative = stored.relative_to(harness)

    reader = FileReadTool(harness, trace_dir=Path(".hiveloom/traces"))
    with pytest.raises(ToolError):
        reader.run(path=str(relative))


def test_search_finds_a_match_that_straddles_a_read_window(tmp_path: Path):
    # The object is scanned in windows rather than loaded, so the windows have
    # to overlap: a match sitting exactly on a boundary is the case that a
    # naive chunked scan silently loses.
    from hiveloom.context import spill as spill_module

    store = _store(tmp_path)
    chunk = spill_module._SEARCH_CHUNK_BYTES
    content = ("a" * (chunk - 3)) + "STRADDLE" + ("b" * 2048)
    record = store.spill(tool="t", content=content)

    found = store.search(record.handle, "straddle")
    assert "1 match(es)" in found
    assert f"byte {chunk - 3}" in found
    assert "STRADDLE" in store.read(record.handle, offset=chunk - 3, limit=8)


def test_search_does_not_load_the_object_into_memory(tmp_path: Path):
    import resource

    store = _store(tmp_path)
    record = store.spill(tool="t", content=("q" * (8 * 1024 * 1024)) + "FOUND")
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    assert "1 match(es)" in store.search(record.handle, "FOUND")
    after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Peak RSS may not fall, but searching an 8 MB object must not add 8 MB.
    assert after - before < 4 * 1024  # KiB on Linux


def test_a_query_matching_everything_stops_counting(tmp_path: Path):
    store = _store(tmp_path)
    record = store.spill(tool="t", content="a" * 200_000)
    report = store.search(record.handle, "a")
    assert "+ match(es)" in report


# --------------------------------------------------------------------------- #
# Transforming a stored object in place
# --------------------------------------------------------------------------- #
def _stored(tmp_path: Path, content: str, inline_budget: int = 4000) -> tuple[SpillStore, str]:
    """A store with a generous inline budget already holding ``content``.

    Only an oversized result is ever stored, so the object is minted by a
    narrow-budget store in the same root and then inherited by the store under
    test — which is how a fork reaches one too.
    """
    producer = _store(tmp_path, run_id="producer", max_inline_bytes=40)
    record = producer.spill(tool="t", content=content)
    store = _store(tmp_path, run_id="under_test", max_inline_bytes=inline_budget)
    granted = store.inherit(
        [{"handle": record.handle, "sha256": record.sha256, "bytes": record.total_bytes}],
        tmp_path / "spill" / "producer",
    )
    assert granted == [record.handle]
    return store, record.handle


def _numbered(count: int = 60) -> str:
    return "\n".join(
        f"line {i:03d} {'even' if i % 2 == 0 else 'odd'}" for i in range(count)
    )


def test_transform_returns_a_line_range(tmp_path: Path):
    store, handle = _stored(tmp_path, _numbered())

    out = store.transform(handle, "lines", {"start": 3, "count": 2})
    assert "3: line 002 even" in out
    assert "4: line 003 odd" in out
    assert "line 010" not in out


def test_transform_greps_per_line_with_context(tmp_path: Path):
    store, handle = _stored(tmp_path, _numbered())

    out = store.transform(
        handle, "grep", {"pattern": r"line 00[12] ", "max_matches": 2, "context_lines": 1}
    )
    assert "2: line 001 odd" in out
    assert "3: line 002 even" in out
    # Context lines are marked differently from matches, as grep -C does.
    assert "1- line 000 even" in out


def test_transform_counts_lines_bytes_and_matches(tmp_path: Path):
    store, handle = _stored(tmp_path, _numbered(10))

    out = store.transform(handle, "count", {"pattern": "even"})
    assert "10 lines" in out
    assert "5 lines matching" in out


def test_transform_selects_a_json_path(tmp_path: Path):
    document = {"items": [{"id": f"id-{i}", "pad": "x" * 40} for i in range(6)]}
    store, handle = _stored(tmp_path, json.dumps(document))

    out = store.transform(handle, "json_path", {"path": "$.items[*].id"})
    assert json.loads(out) == [f"id-{i}" for i in range(6)]
    assert "pad" not in out


def test_transform_sorts_and_deduplicates(tmp_path: Path):
    store, handle = _stored(tmp_path, "\n".join(["b", "a", "b", "c"] * 60))

    ordered = store.transform(handle, "sort", {"unique": True})
    assert ordered.splitlines()[1:] == ["a", "b", "c"]
    first_seen = store.transform(handle, "unique", {})
    assert first_seen.splitlines()[1:] == ["b", "a", "c"]


def test_transform_head_and_tail(tmp_path: Path):
    store, handle = _stored(tmp_path, "HEAD" + ("m" * 800) + "TAIL")

    assert store.transform(handle, "head", {"bytes": 4}).endswith("HEAD")
    assert store.transform(handle, "tail", {"bytes": 4}).endswith("TAIL")


def test_transform_concat_joins_authorized_objects_only(tmp_path: Path):
    store, first = _stored(tmp_path, "A" * 300)
    second = _store(tmp_path, run_id="producer", max_inline_bytes=40).spill(
        tool="t", content="B" * 300
    )
    store.inherit(
        [{"handle": second.handle, "sha256": second.sha256, "bytes": second.total_bytes}],
        tmp_path / "spill" / "producer",
    )

    joined = store.transform(first, "concat", {"handles": [second.handle]})
    assert "A" * 300 in joined
    assert "B" * 300 in joined
    # concat reaches nothing this run could not already read.
    with pytest.raises(SpillError, match="unknown handle"):
        store.transform(first, "concat", {"handles": ["tr_0000000000000000"]})


def test_an_oversized_transform_becomes_a_derived_object(tmp_path: Path):
    store, handle = _stored(tmp_path, _numbered(400), inline_budget=200)
    minted: list[tuple] = []
    store.set_on_derived(lambda record, source, op: minted.append((record, source, op)))

    out = store.transform(handle, "grep", {"pattern": "even", "max_matches": 200})
    derived = HANDLE_RE.findall(out)
    assert derived and derived[-1] != handle
    # The derived object is readable under its own handle, so a narrowing that
    # is still too large can be narrowed again rather than abandoned.
    assert "even" in store.read(derived[-1], offset=0, limit=200)
    assert minted and minted[0][1] == handle and minted[0][2] == "grep"

    sidecar = json.loads(
        (store._run_dir / f"{minted[0][0].handle}.json").read_text(encoding="utf-8")
    )
    assert sidecar["derived_from"] == handle
    assert sidecar["op"] == "grep"
    assert sidecar["tool"] == "transform_result"


def test_transform_refuses_an_unknown_op_and_an_unknown_handle(tmp_path: Path):
    store, handle = _stored(tmp_path, _numbered())

    with pytest.raises(SpillError, match="unknown op"):
        store.transform(handle, "eval", {})
    with pytest.raises(SpillError, match="unknown handle"):
        store.transform("tr_0000000000000000", "count", {})


def test_transform_refuses_a_catastrophically_backtracking_pattern(tmp_path: Path):
    store, handle = _stored(tmp_path, _numbered())

    with pytest.raises(SpillError, match="backtrack"):
        store.transform(handle, "grep", {"pattern": "(a+)+$"})
    with pytest.raises(SpillError, match="at most"):
        store.transform(handle, "grep", {"pattern": "a" * 300})
    with pytest.raises(SpillError, match="invalid regular expression"):
        store.transform(handle, "grep", {"pattern": "("})


def test_a_line_without_newlines_is_still_bounded(tmp_path: Path):
    # A stored object need not contain a newline at all; a "line" is split at
    # the bound so a regex never meets an unbounded string.
    from hiveloom.context import spill as spill_module

    store, handle = _stored(tmp_path, "z" * (spill_module._LINE_MAX_BYTES + 100))
    out = store.transform(handle, "count", {})
    assert "2 lines" in out


def test_the_model_can_transform_without_reading_into_context(tmp_path: Path):
    harness = _harness(tmp_path, BIG_TOOL, "report")
    provider = HandleAwareProvider(
        [
            tool_response("report", {}, call_id="c1"),
            tool_response(
                "transform_result",
                {"handle": "{{handle}}", "op": "grep", "pattern": "NEEDLE"},
                call_id="c2",
            ),
            text_response("done"),
        ]
    )
    result = runner.run_harness(
        harness, "go", provider=provider, literal_input=True, ingest=False
    )

    assert result.status == "success"
    transformed = _tool_result_text(provider, 2)
    assert "NEEDLE the answer is 42" in transformed
    # One line out of a 200 KB object, without a read of the object first.
    assert len(transformed.encode("utf-8")) < 2000
    assert "transform_result" in {t["name"] for t in provider.calls[1]["tools"]}


def test_a_derived_object_is_journalled_as_minted(tmp_path: Path):
    # The fork path reads minted handles off the verified journal, so an object
    # created inside a tool call has to appear there like any other spill.
    harness = _harness(tmp_path, BIG_TOOL, "report")
    provider = HandleAwareProvider(
        [
            tool_response("report", {}, call_id="c1"),
            tool_response(
                "transform_result",
                {"handle": "{{handle}}", "op": "lines", "start": 1, "count": 4000},
                call_id="c2",
            ),
            text_response("done"),
        ]
    )
    result = runner.run_harness(
        harness, "go", provider=provider, literal_input=True, ingest=False
    )

    spilled = [e for e in read_events(result.trace_path) if e["type"] == "tool_spilled"]
    derived = [e for e in spilled if e["payload"]["name"] == "transform_result"]
    assert derived, "a derived object must be journalled as minted"
    assert derived[0]["payload"]["op"] == "lines"
    assert derived[0]["payload"]["derived_from"] == spilled[0]["payload"]["handle"]


# --------------------------------------------------------------------------- #
# Handing a stored object to another tool
# --------------------------------------------------------------------------- #
def test_a_stored_result_is_written_out_without_passing_through_context(tmp_path: Path):
    harness = _harness(tmp_path, BIG_TOOL, "report")
    construct.add_tool(harness, builtin="file_write", description="Write a file.")
    provider = HandleAwareProvider(
        [
            tool_response("report", {}, call_id="c1"),
            tool_response(
                "file_write",
                {"path": "out.txt", "content": "{{handle}}"},
                call_id="c2",
            ),
            text_response("done"),
        ]
    )
    result = runner.run_harness(
        harness, "go", provider=provider, literal_input=True, ingest=False
    )

    assert result.status == "success"
    written = (harness / "out.txt").read_text(encoding="utf-8")
    assert "NEEDLE the answer is 42" in written
    assert len(written) > 100_000

    # What the model emitted, and what the journal records of it, is the
    # handle: the expansion exists only for the duration of the call.
    call = next(
        e
        for e in read_events(result.trace_path)
        if e["type"] == "tool_call" and e["payload"]["name"] == "file_write"
    )
    assert HANDLE_RE.fullmatch(call["payload"]["input"]["content"])
    assert "NEEDLE the answer is 42" not in str(provider.calls[2]["messages"])
