"""Streaming for the OpenAI-compat provider: SSE chunks assembled offline.

A fake ``urlopen`` yields server-sent-event lines the way ``http.client``
does (one ``bytes`` line per iteration), so these tests cover assembly,
timeouts and retry classification without a server.
"""

from __future__ import annotations

import io
import json
from urllib import error as urlerror

import pytest

from hiveloom import ext
from hiveloom.models.openai_compat import OpenAICompatProvider, _assemble_stream
from hiveloom.models.provider import ModelConfig


def _sse(*events: dict, done: bool = True) -> list[bytes]:
    lines = []
    for event in events:
        lines.append(b"data: " + json.dumps(event).encode() + b"\n")
        lines.append(b"\n")
    if done:
        lines.append(b"data: [DONE]\n")
    return lines


def _chunk(delta: dict, finish: str | None = None, **extra) -> dict:
    return {
        "id": "chatcmpl-1",
        "model": "m-served",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        **extra,
    }


_USAGE = {"id": "chatcmpl-1", "model": "m-served", "choices": [],
          "usage": {"prompt_tokens": 20, "completion_tokens": 2}}


class _Stream:
    """One response: lines yielded in order, an exception raised after ``fail_after`` lines."""

    def __init__(
        self, lines: list[bytes], fail_after: int | None = None, exc: Exception | None = None
    ):
        self._lines = lines
        self._fail_after = fail_after
        self._exc = exc

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        for i, line in enumerate(self._lines):
            if self._fail_after is not None and i >= self._fail_after:
                raise self._exc
            yield line
        if self._fail_after is not None and self._fail_after >= len(self._lines):
            raise self._exc

    def read(self):  # the non-streaming path reads the body whole
        return b"".join(self._lines)


def _serve(monkeypatch, *responses, requests: list | None = None) -> dict[str, int]:
    """Answer successive requests with ``responses`` (a _Stream or an exception to raise)."""
    calls = {"n": 0}

    def fake_urlopen(request, timeout=0):
        if requests is not None:
            requests.append(json.loads(request.data.decode()))
        response = responses[min(calls["n"], len(responses) - 1)]
        calls["n"] += 1
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr("hiveloom.models.openai_compat.urlrequest.urlopen", fake_urlopen)
    return calls


def _complete(provider, **config):
    return provider.complete(
        system="s",
        messages=[{"role": "user", "content": "x"}],
        tools=[],
        config=ModelConfig(id="m", **config),
    )


def test_text_chunks_assemble_into_one_reply(monkeypatch):
    requests: list = []
    _serve(
        monkeypatch,
        _Stream(_sse(_chunk({"content": "4"}), _chunk({"content": "2"}, finish="stop"), _USAGE)),
        requests=requests,
    )
    response = _complete(OpenAICompatProvider("http://localhost:9"))
    assert response.text == "42"
    assert response.stop_reason == "end_turn"
    assert (response.usage.input_tokens, response.usage.output_tokens) == (20, 2)
    assert response.model == "m-served"
    assert response.provider_request_id == "chatcmpl-1"
    assert requests[0]["stream"] is True
    assert requests[0]["stream_options"] == {"include_usage": True}


def test_a_tool_call_split_across_chunks_is_reassembled(monkeypatch):
    first = {"index": 0, "id": "call_1", "type": "function",
             "function": {"name": "get_weather", "arguments": ""}}
    _serve(
        monkeypatch,
        _Stream(_sse(
            _chunk({"tool_calls": [first]}),
            _chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"city": '}}]}),
            _chunk(
                {"tool_calls": [{"index": 0, "function": {"arguments": '"Rome"}'}}]},
                finish="tool_calls",
            ),
            _USAGE,
        )),
    )
    response = _complete(OpenAICompatProvider("http://localhost:9"))
    assert response.stop_reason == "tool_use"
    assert [(c.id, c.name, c.input) for c in response.tool_calls] == [
        ("call_1", "get_weather", {"city": "Rome"})
    ]


def test_a_tool_call_streamed_whole_with_dict_arguments(monkeypatch):
    # Ollama sends the complete call in one chunk, arguments as an object, no index.
    whole = {"id": "call_o", "type": "function",
             "function": {"name": "get_weather", "arguments": {"city": "Rome"}}}
    _serve(monkeypatch, _Stream(_sse(_chunk({"tool_calls": [whole]}, finish="tool_calls"))))
    response = _complete(OpenAICompatProvider("http://localhost:9"))
    assert [(c.name, c.input) for c in response.tool_calls] == [("get_weather", {"city": "Rome"})]


def test_two_whole_calls_sharing_index_zero_stay_two_calls(monkeypatch):
    # Gemini's OpenAI-compatible endpoint and some gateways put index 0 on
    # every call and send each one whole under its own id.
    read = {"index": 0, "id": "call_a", "type": "function",
            "function": {"name": "file_read", "arguments": '{"path": "a"}'}}
    write = {"index": 0, "id": "call_b", "type": "function",
             "function": {"name": "file_write", "arguments": '{"path": "b"}'}}
    _serve(monkeypatch, _Stream(_sse(
        _chunk({"tool_calls": [read]}),
        _chunk({"tool_calls": [write]}, finish="tool_calls"),
        _USAGE,
    )))
    response = _complete(OpenAICompatProvider("http://localhost:9"))
    assert [(c.id, c.name, c.input) for c in response.tool_calls] == [
        ("call_a", "file_read", {"path": "a"}),
        ("call_b", "file_write", {"path": "b"}),
    ]


def test_a_name_repeated_on_every_fragment_is_not_concatenated(monkeypatch):
    first = {"index": 0, "id": "call_1", "function": {"name": "file_write", "arguments": '{"pa'}}
    second = {"index": 0, "function": {"name": "file_write", "arguments": 'th": "a"}'}}
    _serve(monkeypatch, _Stream(_sse(
        _chunk({"tool_calls": [first]}),
        _chunk({"tool_calls": [second]}, finish="tool_calls"),
        _USAGE,
    )))
    response = _complete(OpenAICompatProvider("http://localhost:9"))
    assert [(c.name, c.input) for c in response.tool_calls] == [("file_write", {"path": "a"})]


def test_a_tool_only_turn_assembles_with_no_content_like_a_one_shot_reply():
    call = {"index": 0, "id": "call_1", "function": {"name": "f", "arguments": "{}"}}
    data = _assemble_stream([_chunk({"tool_calls": [call]}, finish="tool_calls")])
    assert data["choices"][0]["message"]["content"] is None


def test_a_server_that_rejects_stream_options_is_asked_again_without_it(monkeypatch):
    requests: list = []
    rejected = urlerror.HTTPError(
        "http://localhost:9/v1/chat/completions", 422, "Unprocessable", {},
        io.BytesIO(b'{"detail": "Extra inputs are not permitted: stream_options"}'),
    )
    calls = _serve(
        monkeypatch,
        rejected,
        _Stream(_sse(_chunk({"content": "42"}, finish="stop"))),
        _Stream(_sse(_chunk({"content": "43"}, finish="stop"))),
        requests=requests,
    )
    provider = OpenAICompatProvider("http://localhost:9", sleep=lambda _s: None)
    assert _complete(provider).text == "42"
    assert _complete(provider).text == "43"
    assert calls["n"] == 3
    assert "stream_options" in requests[0]
    assert all("stream_options" not in request for request in requests[1:])
    assert all(request["stream"] is True for request in requests)


def test_reasoning_deltas_are_kept_for_replay(monkeypatch):
    _serve(
        monkeypatch,
        _Stream(_sse(
            _chunk({"reasoning_content": "think"}),
            _chunk({"reasoning_content": "ing", "content": ""}),
            _chunk({"content": "42"}, finish="stop"),
            _USAGE,
        )),
    )
    response = _complete(OpenAICompatProvider("http://localhost:9"))
    assert response.text == "42"
    assert response.reasoning == {"reasoning_content": "thinking"}


def test_length_finish_normalizes_to_max_tokens(monkeypatch):
    _serve(monkeypatch, _Stream(_sse(_chunk({"content": "partial"}, finish="length"), _USAGE)))
    assert _complete(OpenAICompatProvider("http://localhost:9")).stop_reason == "max_tokens"


def test_missing_usage_falls_back_to_the_estimate(monkeypatch):
    _serve(monkeypatch, _Stream(_sse(_chunk({"content": "forty-two"}, finish="stop"))))
    response = _complete(OpenAICompatProvider("http://localhost:9"))
    assert response.text == "forty-two"
    assert response.usage.output_tokens > 0


def test_a_stall_after_the_first_chunk_is_not_retried(monkeypatch):
    sleeps: list = []
    calls = _serve(
        monkeypatch,
        _Stream(_sse(_chunk({"content": "4"}), _chunk({"content": "2"}, finish="stop")),
                fail_after=2, exc=TimeoutError("timed out")),
    )
    with pytest.raises(RuntimeError, match="broke after 1 chunk: timed out"):
        _complete(OpenAICompatProvider("http://localhost:9", sleep=sleeps.append))
    assert calls["n"] == 1
    assert sleeps == []


def test_a_clean_close_without_a_finish_reason_is_broken_not_complete(monkeypatch):
    # The server or a proxy went away mid-generation without an error on the
    # socket: what arrived is a reply the model never finished.
    sleeps: list = []
    calls = _serve(
        monkeypatch,
        _Stream(_sse(_chunk({"content": "The ans"}), _chunk({"content": "wer is"}), done=False)),
    )
    with pytest.raises(RuntimeError, match="broke after 2 chunks: the stream ended without"):
        _complete(OpenAICompatProvider("http://localhost:9", sleep=sleeps.append))
    assert calls["n"] == 1
    assert sleeps == []


def test_done_without_a_finish_reason_is_still_complete(monkeypatch):
    _serve(monkeypatch, _Stream(_sse(_chunk({"content": "42"}), _USAGE)))
    assert _complete(OpenAICompatProvider("http://localhost:9")).text == "42"


def test_a_bare_data_line_is_ignored(monkeypatch):
    lines = _sse(_chunk({"content": "4"}))[:-1] + [b"data:\n", b"\n"]
    lines += _sse(_chunk({"content": "2"}, finish="stop"), _USAGE)
    _serve(monkeypatch, _Stream(lines))
    assert _complete(OpenAICompatProvider("http://localhost:9")).text == "42"


def test_a_line_that_is_not_a_chunk_after_the_first_chunk_breaks_the_stream(monkeypatch):
    # A proxy injecting a plain-text line mid-stream: not retried, and named
    # with the chunk count like any other break.
    lines = _sse(_chunk({"content": "4"}))[:-1] + [b"data: upstream timeout\n", b"\n"]
    calls = _serve(monkeypatch, _Stream(lines))
    with pytest.raises(RuntimeError, match="broke after 1 chunk: provider returned a body"):
        _complete(OpenAICompatProvider("http://localhost:9"))
    assert calls["n"] == 1


def test_a_stream_with_only_done_is_not_a_completion(monkeypatch):
    _serve(monkeypatch, _Stream([b"data: [DONE]\n"]))
    with pytest.raises(RuntimeError, match="stream with no completion"):
        _complete(OpenAICompatProvider("http://localhost:9", sleep=lambda _s: None))


def test_a_trailing_empty_usage_does_not_erase_the_real_one(monkeypatch):
    empty = {"id": "chatcmpl-1", "choices": [], "usage": {}}
    _serve(monkeypatch, _Stream(_sse(_chunk({"content": "42"}, finish="stop"), _USAGE, empty)))
    response = _complete(OpenAICompatProvider("http://localhost:9"))
    assert (response.usage.input_tokens, response.usage.output_tokens) == (20, 2)


def test_a_failure_before_the_first_chunk_is_retried(monkeypatch):
    calls = _serve(
        monkeypatch,
        urlerror.URLError("connection refused"),
        _Stream(_sse(_chunk({"content": "42"}, finish="stop"), _USAGE)),
    )
    response = _complete(OpenAICompatProvider("http://localhost:9", sleep=lambda _s: None))
    assert response.text == "42"
    assert calls["n"] == 2


def test_a_timeout_before_the_first_chunk_is_retried(monkeypatch):
    calls = _serve(
        monkeypatch,
        _Stream([], fail_after=0, exc=TimeoutError("timed out")),
        _Stream(_sse(_chunk({"content": "42"}, finish="stop"), _USAGE)),
    )
    response = _complete(OpenAICompatProvider("http://localhost:9", sleep=lambda _s: None))
    assert response.text == "42"
    assert calls["n"] == 2


def test_a_plain_json_error_body_on_a_streaming_request_is_classified(monkeypatch):
    # An aggregator can answer a streaming request with a one-shot error body.
    calls = _serve(
        monkeypatch,
        _Stream([b'{"error": {"code": 502, "message": "at capacity"}}']),
        _Stream(_sse(_chunk({"content": "42"}, finish="stop"), _USAGE)),
    )
    response = _complete(OpenAICompatProvider("http://localhost:9", sleep=lambda _s: None))
    assert response.text == "42"
    assert calls["n"] == 2


def test_an_error_chunk_mid_stream_is_classified_like_an_error_body(monkeypatch):
    # OpenRouter and vLLM report a failure that happens during generation as
    # one more event: the reply is unusable, and a 5xx code is retried.
    failed = {"id": "chatcmpl-1", "choices": [],
              "error": {"code": 502, "message": "upstream disconnected"}}
    calls = _serve(
        monkeypatch,
        _Stream(_sse(_chunk({"content": "The answer"}), failed)),
        _Stream(_sse(_chunk({"content": "42"}, finish="stop"), _USAGE)),
    )
    response = _complete(OpenAICompatProvider("http://localhost:9", sleep=lambda _s: None))
    assert response.text == "42"
    assert calls["n"] == 2


def test_the_output_ceiling_applies_to_a_streamed_tool_call(monkeypatch):
    whole = {"index": 0, "id": "call_1", "type": "function",
             "function": {"name": "file_write", "arguments": '{"path": "a"}'}}
    at_cap = {"id": "chatcmpl-1", "model": "m", "choices": [],
              "usage": {"prompt_tokens": 20, "completion_tokens": 64}}
    _serve(monkeypatch, _Stream(_sse(_chunk({"tool_calls": [whole]}, finish="tool_calls"), at_cap)))
    response = _complete(OpenAICompatProvider("http://localhost:9"), max_tokens=64)
    assert response.stop_reason == "max_tokens"


def test_stream_false_keeps_the_one_shot_request(monkeypatch):
    requests: list = []
    body = json.dumps(
        {"choices": [{"finish_reason": "stop", "message": {"content": "42"}}]}
    ).encode()
    _serve(monkeypatch, _Stream([body]), requests=requests)
    response = _complete(OpenAICompatProvider("http://localhost:9", stream=False))
    assert response.text == "42"
    assert "stream" not in requests[0]
    assert "stream_options" not in requests[0]


def test_models_yaml_stream_false_reaches_the_provider(monkeypatch, tmp_path):
    monkeypatch.setenv("HIVELOOM_HOME", str(tmp_path))
    ext.reset()
    (tmp_path / "models.yaml").write_text(
        """
providers:
  gateway:
    api: openai_compat
    base_url: https://gateway.example.test/v1
    stream: false
    models:
      - id: gw-model
        input_cost_per_mtok: 0
        output_cost_per_mtok: 0
  lab:
    api: openai_compat
    base_url: https://lab.example.test/v1
    models:
      - id: lab-model
        input_cost_per_mtok: 0
        output_cost_per_mtok: 0
""",
        encoding="utf-8",
    )
    assert ext.build_provider("gateway")._stream is False
    assert ext.build_provider("lab")._stream is True
