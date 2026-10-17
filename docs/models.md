# Models and providers

A harness names the model it executes with in two fields:

```yaml
model:
  provider: openai        # a registry name
  id: gpt-4.1-mini        # a model id that provider serves
```

`provider` is a registry name, not a hard-coded literal. hiveloom ships
builtin providers for the major labs, the routing aggregators, and local
servers, so most harnesses need no configuration at all — just the API key.

Run `hiveloom models` to see what is registered, which environment variable
each provider reads, and whether that variable is currently set:

```console
$ hiveloom models
provider     key env              key    catalog  models
claude       ANTHROPIC_API_KEY    set    fixed    claude-haiku-4-5, claude-sonnet-5, …
openai       OPENAI_API_KEY       unset  open     gpt-4o, gpt-4o-mini, gpt-4.1, …
ollama       -                    n/a    open     -
```

`hiveloom models <provider>` narrows to one provider and prints its endpoint
and per-model pricing. Both forms accept `--json`. Neither ever prints a key
value — only whether one is present.

## Builtin providers

| `provider` | Lab / service | Key variable | Endpoint |
|---|---|---|---|
| `claude` | Anthropic | `ANTHROPIC_API_KEY` | native SDK |
| `openai` | OpenAI | `OPENAI_API_KEY` | `https://api.openai.com/v1` |
| `gemini` | Google Gemini | `GEMINI_API_KEY` | `https://generativelanguage.googleapis.com/v1beta/openai` |
| `mistral` | Mistral | `MISTRAL_API_KEY` | `https://api.mistral.ai/v1` |
| `deepseek` | DeepSeek | `DEEPSEEK_API_KEY` | `https://api.deepseek.com/v1` |
| `xai` | xAI Grok | `XAI_API_KEY` | `https://api.x.ai/v1` |
| `moonshot` | Moonshot AI | `MOONSHOT_API_KEY` | `https://api.moonshot.ai/v1` |
| `groq` | Groq | `GROQ_API_KEY` | `https://api.groq.com/openai/v1` |
| `openrouter` | OpenRouter | `OPENROUTER_API_KEY` | `https://openrouter.ai/api/v1` |
| `together` | Together AI | `TOGETHER_API_KEY` | `https://api.together.xyz/v1` |
| `fireworks` | Fireworks AI | `FIREWORKS_API_KEY` | `https://api.fireworks.ai/inference/v1` |
| `ollama` | Ollama (local) | none | `http://localhost:11434/v1` |
| `vllm` | vLLM (local) | none | `http://localhost:8000/v1` |

### Switching a harness to another lab

Use the `provider/model-id` selector — the same syntax `generate --model` and
`evolve --model` accept:

```console
$ hiveloom set model openai/gpt-4.1-mini --dir ./summarizer
set model openai/gpt-4.1-mini
```

Do **not** set `model.provider` and `model.id` separately. They validate
against each other, so whichever you write first leaves the spec inconsistent
and the edit is rolled back — `set model` is the only ordering that works. The
selector splits on the first `/` only, so aggregator ids keep their own
slashes: `openrouter/deepseek/deepseek-r1`.

`claude` uses the Anthropic SDK. Every other builtin is the stdlib-only
`OpenAICompatProvider` speaking `/chat/completions`, so any other server with
that API works too — LM Studio, `mlx_lm.server`, a corporate gateway — either
by pointing `vllm`/`ollama` at it or by declaring your own entry (below).

The key is read from the process environment or the harness's own `.env`, so a
harness folder stays self-contained:

```console
$ echo 'OPENAI_API_KEY=sk-...' >> my-harness/.env
$ hiveloom run my-harness --input-text "…"
```

For a one-run comparison, override the runtime config instead of mutating the
harness:

```console
$ hiveloom run my-harness --input-file case.txt \
    --provider openrouter --model qwen3.5-9b --json
```

The override is validated and included in the run's version hash, but
`harness.yaml` remains byte-identical.

## Open vs fixed catalogs

`hiveloom models` labels each provider's catalog `open` or `fixed`.

- **Fixed** (`claude`): only the model ids registered in-repo validate, so
  `claude-hiaku-4-5` fails `hiveloom validate` with a clear error instead of at
  runtime.
- **Open** (everything else): any model id validates. Lab catalogs change far
  faster than hiveloom releases, and aggregators route to thousands of ids —
  a fixed list would make each new frontier model unusable until the next
  hiveloom version. `provider: openai, id: <released-yesterday>` just works.

The trade-off is that a typo on an open provider is only caught when the call
fails. That is the right side to err on: the alternative blocks real work.

## Capability and identity probes

Inspect registered declarations without contacting the endpoint:

```bash
hiveloom models probe ./h --provider openrouter --model qwen3.5-9b --json
```

Add `--live` to send one small tool request and, only when the response carries
replayable reasoning, one follow-up request:

```bash
hiveloom models probe ./h --provider openrouter --model qwen3.5-9b \
  --identity exact --live --require-compatible --json
```

`--live` may perform two billed provider calls. The JSON `plan` repeats that
side-effect boundary, and the result reports call count, usage, cost source,
and request IDs. Live results are cached for 24 hours; `--refresh` bypasses the
cache. A provider, requested-model, or adapter-implementation change creates a
new cache key.

Identity policies are explicit:

- `warn` accepts a mismatch but keeps a machine-readable warning;
- `exact` accepts only the requested model and rejects an absent identity;
- `alias` accepts the requested model or a repeated `--alias` value.

Capabilities for tool calling, structured output, and reasoning replay each
say whether the value was `declared`, `observed`, or remains `unknown`. The
generic live probe can observe tool use and replay reasoning. Provider
extensions may override `probe_capabilities()` when their API exposes a more
precise structured-output check.

## Pricing and budget guardrails

Pricing drives cost estimation, the `max_cost_usd` guardrail, and the
cost-per-success numbers in `hiveloom stats`. Resolution order for a model id:

1. An exact registration — the builtin per-lab lists, or your `models.yaml`.
2. The provider's default price. Only local providers declare one, at zero, so
   an unlisted Ollama model is correctly free.
3. The conservative fallback, Haiku-class `$1.00 / $5.00` per 1M tokens.

Step 3 is deliberately pessimistic: an unknown hosted model is assumed to cost
something, so a budget guardrail can stop a run early but never lets one
overspend because it thought a model was free.

> The shipped prices are list price at release time. They are estimates for
> budgeting, not billing. Verify against your provider's current pricing and
> override anything that has moved.

### Prompt caching

The `claude` provider always requests prompt caching: the system prompt, the
tool list, and the conversation tail are marked as cache breakpoints, so the
stable prefix of an agent loop is written once and read cheaply on every later
turn. Cache traffic is reported separately on usage (`cache_read_tokens`,
`cache_write_tokens`) and priced at 0.1x / 1.25x the input price. OpenAI-style
servers cache implicitly; when they report `cached_tokens`, hiveloom splits
them out of the input count and prices them the same way. Both feed the
`max_cost_usd` guardrail and `hiveloom stats`, so cached runs show their real,
lower cost.

### Adaptive-thinking Claude models

The newer Claude tiers (Opus 4.7 and later, Sonnet 5, Fable/Mythos) reject
sampling parameters and reason with adaptive thinking. The provider adapts on
its own — nothing to configure — but two consequences are worth knowing:

- `model.temperature` is **not sent** for those ids. The spec default of `0.0`
  is a non-default value to that API and would fail the call, so it is dropped
  rather than silently reinterpreted. Set it for other models as usual.
- Thinking blocks are kept on the assistant turn and replayed verbatim, which
  those models require across a tool-use loop. They are billed as output
  tokens, so budget `model.max_tokens` for thinking *plus* the answer: a
  harness whose responses are large should raise it (the frontier evals use
  `16000`).

Those ids are not all in the fixed `claude` catalog yet; register a missing one
with its pricing in `models.yaml` as below.

## Customising with `models.yaml`

`~/.hiveloom/models.yaml` (or `$HIVELOOM_HOME/models.yaml`) adds providers and
adjusts builtin ones. Three shapes:

**Correct or add models on a builtin** — omit `base_url` to extend rather than
replace:

```yaml
providers:
  openai:
    models:
      - id: gpt-4o
        input_cost_per_mtok: 1.11    # your negotiated rate
        output_cost_per_mtok: 2.22
```

**Point a builtin name at a different endpoint** — supply `base_url` and it
overrides the builtin, keeping the name your harnesses already reference:

```yaml
providers:
  openai:
    base_url: https://gateway.internal.example/v1
    api_key_env: INTERNAL_GATEWAY_KEY
```

**Declare a new provider** — any OpenAI-compatible server:

```yaml
providers:
  lmstudio:
    api: openai_compat
    base_url: http://localhost:1234/v1
    models:
      - id: qwen3-8b
        input_cost_per_mtok: 0
        output_cost_per_mtok: 0
        supports_tool_calling: true
        supports_structured_output: false
        supports_reasoning_replay: false
```

A newly declared provider is **fixed** by default: you listed the models you
meant, so a typo should fail. Set `open_catalog: true` to accept any id.

Omitting a model's pricing is allowed but reported by `hiveloom extensions`,
and that model is priced at the conservative fallback. Set both costs to `0`
to declare a model genuinely free.

A malformed `models.yaml` never crashes the CLI — the error is collected and
shown by `hiveloom extensions`.

## Provider request parameters

`model.params` carries provider-specific JSON request fields. Both the Claude
provider and the OpenAI-compatible provider forward these fields, and model
overrides retain them. Extensions implementing other providers decide which
parameters they support. Provider-specific settings may need an operator update
when switching providers.

```console
hiveloom set model.params '{"seed": 7}' --dir ./harness --json
```

Use field names accepted by the selected endpoint. The mapping is limited to
64 KiB of JSON and cannot replace harness-controlled fields: `model`,
`messages`, `system`, `input`, `tools`, `functions`, `max_tokens`,
`max_completion_tokens`, `max_output_tokens`, `temperature`, `stream`,
`stream_options`, or `n`. `model` remains frozen from evolution, including these
parameters. Parameters are request-body values, not HTTP headers or SDK options.

A model registered through an extension or `models.yaml` can declare a positive
`max_output_tokens`. `model.max_tokens` is validated against that capability.
Unknown capabilities use the spec's absolute 1,000,000-token validation bound;
this does not promise that an endpoint accepts that many tokens. The runtime
never automatically raises the configured budget after a truncated response.

Custom OpenAI-compatible endpoints can declare a positive `timeout_seconds`:

```yaml
providers:
  local:
    api: openai_compat
    base_url: http://localhost:8000/v1
    timeout_seconds: 600
    models:
      - id: local-model
        max_output_tokens: 16384
```

The default timeout is 120 seconds per HTTP request. Retries can consume
additional time. Runtime wall-clock guardrails are checked at loop boundaries;
they do not cancel a blocking HTTP request.

## Reasoning control

Open reasoning models spend their output budget thinking unless told not to,
and a reasoning token counts against `model.max_tokens`. A harness with a small
budget can then get back an empty answer. `model.reasoning` is the one
provider-neutral setting for it:

```console
hiveloom set model.reasoning low --dir ./harness --json
```

Values: `default` (send nothing — the model's own behavior, and what every
existing harness has), `none`, `minimal`, `low`, `medium`, `high`. Hiveloom maps
the level to the field the host actually reads, chosen by the provider's
**reasoning style**, because a host that does not recognise a field usually
ignores it without an error:

| Style | Sends | `none` sends |
|---|---|---|
| `openai` | `reasoning_effort: <level>` | `reasoning_effort: "none"` |
| `openrouter` | `reasoning: {effort: <level>}` | `reasoning: {enabled: false}` |
| `chat_template` | `reasoning_effort: <level>` | `reasoning_effort: "none"` and `chat_template_kwargs: {enable_thinking: false}` |
| `none` | nothing: any non-default level is rejected | rejected |
| `anthropic` (`claude` only) | `thinking: {type: adaptive}` and `output_config: {effort: <level>}` | `thinking: {type: disabled}` (`between_tools` on Sonnet 5.5) |

Builtin assignments: `openrouter` is `openrouter`; `vllm` is `chat_template`;
`openai`, `gemini`, `xai`, `groq`, `together`, `fireworks` and `ollama` are
`openai`; `mistral`, `deepseek` and `moonshot` are `none` (hiveloom does not
know of a control there, so asking for one is an error rather than a silent
no-op); `claude` is `anthropic`. A provider declared in `models.yaml` or
through `register_provider` and naming no style is `openai`. Correct any of
them with `reasoning_style` (`openai`, `openrouter`, `chat_template`, `none`)
on the provider entry:

```yaml
providers:
  gateway:
    base_url: https://gateway.internal.example/v1
    open_catalog: true
    reasoning_style: chat_template
  mistral:                       # extend a builtin: keeps its endpoint
    reasoning_style: openai
```

Details worth knowing:

- Exact accepted values vary by model (`minimal` is not valid everywhere).
  The level is passed through, so a host that refuses it answers with its own
  error, and the run reports it. OpenRouter models that cannot turn reasoning
  off answer `none` with a 400 ("Reasoning is mandatory"); that is surfaced,
  not worked around.
- An explicit key in `model.params` (`reasoning`, `reasoning_effort`,
  `chat_template_kwargs`, `thinking`, `output_config`) beats the mapping — the
  escape hatch for a host that needs something else. An explicit `reasoning`
  or `reasoning_effort` takes over the switch entirely: the mapped fields are
  not sent next to it. When both set an
  object (`chat_template_kwargs`, Claude's `output_config`), the
  two are merged key by key and the explicit keys win: a structured-output
  format or an unrelated template flag never silently drops the mapped
  switch. Set the same nested key (say `enable_thinking`) to override it.
- On Claude, `minimal` is sent as `low`; models whose thinking is always on
  (Opus 5.5, Fable, Mythos) reject `none`; models without effort control
  (Haiku 4.5) accept only `none` and `default`. These are rejected when the
  spec is validated.
- Every provider call records what was sent (`reasoning_sent` in `provider_calls`
  and the trace), its `stop_reason`, whether the response was empty
  (`output_empty`), and `usage.reasoning_tokens` when the host reports them
  (OpenAI-compatible `completion_tokens_details.reasoning_tokens`). Reasoning
  tokens are already part of `output_tokens`; cost is unchanged.
- A run whose last turn hit `model.max_tokens` with no answer ends `truncated`,
  with a reason that names the budget and the reasoning tokens spent.
- `hiveloom validate` and `run --dry-run` warn when `model.reasoning` is
  `default`, `model.max_tokens` is under 2048 and the model is known to reason.
  A model is known to reason when its registry entry says `reasoning: true`
  (builtin: the thinking-by-default Claude ids, `deepseek-reasoner`,
  `gemini-2.5-*`, `grok-3-mini`); set it for your own models in `models.yaml`:

```yaml
providers:
  openrouter:
    models:
      - id: z-ai/glm-4.6
        input_cost_per_mtok: 0.5
        output_cost_per_mtok: 1.5
        reasoning: true
```

## Generation and evolution models

`hiveloom generate` and `hiveloom evolve` use a *strong* model, separate from
the small executor inside the harness. Select it with `provider/model-id`:

```console
$ hiveloom generate "extract invoice totals" --model openai/gpt-4.1 -o ./invoices
$ hiveloom evolve ./invoices --model ollama/qwen3:32b
```

Without `--model` they default to a Claude strong model and need
`ANTHROPIC_API_KEY`. Note that `model` is in `ALWAYS_FROZEN`: the registry
widens what a human or a generator may *choose*, never what evolution may
*mutate*.

## Compatibility notes

- **Tool calling** is required for most harnesses. Provider support differs per
  model; a small local model may ignore tools entirely. Use a declared or live
  `hiveloom models probe` before a batch. `run --dry-run` validates the harness
  but does not test model behavior.
- **Gemini** is reached through Google's OpenAI-compatible surface, which
  supports a narrower slice of the API than the native one. Basic tool calling
  works; exotic parameters may not.
- **Reasoning models** (the DeepSeek-R1 family and similar) are handled: a
  reasoning-only turn is normalized from the response's
  `reasoning`/`reasoning_content` field when `content` is empty.
- **Aggregators** (`openrouter`, `together`, `fireworks`) use their own id
  namespace, e.g. `deepseek/deepseek-r1` — pass the id exactly as that service
  documents it.

## Programmatic registration

For anything that is not an OpenAI-compatible HTTP endpoint, register a
provider from an extension:

```python
def setup(hive):
    hive.register_provider(
        "mylab",
        lambda ctx: MyProvider(),
        api_key_env="MYLAB_API_KEY",
        open_catalog=False,
        models=[{"id": "mylab-small", "input_cost_per_mtok": 0.1,
                 "output_cost_per_mtok": 0.4}],
    )
```

The factory must return a `ModelProvider` (see
[`models/provider.py`](../src/hiveloom/models/provider.py)). Full extension
mechanics live in [extending.md](extending.md).

Strong-model generation and evolution use up to 32,768 output tokens when the
model declares an output capability, capped by that capability. Models without
a declared capability retain the 4,096-token default. SDK callers may request a
positive budget explicitly, within any declared model limit.
