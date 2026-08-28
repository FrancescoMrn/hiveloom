# The Hiveloom workbench

A chat-first workbench for building and improving harnesses without first
learning Hiveloom's CLI or specification language. It ships as its own
distribution, `hiveloom-workbench`, built from this directory.

```sh
uv add hiveloom          # the framework your harnesses run on
npx hiveloom-workbench   # the inspector — fetched on demand, nothing to install
```

One npm package carries the compiled interface, the Python API (`server.py` plus
the `copilot/` harness), and `bin/cli.mjs`, the launcher. The launcher locates an
interpreter with hiveloom installed, starts the API on a private loopback port,
and serves the interface in front of it — proxying `/api` so everything is one
origin. State lives under `~/.hiveloom/workbench/`, never in the package.

Contributors run both halves with hot reload instead — the only mode that needs
Node 22+:

```sh
devtools/ui/dev.sh
# ui:  http://127.0.0.1:5173
# api: http://127.0.0.1:8770
```

The first launch installs the local npm dependencies. Use
`devtools/ui/dev.sh --host 0.0.0.0` when the browser is outside the machine or
container. Ports can be changed with `HIVELOOM_UI_PORT` and
`HIVELOOM_UI_API_PORT`. A checkout keeps its state in `devtools/ui/.hiveloom/`,
never in the directory an installed workbench uses.

## Product model

There is one primary interaction: a conversation with the bundled
`hiveloom-copilot` harness. The copilot is a framework expert with constrained
tools for creating, inspecting, validating, dry-running, executing, diagnosing,
measuring, and proposing improvements to target harnesses.

Target harnesses are context, not alternate chat agents. Selecting a harness or
run in the rail attaches it to the conversation. A target execution is always a
separate, journalled run owned by that target harness, so copilot reasoning never
pollutes the target's fitness data.

Conversations are durable. The workbench stores their messages, artifacts, and
selected harness/run in a local SQLite database — `~/.hiveloom/workbench/`
installed, `devtools/ui/.hiveloom/` from a checkout. Set `HIVELOOM_UI_DB` to use a different database. A
browser refresh or API restart resumes the same conversation instead of opening
an empty session.

Cross-conversation memory is separate and explicit. Global memories and
harness-scoped memories share the same SQLite database, can be inspected or
deleted under Settings → Memory, and are available to the copilot through
`recall_memories`, `remember_memory`, and `forget_memory`. The copilot is told
to save only requested or clearly durable preferences and conventions; ordinary
conversation content is not silently promoted into memory.

Chat remains the front door, while selecting a harness opens its contextual
workspace beside the conversation. Its Overview, Runs, Trace, Versions, Spec,
Improve, and Interface tabs expose the exact framework state without requiring
the copilot to paraphrase it. The Trace tab includes the complete journal,
filters, integrity result, event payloads, context, timing, and fork controls.
Opening that workspace collapses the navigation rail to an icon strip; it can
be expanded at any time. Drag the divider between chat and the harness
workspace (or focus it and use the arrow keys) to resize the evidence area.

The composer exposes the model used for the next copilot run and a Settings
panel in the bottom of the navigation rail provides workbench preferences,
memory, provider status, and the selected harness's validated model
configuration. With a harness selected, the paperclip uploads
files into that harness's `uploads/` directory. The persisted user message
records the safe relative path and the copilot may inspect UTF-8 attachments
through its constrained `read_harness_file` tool; protected state and paths
outside the harness remain inaccessible.

Tool results arrive as typed artifacts. The same artifact canvas is opened by a
chat response or a direct click in the rail:

- harness contracts and newly-created harnesses;
- validation and dry-run results;
- target runs and actionable failure evidence;
- aggregate fitness and version comparisons;
- safety-gated improvement proposals;
- generated standalone HTML interfaces.

The copilot may draft an improvement, but applying it is a distinct user action.
The UI applies accepted YAML changes explicitly and leaves code changes
unapproved unless the user selects them. Frozen safety fields remain protected
by Hiveloom's evolution gate.

## Generated harness interfaces

`create_interface` writes a dependency-free page to
`<harness>/interfaces/default/index.html` and returns the complete HTML as an
artifact. The workbench previews it in an `allow-scripts` sandbox with a narrow
message bridge that can only run the artifact's named harness. URL and text
contracts send literal input. File contracts upload into the harness workspace
and send the resulting safe relative path. Preview executions use the normal
streaming runner, so progress, stop control, verification verdicts, cost, and
run identity remain visible. A completed preview run opens directly into the
same recorded trace used by the Runs view.

In the workbench, select a harness in the left rail and choose **Use** (the
default harness view). Harnesses without a generated interface explain the
copilot request that creates one; after creation, the same harness selection
opens the runnable interface directly.

The generated page also supports the ordinary non-streaming `/run` endpoint
when hosted with a deployment. A direct file page needs a deployment-provided
upload bridge; the workbench preview provides that bridge automatically.

## Safety and boundaries

- The bundled copilot is validated and trusted when the workbench starts.
- Foreign target harnesses retain the normal trust gate before their code runs.
- Harness creation uses `hiveloom.construct`; every step validates, and a failed
  creation removes the incomplete directory.
- Builtin names come from the live catalog, never from a UI-maintained copy.
- Uploads pass through Hiveloom's safe-path containment and cannot enter
  `.hiveloom` or `.env`.
- The preview iframe receives no credentials or direct framework object.
- Copilot selection is request-owned context, not a global, so concurrent chats
  do not leak their selected harness or run into one another.

- The `hiveloom` wheel never carries any of this. The workbench is a separate
  distribution so that a deployment is not made to carry a web interface.

## Building the package

```sh
scripts/build_workbench.sh            # typecheck, test, bundle, pack
scripts/build_workbench.sh --check    # also install the tarball and launch it
```

The order matters: `npm pack` ships whatever is in `web/`, so packing before
`npm run build` would publish a stale interface. The script compiles first for
exactly that reason, then asserts the tarball's contents — the API and the
bundle present, the TypeScript source and `vite.config.ts` absent. That last one
is not cosmetic: `server.py` treats `vite.config.ts` as proof of a source
checkout, so shipping it would make every install write its database into
`node_modules`.

## Tests

```sh
uv run pytest -q tests/test_devtools_ui.py
uv run ruff check devtools/ui/server.py devtools/ui/copilot/tools/workbench.py
npm run typecheck --prefix devtools/ui
npm test --prefix devtools/ui
npm run build --prefix devtools/ui
```

The copilot harness itself should also remain valid and free to assemble:

```sh
uv run hiveloom validate devtools/ui/copilot --json
uv run hiveloom run devtools/ui/copilot --input "inspect this harness" --dry-run --json
```
