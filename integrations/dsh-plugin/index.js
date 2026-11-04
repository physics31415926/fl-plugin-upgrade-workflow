/**
 * Host half of the FL adaptation workbench.
 *
 * Owns the Python dashboard process (fl-plugin-upgrade-workflow) and exposes a
 * same-origin HTTP surface the Client panel reads. The dashboard itself binds
 * loopback only and rejects any request whose `Host` header is not its own
 * origin, so the browser cannot reach it directly: every read goes through the
 * proxy route below, which rewrites `Host` for the upstream request.
 */

import { request as httpRequest } from 'node:http'
import { existsSync, readFileSync } from 'node:fs'
import { join, resolve as resolvePath } from 'node:path'
import { createServer as createSocketServer } from 'node:net'

/*
 * Only hard dependencies are injected. `workspaceRegistry` is read through
 * `ctx.get` instead: listing it here would hold the plugin pending on a build
 * that does not provide it, when the right behaviour is to drop the
 * workspace-adoption button and keep everything else working.
 */
export const inject = ['webServer', 'subprocess', 'tools']

/**
 * Route prefix claimed on the Harness web server.
 *
 * Registered without a trailing slash: the carrier matches a prefix at a path
 * segment boundary, so `/fl-upgrade` also serves `/fl-upgrade/state`, while a
 * registered `/fl-upgrade/` would only match a doubled separator.
 */
const ROUTE_PREFIX = '/fl-upgrade'

/**
 * Upstream dashboard paths the panel may read. Read-only, no mutations.
 *
 * Deliberately excluded, because each carries machine identity, SSH aliases,
 * credential locations or absolute filesystem paths that no view here needs:
 * `/api/codex-sessions`, `/api/jumpserver`, `/api/access-shares`,
 * `/api/agent-settings`, `/api/logs`, and `/api/status` (its `platforms[]`
 * embeds absolute log paths and worktrees). Task and credential routes stay
 * user-only and out of this surface entirely.
 */
const READ_PATHS = new Set([
  '/api/projects',
  '/api/projects-status',
  '/api/capabilities',
  '/api/branches',
  '/api/branch-status',
  '/api/devices',
  '/api/adaptation-tasks',
  '/api/codex-health',
  '/api/agent-providers',
])

/**
 * Marker for the per-session transcript summarizer. Not a real allowlist entry:
 * the upstream path embeds a session id, so the `transcript` action builds it
 * and only this constant selects the summarizer.
 */
const TRANSCRIPT_PATH = '/api/codex-sessions/:id/events'

/**
 * Transcript events with no user-facing meaning.
 *
 * `account/rateLimits/updated` additionally describes the plan tier and credit
 * balance, which is account detail the panel has no reason to show.
 */
const TELEMETRY_METHODS = new Set([
  'account/rateLimits/updated',
  'thread/tokenUsage/updated',
  'serverRequest/resolved',
  'deprecationNotice',
  'remoteControl/status/changed',
])

/** Allowed read paths that may block on a cold upstream cache. */
const SLOW_READ_PATHS = new Set(['/api/adaptation-tasks', '/api/projects-status'])

/** Upstream dashboard paths the panel may trigger. Collection refresh only. */
const REFRESH_PATHS = new Set(['/api/branch-refresh', '/api/devices/refresh'])

const MAX_UPSTREAM_BYTES = 4 * 1024 * 1024
const OUTPUT_LIMIT = 256 * 1024
const UPSTREAM_TIMEOUT_MS = 30_000

/**
 * Timeout for upstream reads that block on a cold cache instead of answering
 * from one. `/api/adaptation-tasks` was measured at 40-50 s on a first read
 * (and ~2 s once warm), which the default would abort.
 */
const SLOW_UPSTREAM_TIMEOUT_MS = 90_000

function normalizeConfig(raw) {
  const value = raw && typeof raw === 'object' ? raw : {}
  const repoPath = typeof value.repoPath === 'string' && value.repoPath.trim()
    ? resolvePath(value.repoPath.trim())
    : ''
  const port = Number.isInteger(value.port) && value.port >= 1 && value.port <= 65535
    ? value.port
    : 8766
  return {
    repoPath,
    port,
    pythonPath: typeof value.pythonPath === 'string' ? value.pythonPath.trim() : '',
    projectsConfig: typeof value.projectsConfig === 'string' ? value.projectsConfig.trim() : '',
    autoStart: value.autoStart === true,
  }
}

/**
 * Resolve how the dashboard should be launched.
 *
 * `runs/configured/launch.json` is what the repo's own wizard writes; when it
 * is present its recorded python and argv are authoritative, because they
 * encode the generated projects.json path. Otherwise fall back to the
 * configured projects.json, and report `configured: false` when neither
 * exists so the panel can explain the missing step instead of failing a spawn.
 */
function resolveLaunch(config) {
  if (!config.repoPath) {
    return { configured: false, reason: 'repoPath-missing' }
  }
  if (!existsSync(config.repoPath)) {
    return { configured: false, reason: 'repo-missing' }
  }
  const entry = join(config.repoPath, 'scripts', 'flflow.py')
  if (!existsSync(entry)) {
    return { configured: false, reason: 'not-a-checkout' }
  }

  // An older deployment keeps projects.json at the checkout root rather than
  // under runs/configured, so accept either location.
  const rootProjects = join(config.repoPath, 'projects.json')

  const launchFile = join(config.repoPath, 'runs', 'configured', 'launch.json')
  if (existsSync(launchFile)) {
    try {
      const parsed = JSON.parse(readFileSync(launchFile, 'utf8'))
      const argv = Array.isArray(parsed?.arguments) ? parsed.arguments.filter(a => typeof a === 'string') : []
      const python = typeof parsed?.python === 'string' && parsed.python ? parsed.python : (config.pythonPath || 'python')
      const cwd = typeof parsed?.working_directory === 'string' && parsed.working_directory
        ? parsed.working_directory
        : config.repoPath
      if (argv.length >= 5 && argv.includes('serve')) {
        return { configured: true, source: 'launch.json', python, argv, cwd, launchFile }
      }
    } catch {
      // A malformed wizard file is not fatal; fall through to the explicit form.
    }
  }

  const configuredProjects = join(config.repoPath, 'runs', 'configured', 'projects.json')
  const projects = config.projectsConfig
    ? resolvePath(config.projectsConfig)
    : (existsSync(configuredProjects) ? configuredProjects : rootProjects)
  if (!existsSync(projects)) {
    return { configured: false, reason: 'projects-missing', expected: configuredProjects }
  }
  return {
    configured: true,
    source: 'projects.json',
    python: config.pythonPath || 'python',
    argv: ['-u', '-B', entry, 'serve', '--projects-config', projects, '--host', '127.0.0.1', '--port', String(config.port)],
    cwd: config.repoPath,
  }
}

/**
 * Discover the configured projects from disk.
 *
 * The Client registers one sidebar entry per project, and sidebar entries must
 * exist before anything is running — otherwise the icons would appear only
 * after a successful start. `projects.json` is the same file the dashboard
 * itself reads, so this stays authoritative without the process being up.
 */
function readProjectsFromDisk(config) {
  const launch = resolveLaunch(config)
  const candidates = []
  if (config.projectsConfig) candidates.push(resolvePath(config.projectsConfig))
  // Prefer whatever the resolved launch actually passes to `--projects-config`.
  const argv = Array.isArray(launch.argv) ? launch.argv : []
  const flag = argv.indexOf('--projects-config')
  if (flag !== -1 && typeof argv[flag + 1] === 'string') candidates.push(argv[flag + 1])
  if (config.repoPath) {
    candidates.push(join(config.repoPath, 'runs', 'configured', 'projects.json'))
    candidates.push(join(config.repoPath, 'projects.json'))
  }

  for (const file of candidates) {
    if (!file || !existsSync(file)) continue
    try {
      const parsed = JSON.parse(readFileSync(file, 'utf8'))
      const list = Array.isArray(parsed?.projects) ? parsed.projects : []
      const projects = list
        .filter(p => p && typeof p.id === 'string' && p.id)
        .map(p => ({
          id: p.id,
          label: typeof p.label === 'string' && p.label ? p.label : p.id,
          engine: typeof p.engine === 'string' ? p.engine : null,
        }))
      if (projects.length) return { projects, source: file }
    } catch {
      // Try the next candidate rather than failing project discovery outright.
    }
  }
  return { projects: [], source: null }
}

/**
 * Read an upstream dashboard response, capped and decoded as text.
 *
 * `timeoutMs` exists because a few endpoints block on a cold cache rather than
 * answering from one: `/api/adaptation-tasks` has been measured at 40-50 s on
 * a first read, and `/api/branch-refresh` waits out a `git fetch`.
 */
function callUpstream(port, method, path, body, timeoutMs = UPSTREAM_TIMEOUT_MS) {
  return new Promise((resolve, reject) => {
    const headers = { Host: `127.0.0.1:${port}`, Accept: 'application/json' }
    let payload
    if (method === 'POST') {
      payload = Buffer.from(body ?? '{}', 'utf8')
      headers['Content-Type'] = 'application/json'
      headers['Content-Length'] = String(payload.byteLength)
      // The dashboard's CSRF defense accepts same-origin fetch metadata only.
      headers['Origin'] = `http://127.0.0.1:${port}`
      headers['Sec-Fetch-Site'] = 'same-origin'
    }
    const req = httpRequest(
      { host: '127.0.0.1', port, path, method, headers, timeout: timeoutMs },
      res => {
        const chunks = []
        let size = 0
        res.on('data', chunk => {
          size += chunk.byteLength
          if (size > MAX_UPSTREAM_BYTES) {
            res.destroy()
            reject(new Error('upstream response exceeded the size limit'))
            return
          }
          chunks.push(chunk)
        })
        res.on('end', () => resolve({
          status: res.statusCode ?? 502,
          contentType: res.headers['content-type'] ?? 'application/json; charset=utf-8',
          text: Buffer.concat(chunks).toString('utf8'),
        }))
        res.on('error', reject)
      },
    )
    req.on('timeout', () => req.destroy(new Error('upstream request timed out')))
    req.on('error', reject)
    if (payload) req.write(payload)
    req.end()
  })
}

/**
 * Strip machine-identifying detail from free-form agent prose.
 *
 * Fields like `last_agent_message` and `progress_summary` are written by the
 * agent, not by a schema, so they routinely embed absolute paths (in either
 * slash form), user home directories, SSH targets and bare hosts. Field-level
 * allowlisting cannot help here; the text itself has to be redacted.
 */
function redactText(value, limit) {
  if (typeof value !== 'string') return null
  const clean = value
    // Windows absolute paths, both `C:\x` and the `C:/x` form agents emit.
    .replace(/[A-Za-z]:[\\/][^\s"'<>|)\]]*/g, '[path]')
    // UNC shares.
    .replace(/\\\\[^\s"'<>|)\]]+/g, '[path]')
    // POSIX home and root-owned working directories.
    .replace(/(?:\/home\/|\/root\/|\/Users\/)[^\s"'<>|)\]]*/g, '[path]')
    // `user@host` SSH targets and bare IPv4 literals.
    .replace(/\b[\w.-]+@[\w.-]+\b/g, '[host]')
    .replace(/\b(?:\d{1,3}\.){3}\d{1,3}\b/g, '[host]')
  return clean.slice(0, limit)
}

/**
 * A readable name for one task.
 *
 * Upstream supplies `title` only for native observer rows and `thread_name`
 * only once an agent thread has been named, so most dashboard sessions have
 * neither. Falling back to the session id puts a 32-hex string on screen,
 * which is unreadable and identical-looking between rows. `backend` and
 * `branch` are always present and are what actually distinguishes a task.
 */
function taskLabel(task) {
  const given = task?.title ?? task?.thread_name
  if (typeof given === 'string' && given.trim()) return given.trim().slice(0, 200)
  const backend = typeof task?.backend === 'string' ? task.backend : null
  const branch = typeof task?.branch === 'string' ? task.branch : null
  if (backend && branch) return `${backend} · ${branch}`
  if (backend) return backend
  // Last resort: a short id is still better than 32 undifferentiated hex
  // characters, and the full id stays available in the detail view.
  const id = typeof task?.id === 'string' ? task.id : ''
  return id ? `#${id.slice(0, 8)}` : '—'
}

/** Reduce one backend row to the fields that decide adaptation progress. */
function summarizeBackend(backend) {
  const pick = value => (value && typeof value === 'object'
    ? { status: value.status ?? null, detail: value.detail ?? null, url: value.url ?? null }
    : null)
  const gate = backend?.gate && typeof backend.gate === 'object' ? backend.gate : null
  return {
    id: backend?.id ?? null,
    label: backend?.label ?? null,
    pr: backend?.pr ? { number: backend.pr.number, state: backend.pr.state, merged: backend.pr.merged, url: backend.pr.url } : null,
    integration: pick(backend?.integration),
    ci: pick(backend?.current_ci || backend?.ci),
    acceptance: backend?.acceptance
      ? { status: backend.acceptance.status ?? null, summary: backend.acceptance.summary ?? null, rows_total: backend.acceptance.rows_total ?? 0 }
      : null,
    gate: gate
      ? { status: gate.status, passed: gate.passed, failed: gate.failed, missing: gate.missing, total: gate.total }
      : null,
  }
}

/** Reduce one branch status to a compact shape; per-case `results` are dropped. */
function summarizeBranch(status) {
  if (!status || typeof status !== 'object') return status ?? null
  return {
    branch: status.branch ?? null,
    head: status.head ?? null,
    head_url: status.head_url ?? null,
    engine: status.engine ?? null,
    upstream_version: status.upstream_version ?? null,
    observed_at: status.observed_at ?? null,
    branch_ci: status.branch_ci
      ? { status: status.branch_ci.status, detail: status.branch_ci.detail, url: status.branch_ci.url ?? null }
      : null,
    backends: Array.isArray(status.backends) ? status.backends.map(summarizeBackend) : [],
    // Upstream `errors[]` is raw git/GitHub diagnostic text and can carry
    // remote and repository detail; `collection.error` is the sanitized,
    // user-facing message. Report only that a diagnostic exists.
    error_count: Array.isArray(status.errors) ? status.errors.length : 0,
    collection: status.collection ?? null,
    branch_count: Array.isArray(status.branches) ? status.branches.length : 0,
  }
}

/**
 * Compact a dashboard payload for a tool result.
 *
 * The raw `/api/branch-status` and `/api/projects-status` documents carry every
 * model/mode/scenario row for every backend and routinely exceed 500 KB, which
 * is useless context. Keep the decision-bearing fields only.
 */
function summarizeUpstream(path, body) {
  if (!body || typeof body !== 'object') return body
  if (path === '/api/projects-status' && Array.isArray(body.projects)) {
    return {
      projects: body.projects.map(project => ({
        id: project.id,
        label: project.label,
        engine: project.engine,
        error: project.error ?? null,
        status: summarizeBranch(project.status),
        task_count: Array.isArray(project.adaptation_tasks) ? project.adaptation_tasks.length : 0,
      })),
    }
  }
  if (path === '/api/branch-status') return summarizeBranch(body)
  if (path === '/api/devices') {
    // `accel` nests a per-card array with utilization and process lists; keep
    // the capacity counts and drop the card detail and PIDs.
    const counts = accel => (accel && typeof accel === 'object'
      ? {
        card_name: accel.card_name ?? null,
        total: accel.total ?? 0,
        free: accel.free ?? 0,
        busy: accel.busy ?? 0,
        unknown: accel.unknown ?? 0,
        residual: accel.residual ?? 0,
      }
      : null)
    const hosts = Array.isArray(body.hosts) ? body.hosts : []
    return {
      summary: body.summary ?? null,
      updated_at: body.updated_at ?? null,
      refreshing: body.refreshing ?? null,
      error: body.error ?? null,
      host_count: hosts.length,
      hosts: hosts.map(host => ({
        id: host.id,
        label: host.label,
        backend: host.backend_label ?? host.backend ?? null,
        verdict: host.verdict ?? null,
        stale: host.stale ?? null,
        accel: counts(host.accel),
      })),
    }
  }
  if (path === '/api/adaptation-tasks' && Array.isArray(body.tasks)) {
    // Task rows carry SSH aliases, remote workspaces and local rollout paths.
    // The panel shows identity and progress only, so project those fields and
    // keep machine and filesystem detail inside the dashboard process.
    return {
      error: body.error ?? null,
      task_count: body.tasks.length,
      tasks: body.tasks.map(task => ({
        id: task.id ?? null,
        // Most dashboard sessions carry neither `title` nor `thread_name`, so
        // a raw 32-hex id used to reach the screen. Build a readable label
        // from what a task always has: its backend and target branch.
        title: taskLabel(task),
        branch: task.branch ?? null,
        backend: task.backend ?? null,
        status: task.status ?? null,
        stage: task.stage ?? null,
        provider: task.provider ?? null,
        source_label: task.source_label ?? null,
        historical: task.historical ?? null,
        updated_at: task.updated_at ?? task.observed_at ?? null,
        progress_summary: redactText(task.progress_summary, 400),
        error: redactText(task.error, 400),
        // Drill-in needs to know whether this task can be driven at all and
        // which transcript belongs to it. `thread_id` is an opaque session
        // handle, not a machine identity, so it is safe to expose.
        controllable: task.controllable === true,
        source: task.source ?? null,
        work_branch: task.work_branch ?? null,
        stale: task.stale === true,
        // `thread_id` is the agent's CLI thread UUID, not a dashboard session
        // id, so it is useless as an address. Forward only whether a thread
        // exists: that is the "has the agent actually started" signal.
        has_thread: typeof task.thread_id === 'string' && task.thread_id !== '',
        // Non-null means a turn is in flight.
        turn_active: task.turn_id !== null && task.turn_id !== undefined,
        // `monitor` mode makes upstream refuse start and message. It applies
        // only when the policy branch equals the session branch, so forward
        // what upstream already resolved rather than recomputing it.
        policy_mode: task.task_policy?.mode ?? null,
        policy_reason: redactText(task.task_policy?.reason, 400),
        policy_pr_url: typeof task.task_policy?.pr_url === 'string' ? task.task_policy.pr_url : null,
        policy_pr: typeof task.task_policy?.pr === 'number' ? task.task_policy.pr : null,
        // Handed to the Codex desktop app: the dashboard no longer drives it.
        desktop_owned: task.execution_owner === 'codex_desktop',
        // Approvals block a turn; the panel says so instead of looking hung.
        pending_count: Array.isArray(task.pending_requests) ? task.pending_requests.length : 0,
        // The agent's own prose. `goal` is deliberately excluded: it embeds
        // raw device-probe output including host identities.
        last_agent_message: redactText(task.last_agent_message, 4000),
        message_updated_at: task.message_updated_at ?? null,
        plan: Array.isArray(task.plan)
          ? task.plan.slice(0, 40).map(step => (step && typeof step === 'object'
            ? { step: redactText(step.step ?? step.title, 300), status: step.status ?? null }
            : { step: redactText(step, 300), status: null }))
          : [],
      })),
    }
  }
  if (path === TRANSCRIPT_PATH && Array.isArray(body.events)) {
    return {
      session_id: body.session_id ?? null,
      status: body.status ?? null,
      warnings: Array.isArray(body.warnings) ? body.warnings.slice(0, 8) : [],
      has_more: body.has_more === true,
      next_cursor: typeof body.next_cursor === 'string' ? body.next_cursor : null,
      provider: body.provider ?? null,
      events: body.events
        // Drop pure telemetry. These carry no user-visible meaning, and the
        // account/rate-limit rows describe the plan and credit balance.
        .filter(event => !TELEMETRY_METHODS.has(String(event?.method ?? '')))
        .slice(-80)
        .map(event => ({
          at: event?.at ?? null,
          method: event?.method ?? null,
          scope: event?.source_scope ?? null,
          // `params` is NOT forwarded. Only these two fields hold agent prose;
          // every sibling (command, cwd, aggregatedOutput, diff, agentPath,
          // scriptPath, commandActions[], execpolicy amendments) carries
          // absolute paths, captured stdout or raw diffs. The panel renders
          // other methods as a bare label.
          text: redactText(event?.params?.delta ?? event?.params?.item?.text, 4000),
        })),
    }
  }
  if (path === '/api/agent-providers' && body.providers && typeof body.providers === 'object') {
    // Keep availability and capability only. `version` strings carry the OS
    // build and architecture, and settings objects carry base URLs and key
    // storage locations, so neither is forwarded.
    return {
      providers: Object.entries(body.providers).map(([id, p]) => ({
        id,
        available: p?.available === true,
        configured: p?.configured ?? null,
        authenticated: p?.authenticated ?? null,
        account_type: p?.account_type ?? null,
        model: p?.model ?? null,
        permission_mode: p?.permission_mode ?? null,
        supports_resume: p?.supports_resume ?? p?.capabilities?.supports_resume ?? null,
        follow_up_mode: p?.follow_up_mode ?? p?.capabilities?.follow_up_mode ?? null,
        error: redactText(p?.error, 300),
      })),
    }
  }
  if (path === '/api/branches' && Array.isArray(body.branches)) {
    return { branch: body.branch ?? null, branch_count: body.branches.length, branches: body.branches.slice(0, 60) }
  }
  return body
}

export function apply(ctx, rawConfig) {
  const config = normalizeConfig(rawConfig)

  /** The single managed dashboard process, or null while stopped. */
  let current = null
  let lastError = null

  function readOutput(handle) {
    const parts = []
    for (const reader of [handle.collected?.stdout, handle.collected?.stderr]) {
      if (!reader) continue
      try {
        const read = reader.readFrom(0)
        if (read.text) parts.push(read.text)
      } catch {
        // A reader that cannot be read yields no diagnostics; keep the rest.
      }
    }
    return parts.join('\n').slice(-8000)
  }

  /**
   * Whether a dashboard already answers on the configured port.
   *
   * A deployment started outside this plugin (the repo's own launcher, a
   * scheduled task) owns the port legitimately. Adopting it keeps the panel
   * useful and avoids spawning a second process that would fail to bind.
   */
  let adopted = false

  /**
   * Whether the port is free to bind.
   *
   * `flflow serve` builds its server with `allow_reuse_address = 1`, so on
   * Windows it binds a port another process is already serving instead of
   * failing: the result is two dashboards on one port and nondeterministic
   * routing. The repo's own launcher guards this with an exclusive bind, and
   * spawning the server directly has to do the same.
   */
  function portIsFree(port) {
    return new Promise(done => {
      const probe = createSocketServer()
      probe.once('error', () => done(false))
      probe.once('listening', () => probe.close(() => done(true)))
      // `exclusive` asks for a non-shared bind, which is what detects a
      // listener that SO_REUSEADDR would otherwise hide.
      probe.listen({ host: '127.0.0.1', port, exclusive: true })
    })
  }

  async function probeUpstream() {
    try {
      const upstream = await callUpstream(config.port, 'GET', '/api/projects')
      return upstream.status < 500
    } catch {
      return false
    }
  }

  function snapshot() {
    const launch = resolveLaunch(config)
    return {
      port: config.port,
      repoPath: config.repoPath,
      configured: launch.configured === true,
      configReason: launch.configured ? null : (launch.reason ?? 'unknown'),
      configExpected: launch.expected ?? null,
      launchSource: launch.source ?? null,
      // An adopted dashboard is reachable but not owned, so the panel offers
      // no stop control for it.
      adopted: adopted && current === null,
      running: current !== null || (adopted && current === null),
      managed: current !== null,
      startedAt: current?.startedAt ?? null,
      exit: current ? null : lastError,
      output: current ? readOutput(current.handle) : (lastError?.output ?? null),
    }
  }

  /** Refresh the adoption flag; called before every state report. */
  async function syncAdoption() {
    adopted = current === null ? await probeUpstream() : false
    return adopted
  }

  /**
   * One project's raw, unsummarized task rows.
   *
   * Routes that must make a decision need fields the panel never receives
   * (`worktree`, `task_policy`, `execution_owner`), so they resolve the task
   * here instead of trusting what the browser sent. Rejects rather than
   * returning a status, so each caller maps the failure to its own response.
   */
  async function listTasks(projectId) {
    const upstream = await callUpstream(
      config.port,
      'GET',
      `/api/adaptation-tasks?project=${encodeURIComponent(projectId)}`,
      undefined,
      SLOW_UPSTREAM_TIMEOUT_MS,
    )
    if (upstream.status >= 400) throw new Error('cannot read the task list')
    const body = JSON.parse(upstream.text)
    return Array.isArray(body?.tasks) ? body.tasks : []
  }

  async function start() {
    if (current) return { ok: true, already: true, state: snapshot() }
    // Never spawn against a port that already answers: that deployment is
    // another owner's, and a second bind would simply fail.
    if (await syncAdoption()) {
      return { ok: true, already: true, adopted: true, state: snapshot() }
    }
    const launch = resolveLaunch(config)
    if (!launch.configured) {
      return { ok: false, error: `dashboard is not configured (${launch.reason})`, state: snapshot() }
    }
    // Something answers on the port but it is not a dashboard, so adoption did
    // not catch it. Spawning anyway would produce a silent duplicate bind.
    if (!(await portIsFree(config.port))) {
      return {
        ok: false,
        error: `port ${config.port} is already in use by another process; stop it or change the port in the plugin configuration`,
        state: snapshot(),
      }
    }

    let executable
    try {
      executable = await ctx.subprocess.resolveExecutable(launch.python)
    } catch (error) {
      lastError = { reason: `cannot resolve the Python executable: ${error?.message ?? error}`, output: null }
      return { ok: false, error: lastError.reason, state: snapshot() }
    }

    const controller = new AbortController()
    let handle
    try {
      handle = ctx.subprocess.spawn({
        argv: [executable, ...launch.argv],
        cwd: launch.cwd,
        stdio: {
          stdin: 'ignore',
          stdout: { maxBytes: OUTPUT_LIMIT },
          stderr: { maxBytes: OUTPUT_LIMIT },
        },
        graceMs: 5_000,
        signal: controller.signal,
      })
    } catch (error) {
      lastError = { reason: `spawn failed: ${error?.message ?? error}`, output: null }
      return { ok: false, error: lastError.reason, state: snapshot() }
    }

    const entry = { handle, controller, startedAt: new Date().toISOString() }
    current = entry
    lastError = null

    handle.done.then(
      outcome => {
        if (current !== entry) return
        lastError = {
          reason: `dashboard exited (code ${outcome.exitCode ?? 'null'}${outcome.signal ? `, signal ${outcome.signal}` : ''})`,
          output: readOutput(handle),
        }
        current = null
      },
      error => {
        if (current !== entry) return
        lastError = { reason: `dashboard failed: ${error?.message ?? error}`, output: readOutput(handle) }
        current = null
      },
    )

    // The HTTP listener binds before the first request can succeed; give the
    // interpreter a moment so an immediate config error surfaces as an exit
    // rather than as a confusing "running" state in the panel.
    await Promise.race([handle.done.catch(() => undefined), sleep(1200)])
    return { ok: current !== null, error: current ? null : lastError?.reason ?? 'the dashboard exited immediately', state: snapshot() }
  }

  async function stop() {
    const entry = current
    if (!entry) {
      // Refuse to kill a dashboard this plugin did not start.
      if (await syncAdoption()) {
        return { ok: false, error: 'this dashboard was started outside the plugin; stop it where it was started', state: snapshot() }
      }
      return { ok: true, already: true, state: snapshot() }
    }
    current = null
    entry.controller.abort()
    try {
      entry.handle.terminate()
      await entry.handle.waitForExit()
    } catch {
      // Termination races a natural exit; either way the handle is finished.
    }
    lastError = null
    return { ok: true, state: snapshot() }
  }

  function sleep(ms) {
    return new Promise(done => setTimeout(done, ms))
  }

  // --- Local deployment ----------------------------------------------------
  //
  // The repo ships a non-interactive configure wizard, so the panel can take a
  // checkout from "cloned" to "serving" without a terminal. Everything here
  // runs the repo's own scripts; nothing reimplements its configuration.

  /** Run a short-lived Python helper from the checkout and collect its output. */
  async function runPython(args, timeoutMs = 180_000) {
    const launch = resolveLaunch(config)
    const python = launch.python || config.pythonPath || 'python'
    let executable
    try {
      executable = await ctx.subprocess.resolveExecutable(python)
    } catch (error) {
      return { ok: false, error: `cannot resolve the Python executable: ${error?.message ?? error}` }
    }
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), timeoutMs)
    try {
      const handle = ctx.subprocess.spawn({
        argv: [executable, ...args],
        cwd: config.repoPath,
        stdio: {
          stdin: 'ignore',
          stdout: { maxBytes: OUTPUT_LIMIT },
          stderr: { maxBytes: OUTPUT_LIMIT },
        },
        graceMs: 5_000,
        signal: controller.signal,
      })
      const outcome = await handle.done
      return {
        ok: outcome?.exitCode === 0,
        exitCode: outcome?.exitCode ?? null,
        // configure.py writes "Configuration error: <msg>" to stderr and the
        // repo deliberately keeps credentials out of those messages, so this
        // is safe to surface verbatim.
        output: readOutput(handle),
      }
    } catch (error) {
      return { ok: false, error: String(error?.message ?? error) }
    } finally {
      clearTimeout(timer)
    }
  }

  /**
   * Report what a local deployment still needs.
   *
   * Plugin checkout paths cannot be guessed, so the panel collects them; this
   * reports which inputs are outstanding and which are already satisfied.
   */
  function deployPreflight() {
    const checks = []
    const repoOk = Boolean(config.repoPath) && existsSync(config.repoPath)
    checks.push({ id: 'repo', ok: repoOk, detail: config.repoPath || null })
    const entry = repoOk ? join(config.repoPath, 'scripts', 'configure.py') : ''
    checks.push({ id: 'configure-script', ok: Boolean(entry) && existsSync(entry) })
    const launch = resolveLaunch(config)
    checks.push({ id: 'configured', ok: launch.configured === true, detail: launch.configured ? launch.source : launch.reason })
    return {
      ready: checks.every(check => check.ok),
      checks,
      // Projects the wizard can configure, with the inputs each one needs.
      projects: [
        { id: 'vllm', label: 'vLLM-Plugin-FL', repo: 'https://github.com/flagos-ai/vllm-plugin-FL' },
        { id: 'sglang', label: 'SGLang-Plugin-FL', repo: 'https://github.com/flagos-ai/sglang-plugin-FL' },
      ],
    }
  }

  /**
   * Validate one deploy request field as a safe scalar for argv.
   *
   * argv is passed as an array, never a shell string, so quoting is not the
   * concern. A leading dash is: argparse would read it as the next flag rather
   * than as this flag's value. Git refs and paths never need one.
   */
  function scalar(value) {
    if (typeof value !== 'string') return null
    const trimmed = value.trim()
    if (!trimmed || trimmed.length > 400) return null
    if (/[\u0000-\u001f]/.test(trimmed)) return null
    if (trimmed.startsWith('-')) return null
    return trimmed
  }

  /**
   * Configure the checkout non-interactively, then start it.
   *
   * `selections` is `[{ id, repoPath, remote, branch }]` for the projects the
   * user chose in the panel.
   */
  async function deploy(input) {
    if (!config.repoPath || !existsSync(config.repoPath)) {
      return { ok: false, error: 'set repoPath in the plugin configuration first', state: snapshot() }
    }
    const configureScript = join(config.repoPath, 'scripts', 'configure.py')
    if (!existsSync(configureScript)) {
      return { ok: false, error: 'this checkout has no scripts/configure.py', state: snapshot() }
    }
    if (current || await syncAdoption()) {
      return { ok: false, error: 'a dashboard is already running on this port', state: snapshot() }
    }
    if (!(await portIsFree(config.port))) {
      return {
        ok: false,
        error: `port ${config.port} is already in use by another process; stop it or change the port in the plugin configuration`,
        state: snapshot(),
      }
    }

    const selections = Array.isArray(input?.projects) ? input.projects : []
    if (!selections.length) {
      return { ok: false, error: 'choose at least one project', state: snapshot() }
    }

    const ids = []
    const perProject = []
    for (const entry of selections) {
      const id = scalar(entry?.id)
      if (id !== 'vllm' && id !== 'sglang') {
        return { ok: false, error: `unsupported project id: ${String(entry?.id)}`, state: snapshot() }
      }
      const repo = scalar(entry?.repoPath)
      if (!repo) return { ok: false, error: `${id}: a plugin checkout path is required`, state: snapshot() }
      if (!existsSync(repo)) return { ok: false, error: `${id}: that path does not exist: ${repo}`, state: snapshot() }
      const branch = scalar(entry?.branch)
      if (!branch) return { ok: false, error: `${id}: a target branch is required`, state: snapshot() }
      const remote = scalar(entry?.remote) || 'origin'
      ids.push(id)
      perProject.push(`--${id}-repo`, repo, `--${id}-remote`, remote, `--${id}-branch`, branch)
    }

    const args = [
      '-B', configureScript,
      '--non-interactive',
      '--projects', ...ids,
      ...perProject,
      '--output-dir', 'runs/configured',
      '--port', String(config.port),
    ]
    if (input?.overwrite === true) args.push('--overwrite')

    const configured = await runPython(args)
    if (!configured.ok) {
      return {
        ok: false,
        stage: 'configure',
        error: configured.error || `the configure wizard exited with code ${configured.exitCode}`,
        output: configured.output ?? null,
        state: snapshot(),
      }
    }

    const started = await start()
    return {
      ok: started.ok === true,
      stage: started.ok ? 'running' : 'start',
      error: started.error ?? null,
      output: configured.output ?? null,
      state: snapshot(),
    }
  }

  // --- HTTP surface for the Client panel -----------------------------------
  //
  // These routes live on the Harness web server, so the panel reaches them
  // same-origin. They are reachable by any local process that can reach the
  // Harness port, exactly like the rest of the Harness API surface; the proxy
  // therefore allows only the dashboard's read endpoints plus its two refresh
  // endpoints, and never forwards an arbitrary upstream path.

  function sendJson(res, status, value) {
    const text = JSON.stringify(value)
    res.writeHead(status, {
      'Content-Type': 'application/json; charset=utf-8',
      'Content-Length': Buffer.byteLength(text),
      'Cache-Control': 'no-store',
      'X-Content-Type-Options': 'nosniff',
    })
    res.end(text)
  }

  function readBody(req) {
    return new Promise((done, fail) => {
      const chunks = []
      let size = 0
      req.on('data', chunk => {
        size += chunk.byteLength
        if (size > 64 * 1024) {
          fail(new Error('request body too large'))
          req.destroy()
          return
        }
        chunks.push(chunk)
      })
      req.on('end', () => done(Buffer.concat(chunks).toString('utf8')))
      req.on('error', fail)
    })
  }

  const disposeRoute = ctx.webServer.register({
    kind: 'prefix',
    path: ROUTE_PREFIX,
    handler: async (req, res) => {
      let url
      try {
        url = new URL(req.url ?? '/', 'http://127.0.0.1')
      } catch {
        sendJson(res, 400, { error: 'malformed request target' })
        return
      }
      // `req.url` carries the full path, so strip the registered prefix and
      // any separators around the remaining action segment.
      const action = (url.pathname.startsWith(ROUTE_PREFIX)
        ? url.pathname.slice(ROUTE_PREFIX.length)
        : url.pathname
      ).replace(/^\/+|\/+$/g, '')

      try {
        if (req.method === 'GET' && action === 'state') {
          await syncAdoption()
          sendJson(res, 200, snapshot())
          return
        }

        if (req.method === 'GET' && action === 'deploy-preflight') {
          sendJson(res, 200, deployPreflight())
          return
        }

        // The panel needs the project list before anything is running, so this
        // answers from disk and only enriches from the live dashboard.
        if (req.method === 'GET' && action === 'projects') {
          const disk = readProjectsFromDisk(config)
          let projects = disk.projects
          if (current || await syncAdoption()) {
            try {
              const upstream = await callUpstream(config.port, 'GET', '/api/projects')
              const live = JSON.parse(upstream.text)
              if (Array.isArray(live?.projects) && live.projects.length) {
                const byId = new Map(projects.map(p => [p.id, p]))
                projects = live.projects
                  .filter(p => p && typeof p.id === 'string')
                  .map(p => ({
                    id: p.id,
                    label: p.label || byId.get(p.id)?.label || p.id,
                    engine: p.engine ?? byId.get(p.id)?.engine ?? null,
                    default_branch: p.default_branch ?? null,
                    github_repo: p.github_repo ?? null,
                    adaptation_tasks: p.adaptation_tasks === true,
                  }))
              }
            } catch {
              // Keep the disk list; the panel still works without enrichment.
            }
          }
          sendJson(res, 200, { projects, source: disk.source ? 'disk' : null })
          return
        }

        if (req.method === 'GET' && action === 'read') {
          const path = url.searchParams.get('path') ?? ''
          if (!READ_PATHS.has(path)) {
            sendJson(res, 400, { error: 'path is not an allowed dashboard read endpoint' })
            return
          }
          if (!current && !(await syncAdoption())) {
            sendJson(res, 503, { error: 'dashboard is not running', state: snapshot() })
            return
          }
          const project = url.searchParams.get('project')
          const branch = url.searchParams.get('branch')
          const query = new URLSearchParams()
          if (project) query.set('project', project)
          if (branch) query.set('branch', branch)
          const suffix = query.size ? `?${query.toString()}` : ''
          const upstream = await callUpstream(
            config.port,
            'GET',
            `${path}${suffix}`,
            undefined,
            SLOW_READ_PATHS.has(path) ? SLOW_UPSTREAM_TIMEOUT_MS : UPSTREAM_TIMEOUT_MS,
          )
          // The panel renders the summary fields only, so compact the payload
          // here rather than shipping the full per-case matrix to the browser.
          let text = upstream.text
          // An upstream error body is a plain `{error}`; summarizing it would
          // strip that field and leave the panel with a shapeless object.
          if (upstream.status < 400 && url.searchParams.get('full') !== 'true') {
            try {
              text = JSON.stringify(summarizeUpstream(path, JSON.parse(upstream.text)))
            } catch {
              // Not JSON (or unparseable): forward it unchanged.
            }
          }
          res.writeHead(upstream.status, {
            'Content-Type': upstream.contentType,
            'Content-Length': Buffer.byteLength(text),
            'Cache-Control': 'no-store',
            'X-Content-Type-Options': 'nosniff',
          })
          res.end(text)
          return
        }

        /*
         * One task's transcript.
         *
         * Separate from the generic read route because the upstream path is
         * per-session and the summarizer drops `params` wholesale: only
         * `params.delta` and `params.item.text` hold agent prose, while the
         * siblings carry absolute paths, captured stdout and raw diffs.
         */
        if (req.method === 'GET' && action === 'transcript') {
          const taskId = url.searchParams.get('task') ?? ''
          const project = url.searchParams.get('project')
          const cursor = url.searchParams.get('cursor')
          const limit = url.searchParams.get('limit')
          if (!/^[0-9a-f]{32}$/.test(taskId)) {
            sendJson(res, 400, { error: 'task must be a dashboard session id' })
            return
          }
          // Upstream enforces 1..80 and rejects anything else with a 400.
          if (limit !== null && !/^([1-9]|[1-7][0-9]|80)$/.test(limit)) {
            sendJson(res, 400, { error: 'limit must be between 1 and 80' })
            return
          }
          if (!current && !(await syncAdoption())) {
            sendJson(res, 503, { error: 'dashboard is not running' })
            return
          }
          const query = new URLSearchParams()
          if (project) query.set('project', project)
          query.set('limit', limit ?? '40')
          // Cursors are HMAC-signed upstream, so an invalid one is rejected
          // there; forward it verbatim rather than guessing its shape.
          if (cursor) query.set('cursor', cursor)
          const upstream = await callUpstream(
            config.port,
            'GET',
            `/api/codex-sessions/${taskId}/events?${query.toString()}`,
            undefined,
            SLOW_UPSTREAM_TIMEOUT_MS,
          )
          let text = upstream.text
          if (upstream.status < 400) {
            try {
              text = JSON.stringify(summarizeUpstream(TRANSCRIPT_PATH, JSON.parse(upstream.text)))
            } catch {
              text = JSON.stringify({ error: 'the transcript was not valid JSON' })
            }
          }
          res.writeHead(upstream.status, {
            'Content-Type': upstream.contentType,
            'Content-Length': Buffer.byteLength(text),
            'Cache-Control': 'no-store',
            'X-Content-Type-Options': 'nosniff',
          })
          res.end(text)
          return
        }

        /*
         * Which tasks already have a Harness workspace.
         *
         * The browser never sends or receives a filesystem path: it names a
         * task, and the Host resolves that task's worktree upstream. The reply
         * carries only ids, titles and booleans.
         */
        if (req.method === 'GET' && action === 'workspaces') {
          if (!ctx.get('workspaceRegistry')) {
            sendJson(res, 200, { supported: false, tasks: [] })
            return
          }
          const projectId = scalar(url.searchParams.get('project'))
          if (!projectId) {
            sendJson(res, 400, { error: 'project is required' })
            return
          }
          let tasks
          try {
            tasks = await listTasks(projectId)
          } catch (failure) {
            sendJson(res, 502, { error: redactText(failure.message, 400) ?? 'cannot read the task list' })
            return
          }
          const rows = []
          for (const task of tasks) {
            const path = typeof task?.worktree === 'string' ? task.worktree : null
            if (!path) continue
            let workspace
            try {
              workspace = await ctx.get('workspaceRegistry').resolveByPath(path)
            } catch {
              // A worktree the agent has not created yet, or one already
              // pruned: not an error, just nothing to adopt.
              rows.push({ task: task.id, exists: false, missing: true })
              continue
            }
            rows.push({
              task: task.id,
              exists: Boolean(workspace),
              // A title is safe to show; the path it points at is not.
              title: workspace ? workspace.title : null,
              missing: false,
            })
          }
          sendJson(res, 200, { supported: true, tasks: rows })
          return
        }

        if (req.method === 'POST') {
          /*
           * Adopt one task's git worktree as a Harness workspace.
           *
           * Registration only: it records an existing directory so the task's
           * code shows up in the sidebar. It never creates, moves or deletes a
           * worktree — the dashboard owns those, and `create` is idempotent on
           * the canonical path, so two tasks that share a code path resolve to
           * one workspace instead of a duplicate.
           */
          if (action === 'adopt-workspace') {
            if (!ctx.get('workspaceRegistry')) {
              sendJson(res, 501, { error: 'this Harness build has no workspace registry' })
              return
            }
            const raw = await readBody(req)
            let parsed = {}
            if (raw.trim()) {
              try {
                parsed = JSON.parse(raw)
              } catch {
                sendJson(res, 400, { error: 'body must be JSON' })
                return
              }
            }
            const projectId = scalar(parsed?.project)
            const taskId = scalar(parsed?.task)
            if (!projectId || !taskId) {
              sendJson(res, 400, { error: 'project and task are required' })
              return
            }
            let tasks
            try {
              tasks = await listTasks(projectId)
            } catch (failure) {
              sendJson(res, 502, { error: redactText(failure.message, 400) ?? 'cannot read the task list' })
              return
            }
            const task = tasks.find(row => row?.id === taskId)
            if (!task) {
              sendJson(res, 404, { error: 'no such task in this project' })
              return
            }
            // The path comes from the dashboard, never from the request body:
            // the route must not become a way to register an arbitrary
            // directory through the browser.
            const path = typeof task.worktree === 'string' ? task.worktree : ''
            if (!path) {
              sendJson(res, 409, { error: 'this task has no local worktree yet' })
              return
            }
            try {
              const workspace = await ctx.get('workspaceRegistry').create(path, taskLabel(task))
              sendJson(res, 200, { ok: true, title: workspace.title })
            } catch (failure) {
              // `create` rejects a path that is missing or not a directory.
              sendJson(res, 409, {
                ok: false,
                error: redactText(failure.message, 400) ?? 'the worktree could not be registered',
              })
            }
            return
          }
          if (action === 'start') {
            sendJson(res, 200, await start())
            return
          }
          if (action === 'stop') {
            sendJson(res, 200, await stop())
            return
          }
          if (action === 'deploy') {
            const raw = await readBody(req)
            let parsed = {}
            if (raw.trim()) {
              try {
                parsed = JSON.parse(raw)
              } catch {
                sendJson(res, 400, { error: 'body must be JSON' })
                return
              }
            }
            sendJson(res, 200, await deploy(parsed))
            return
          }
          /*
           * Send a follow-up message to the agent working one task.
           *
           * This is the only route here that drives an agent. It deliberately
           * cannot create a session, start remote work, delete, or restore:
           * those either spend money on remote machines or destroy history, so
           * they stay in the dashboard where the user sees the full context.
           * The task must already exist, already be controllable, and already
           * have a session thread.
           */
          if (action === 'task-message') {
            const raw = await readBody(req)
            let parsed = {}
            if (raw.trim()) {
              try {
                parsed = JSON.parse(raw)
              } catch {
                sendJson(res, 400, { error: 'body must be JSON' })
                return
              }
            }
            const projectId = scalar(parsed?.project)
            const taskId = scalar(parsed?.task)
            const message = typeof parsed?.message === 'string' ? parsed.message.trim() : ''
            if (!projectId || !taskId) {
              sendJson(res, 400, { error: 'project and task are required' })
              return
            }
            if (!message) {
              sendJson(res, 400, { error: 'message must be a non-empty string' })
              return
            }
            // Upstream rejects anything over 12000 characters.
            if (message.length > 12_000) {
              sendJson(res, 400, { error: 'message is too long (12000 characters maximum)' })
              return
            }
            if (!current && !(await syncAdoption())) {
              sendJson(res, 503, { error: 'dashboard is not running' })
              return
            }

            // Resolve the task upstream rather than trusting the browser: the
            // session id and the controllable flag decide whether this is
            // allowed at all, and a stale panel must not drive a dead task.
            let task = null
            try {
              task = (await listTasks(projectId)).find(row => row?.id === taskId) ?? null
            } catch (failure) {
              sendJson(res, 502, { error: redactText(failure.message, 400) ?? 'cannot read the task list' })
              return
            }
            if (!task) {
              sendJson(res, 404, { error: 'no such task in this project' })
              return
            }
            if (task.controllable !== true) {
              sendJson(res, 409, { error: 'this task is observed read-only and cannot be driven from the panel' })
              return
            }
            // The session id is the task's own id, a 32-hex string. Native
            // observer rows ("native-<backend>") and read-only share rows
            // ("task-<20hex>") never match, which is exactly the set that has
            // no addressable session. `thread_id` is the agent's CLI thread
            // UUID and is not valid in any dashboard URL.
            if (!/^[0-9a-f]{32}$/.test(taskId)) {
              sendJson(res, 409, {
                error: 'this task is observation-only and has no dashboard session to drive',
              })
              return
            }
            // `monitor` policy is a hard server-side block on start/message, so
            // reject it here with a reason instead of forwarding a doomed call.
            if (task.task_policy?.mode === 'monitor') {
              sendJson(res, 409, {
                error: 'this task is in monitor mode; the dashboard does not accept messages for it',
              })
              return
            }
            // Handed off to the Codex desktop app: the dashboard no longer
            // owns execution and will refuse.
            if (task.execution_owner === 'codex_desktop') {
              sendJson(res, 409, {
                error: 'this task was handed off to the Codex desktop app and is no longer driven from here',
              })
              return
            }

            // The upstream field is `text`, and it is the only key read. The
            // provider is fixed when the session is created and cannot be
            // changed per message, so it is deliberately not forwarded.
            const upstream = await callUpstream(
              config.port,
              'POST',
              `/api/codex-sessions/${taskId}/message${query}`,
              JSON.stringify({ text: message }),
              SLOW_UPSTREAM_TIMEOUT_MS,
            )
            // The reply echoes the whole session, which carries SSH aliases and
            // absolute paths. Confirm acceptance only; the panel re-reads the
            // task through the projecting read route.
            let accepted = { ok: upstream.status < 400 }
            if (upstream.status >= 400) {
              try {
                accepted = { ok: false, error: redactText(JSON.parse(upstream.text)?.error, 300) }
              } catch {
                accepted = { ok: false, error: 'the dashboard rejected the message' }
              }
            }
            sendJson(res, upstream.status, accepted)
            return
          }
          if (action === 'refresh') {
            const raw = await readBody(req)
            let parsed = {}
            if (raw.trim()) {
              try {
                parsed = JSON.parse(raw)
              } catch {
                sendJson(res, 400, { error: 'body must be JSON' })
                return
              }
            }
            const path = typeof parsed?.path === 'string' ? parsed.path : ''
            if (!REFRESH_PATHS.has(path)) {
              sendJson(res, 400, { error: 'path is not an allowed refresh endpoint' })
              return
            }
            if (!current && !(await syncAdoption())) {
              sendJson(res, 503, { error: 'dashboard is not running' })
              return
            }
            const forward = {}
            if (typeof parsed?.branch === 'string') forward.branch = parsed.branch
            if (typeof parsed?.profile_id === 'string') forward.profile_id = parsed.profile_id
            const project = typeof parsed?.project === 'string' && parsed.project
              ? `?project=${encodeURIComponent(parsed.project)}`
              : ''
            const upstream = await callUpstream(config.port, 'POST', `${path}${project}`, JSON.stringify(forward))
            // `/api/branch-refresh` answers with a full branch status, so
            // compact it the same way the read route does.
            let refreshed = upstream.text
            try {
              refreshed = JSON.stringify(summarizeUpstream('/api/branch-status', JSON.parse(upstream.text)))
            } catch {
              // Not JSON: forward unchanged.
            }
            res.writeHead(upstream.status, {
              'Content-Type': upstream.contentType,
              'Content-Length': Buffer.byteLength(refreshed),
              'Cache-Control': 'no-store',
              'X-Content-Type-Options': 'nosniff',
            })
            res.end(refreshed)
            return
          }
        }

        sendJson(res, 404, { error: 'unknown fl-upgrade action' })
      } catch (error) {
        sendJson(res, 502, { error: String(error?.message ?? error) })
      }
    },
  })
  ctx.effect(() => disposeRoute)

  // --- Agent tool ----------------------------------------------------------
  //
  // The same operations the panel offers, so the agent can drive the workbench
  // without the user switching panels. Read and lifecycle actions only; the
  // dashboard's task-creation and credential routes stay user-only.

  const disposeTool = ctx.tools.register({
    name: 'fl_upgrade_dashboard',
    description: [
      'Control and read the FL plugin adaptation workbench (the fl-plugin-upgrade-workflow dashboard).',
      'Actions: `status` reports whether the local dashboard process runs and whether it is configured;',
      '`start` and `stop` manage that process; `read` fetches one of its read-only JSON endpoints.',
      'Use `read` with path `/api/projects-status` for the cross-project overview, `/api/branch-status`',
      'with a `branch` for PR/CI/acceptance detail, and `/api/devices` for GPU fleet state.',
      'A `read` returns a compact per-backend summary; set `full: true` only when you need the raw',
      'payload, which can exceed hundreds of kilobytes for a branch with many backends.',
    ].join(' '),
    parameters: {
      type: 'object',
      properties: {
        action: { type: 'string', enum: ['status', 'start', 'stop', 'read'], description: 'The operation to perform.' },
        path: { type: 'string', description: 'For `read`: the dashboard endpoint, such as /api/projects-status.' },
        project: { type: 'string', description: 'Optional project id, such as vllm or sglang.' },
        branch: { type: 'string', description: 'Optional branch name for /api/branch-status.' },
        full: { type: 'boolean', description: 'Return the raw upstream payload instead of the compact summary.' },
      },
      required: ['action'],
      additionalProperties: false,
    },
    output: {
      schema: { type: 'object' },
      render: (_args, value) => [{ type: 'text', text: JSON.stringify(value, null, 2) }],
    },
    async execute(args) {
      const action = args?.action
      if (action === 'status') {
        await syncAdoption()
        return snapshot()
      }
      if (action === 'start') return await start()
      if (action === 'stop') return await stop()
      if (action === 'read') {
        const path = typeof args?.path === 'string' ? args.path : ''
        if (!READ_PATHS.has(path)) {
          return { error: 'path must be one of the allowed read endpoints', allowed: [...READ_PATHS] }
        }
        if (!current && !(await syncAdoption())) {
          return { error: 'dashboard is not running', state: snapshot() }
        }
        const query = new URLSearchParams()
        if (typeof args?.project === 'string' && args.project) query.set('project', args.project)
        if (typeof args?.branch === 'string' && args.branch) query.set('branch', args.branch)
        const suffix = query.size ? `?${query.toString()}` : ''
        const upstream = await callUpstream(config.port, 'GET', `${path}${suffix}`)
        try {
          const body = JSON.parse(upstream.text)
          return args?.full === true
            ? { status: upstream.status, body }
            : { status: upstream.status, summarized: true, body: summarizeUpstream(path, body) }
        } catch {
          return { status: upstream.status, text: upstream.text.slice(0, 20_000) }
        }
      }
      return { error: 'unknown action' }
    },
  })
  ctx.effect(() => disposeTool)

  // Always reclaim the child process when this plugin unloads.
  ctx.effect(() => () => {
    const entry = current
    current = null
    if (!entry) return
    entry.controller.abort()
    try {
      entry.handle.terminate()
    } catch {
      // Already gone.
    }
  })

  if (config.autoStart) {
    start().catch(() => {
      // A failed autostart is reported through `state`, not thrown at load.
    })
  }
}
