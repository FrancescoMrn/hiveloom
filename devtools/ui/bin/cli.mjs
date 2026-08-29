#!/usr/bin/env node
/**
 * The hiveloom workbench launcher.
 *
 *   npx hiveloom-workbench
 *
 * One package carries three things: the compiled UI, the Python API that
 * actually knows how to run a harness, and this process, which puts them in
 * front of the user as a single URL.
 *
 * Why Node launches Python. The workbench is not a static site — every action
 * validates a spec, executes a harness, reads a journal, or drafts an evolution
 * proposal, all of which are hiveloom's own Python modules. Reimplementing them
 * in JavaScript would mean two implementations of the safety rules that decide
 * whether foreign code runs. So the API stays Python, ships inside this package
 * as `server.py`, and is spawned against the interpreter that already has
 * `hiveloom` installed — the one the user made with `uv add hiveloom`.
 *
 * Why this proxies rather than pointing the browser at the API directly. The
 * frontend calls its API with relative paths, on purpose: one origin means no
 * CORS, no credentials crossing an origin boundary, and no base-URL setting to
 * get wrong. Serving the UI here and forwarding `/api` to a loopback-only
 * Python process preserves that, and keeps the API unreachable from anywhere
 * except this process.
 */

import { spawn, spawnSync } from 'node:child_process'
import { createServer, request as httpRequest } from 'node:http'
import { createReadStream, existsSync, statSync } from 'node:fs'
import { readFile } from 'node:fs/promises'
import { createConnection, createServer as createTcpServer } from 'node:net'
import { dirname, extname, join, normalize, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'

const PACKAGE_DIR = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const WEB_DIR = join(PACKAGE_DIR, 'web')
const SERVER_PY = join(PACKAGE_DIR, 'server.py')
const VERSION = JSON.parse(
  await readFile(join(PACKAGE_DIR, 'package.json'), 'utf8'),
).version

const MIME = {
  '.css': 'text/css; charset=utf-8',
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.map': 'application/json; charset=utf-8',
  '.png': 'image/png',
  '.svg': 'image/svg+xml',
  '.ttf': 'font/ttf',
  '.woff': 'font/woff',
  '.woff2': 'font/woff2',
}

const HELP = `hiveloom-workbench ${VERSION}

  npx hiveloom-workbench [options]

  --port <n>        Port to serve the workbench on (default 8770).
  --host <addr>     Bind address (default 127.0.0.1). Use 0.0.0.0 when the
                    browser is not on this machine — a container, a VM.
  --dir <path>      Offer one harness beyond the registry. Repeatable.
  --scan-dir <path> Discover harnesses recursively below a directory.
                    Repeatable. Defaults to ./harnesses when it exists.
  --python <path>   Interpreter to run the API with. Overrides detection.
  --no-open         Do not open a browser.
  --version         Print the version and exit.

The API runs as a Python process and needs hiveloom importable:

  uv add hiveloom        # or: pip install hiveloom
`

function parseArgs(argv) {
  const options = { dirs: [], scanDirs: [], port: 8770, host: '127.0.0.1', python: '', open: true }
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i]
    const next = () => {
      const value = argv[i + 1]
      if (value === undefined) fail(`${arg} needs a value`)
      i += 1
      return value
    }
    if (arg === '--port') options.port = Number(next())
    else if (arg === '--host') options.host = next()
    else if (arg === '--dir' || arg === '-d') options.dirs.push(resolve(next()))
    else if (arg === '--scan-dir') options.scanDirs.push(resolve(next()))
    else if (arg === '--python') options.python = next()
    else if (arg === '--no-open') options.open = false
    else if (arg === '--version' || arg === '-v') { console.log(VERSION); process.exit(0) }
    else if (arg === '--help' || arg === '-h') { console.log(HELP); process.exit(0) }
    else fail(`unknown option: ${arg} (try --help)`)
  }
  if (!Number.isInteger(options.port) || options.port < 1 || options.port > 65535) {
    fail(`--port must be a port number, got "${options.port}"`)
  }
  return options
}

function fail(message, hint) {
  console.error(`error: ${message}`)
  if (hint) console.error(hint)
  process.exit(1)
}

/**
 * Find an interpreter that can `import hiveloom`.
 *
 * Order is deliberate: an explicit choice, then the environment the user is
 * standing in, then their project's own. `uv run` is tried against the working
 * directory because `uv add hiveloom` is the documented way in, and it puts
 * hiveloom in a project venv that is not on PATH — detecting only `python3`
 * would miss exactly the setup the docs tell people to create.
 */
function findPython(explicit) {
  const candidates = []
  if (explicit) candidates.push({ command: explicit, args: [] })
  else {
    if (process.env.HIVELOOM_PYTHON) {
      candidates.push({ command: process.env.HIVELOOM_PYTHON, args: [] })
    }
    if (process.env.VIRTUAL_ENV) {
      candidates.push({ command: join(process.env.VIRTUAL_ENV, 'bin', 'python'), args: [] })
    }
    if (existsSync(join(process.cwd(), 'pyproject.toml'))) {
      candidates.push({ command: 'uv', args: ['run', '--quiet', 'python'] })
    }
    candidates.push({ command: 'python3', args: [] })
    candidates.push({ command: 'python', args: [] })
    // Last resort and the reason a user with no Python project still gets a
    // working workbench: uv fetches an interpreter and hiveloom on demand,
    // into its own cache rather than into anything of theirs.
    candidates.push({ command: 'uv', args: ['run', '--quiet', '--with', 'hiveloom', 'python'] })
  }

  for (const candidate of candidates) {
    const probe = spawnSync(candidate.command, [...candidate.args, '-c', 'import hiveloom'], {
      stdio: 'ignore',
    })
    if (!probe.error && probe.status === 0) return candidate
  }
  return null
}

/** A free loopback port, asked of the OS rather than guessed. */
function freePort() {
  return new Promise((ok, no) => {
    const probe = createTcpServer()
    probe.on('error', no)
    probe.listen(0, '127.0.0.1', () => {
      const { port } = probe.address()
      probe.close(() => ok(port))
    })
  })
}

function portIsFree(host, port) {
  return new Promise((ok) => {
    const probe = createConnection({ host, port })
    probe.on('connect', () => { probe.destroy(); ok(false) })
    probe.on('error', () => ok(true))
  })
}

async function waitForApi(port, child) {
  const deadline = Date.now() + 60_000
  while (Date.now() < deadline) {
    if (child.exitCode !== null) return null
    try {
      const response = await fetch(`http://127.0.0.1:${port}/api/health`)
      if (response.ok) return await response.json()
    } catch {
      // Not up yet. The loop, not the error, is the signal.
    }
    await new Promise((ok) => setTimeout(ok, 150))
  }
  return null
}

function serveStatic(request, response) {
  const url = new URL(request.url, 'http://localhost')
  const relative = decodeURIComponent(url.pathname).replace(/^\/+/, '')
  // Containment by resolution, not by pattern: what matters is where the path
  // lands, never what it looks like.
  const target = resolve(WEB_DIR, normalize(relative))
  const inside = target === WEB_DIR || target.startsWith(WEB_DIR + sep)
  const file = inside && existsSync(target) && statSync(target).isFile()
    ? target
    : join(WEB_DIR, 'index.html')
  response.writeHead(200, {
    'content-type': MIME[extname(file)] ?? 'application/octet-stream',
    // The document must not be cached: a republished bundle changes its
    // content-hashed asset names, and a stale index.html would ask for assets
    // that no longer exist. The hashed assets themselves are immutable.
    'cache-control': file.endsWith('index.html')
      ? 'no-store'
      : 'public, max-age=31536000, immutable',
  })
  createReadStream(file).pipe(response)
}

function proxyToApi(request, response, apiPort) {
  const proxied = httpRequest(
    {
      host: '127.0.0.1',
      port: apiPort,
      method: request.method,
      path: request.url,
      headers: { ...request.headers, host: `127.0.0.1:${apiPort}` },
    },
    (upstream) => {
      response.writeHead(upstream.statusCode, upstream.headers)
      // Piped, never collected: run streams are NDJSON delivered a line at a
      // time, and buffering the body would hold a live run's progress back
      // until it finished — the one thing the trace view exists to show.
      upstream.pipe(response)
    },
  )
  proxied.on('error', (error) => {
    if (!response.headersSent) {
      response.writeHead(502, { 'content-type': 'application/json' })
    }
    response.end(JSON.stringify({ ok: false, error: `api unreachable: ${error.message}` }))
  })
  // A browser that navigates away mid-run must not leave the upstream request
  // hanging: the API notices the disconnect and stops streaming.
  response.on('close', () => proxied.destroy())
  request.pipe(proxied)
}

async function main() {
  const options = parseArgs(process.argv.slice(2))

  if (!existsSync(join(WEB_DIR, 'index.html'))) {
    fail(
      'this package carries no built interface',
      '  The published package always does; a checkout needs: npm run build',
    )
  }
  if (!(await portIsFree(options.host, options.port))) {
    fail(
      `port ${options.port} on ${options.host} is already in use`,
      `  Free it, or choose another: npx hiveloom-workbench --port <n>`,
    )
  }

  const python = findPython(options.python)
  if (!python) {
    fail(
      'no Python interpreter with hiveloom installed was found',
      '\n' +
        '  The workbench runs harnesses through hiveloom itself, so it needs it\n' +
        '  importable. In your project:\n' +
        '\n' +
        '      uv add hiveloom          # or: pip install hiveloom\n' +
        '\n' +
        '  Then run this again from the same directory, or point at an\n' +
        '  interpreter directly: npx hiveloom-workbench --python /path/to/python',
    )
  }

  if (options.scanDirs.length === 0 && options.dirs.length === 0) {
    const conventional = join(process.cwd(), 'harnesses')
    if (existsSync(conventional)) options.scanDirs.push(conventional)
  }

  const apiPort = await freePort()
  const apiArgs = [
    ...python.args,
    SERVER_PY,
    '--host', '127.0.0.1',
    '--port', String(apiPort),
    ...options.dirs.flatMap((d) => ['--dir', d]),
    ...options.scanDirs.flatMap((d) => ['--scan-dir', d]),
  ]
  const api = spawn(python.command, apiArgs, {
    stdio: ['ignore', 'pipe', 'inherit'],
    env: { ...process.env, PYTHONUNBUFFERED: '1' },
  })
  // The API's own banner would announce a port the user must never use, since
  // it is loopback-only and behind this proxy. Its errors still go to stderr.
  api.stdout.resume()

  const shutdown = () => { api.kill('SIGTERM'); process.exit(0) }
  process.on('SIGINT', shutdown)
  process.on('SIGTERM', shutdown)
  api.on('exit', (code) => {
    if (code) console.error(`\nthe workbench API exited with code ${code}`)
    process.exit(code ?? 0)
  })

  const health = await waitForApi(apiPort, api)
  if (!health) fail('the workbench API did not start (its error is above)')
  if (health.version !== VERSION) {
    // Only possible when a checkout is running a server.py from a different
    // release than the launcher. Worth naming: the UI mirrors 45 route shapes
    // by hand, and a mismatch fails in ways that look like bugs.
    console.error(
      `warning: interface ${VERSION} against API ${health.version}; ` +
        'they ship together and are expected to match',
    )
  }

  const web = createServer((request, response) => {
    if (request.url.startsWith('/api/')) proxyToApi(request, response, apiPort)
    else serveStatic(request, response)
  })
  web.listen(options.port, options.host, () => {
    const url = `http://${options.host}:${options.port}`
    console.log(`\n  hiveloom workbench   ${url}`)
    console.log(`  api                  python (${python.command}), loopback only`)
    if (options.host === '127.0.0.1') {
      console.log('  (only reachable from this machine — pass --host 0.0.0.0 if')
      console.log('   your browser is elsewhere)')
    }
    console.log('')
    if (options.open) openBrowser(url)
  })
}

function openBrowser(url) {
  const command = process.platform === 'darwin' ? 'open'
    : process.platform === 'win32' ? 'start'
    : 'xdg-open'
  // Best effort by design: a headless container or a machine with no desktop
  // has no browser to open, and that is not a failure — the URL is printed.
  try {
    spawn(command, [url], { stdio: 'ignore', detached: true, shell: process.platform === 'win32' })
      .on('error', () => {})
      .unref()
  } catch {
    /* the printed URL is the fallback */
  }
}

main()
