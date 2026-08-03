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
$ hiveloom run my-harness --input "…"
```

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
```

A newly declared provider is **fixed** by default: you listed the models you
meant, so a typo should fail. Set `open_catalog: true` to accept any id.

Omitting a model's pricing is allowed but reported by `hiveloom extensions`,
and that model is priced at the conservative fallback. Set both costs to `0`
to declare a model genuinely free.

A malformed `models.yaml` never crashes the CLI — the error is collected and
shown by `hiveloom extensions`.

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

- **Tool calling** is required for most harnesses. All builtin providers
  support it, but coverage differs per model — a small local model may ignore
  tools entirely. Check with `hiveloom run --dry-run` before a real run.
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
