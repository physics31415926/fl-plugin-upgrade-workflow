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
import { existsSync, readFileSync, realpathSync } from 'node:fs'
import { realpath, rm, writeFile } from 'node:fs/promises'
import { basename, dirname, isAbsolute, join, resolve as resolvePath } from 'node:path'
import { createServer as createSocketServer } from 'node:net'
import { createHash } from 'node:crypto'
import { tmpdir, homedir } from 'node:os'

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

/**
 * Which collector a read path observes, for the status indicator.
 *
 * The panel's header reports whether collection is actually producing data,
 * which is not the same question as whether the process is alive: the server
 * answers immediately while an SSH fan-out to the GPU fleet fails on every
 * host, or while a GitHub token has expired. Attributing each read to its
 * collector is what separates "running" from "running and working".
 *
 * Only paths whose answer comes from a collection round appear here.
 * `/api/projects` and `/api/capabilities` describe configuration and would
 * report health for a collector that never ran.
 */
const COLLECTOR_PATHS = new Map([
  ['/api/devices', 'devices'],
  ['/api/branch-status', 'branches'],
  ['/api/projects-status', 'branches'],
  ['/api/adaptation-tasks', 'tasks'],
])

/** Upstream dashboard paths the panel may trigger. Collection refresh only. */
const REFRESH_PATHS = new Set(['/api/branch-refresh', '/api/devices/refresh'])

const MAX_UPSTREAM_BYTES = 4 * 1024 * 1024
const OUTPUT_LIMIT = 256 * 1024
const UPSTREAM_TIMEOUT_MS = 30_000

/*
 * Where plugin releases come from.
 *
 * Pinned in code rather than taken from config: this is the one place that
 * decides which code the Harness will install and run, so a profile setting
 * (or a request body) must not be able to repoint it at another repository.
 * The GitHub API is addressed over HTTPS and only its release metadata is
 * read; the download is then taken from the release asset list it returns.
 */
const RELEASE_OWNER = 'physics31415926'
const RELEASE_REPO = 'fl-plugin-upgrade-workflow'
const RELEASE_API = `https://api.github.com/repos/${RELEASE_OWNER}/${RELEASE_REPO}/releases`

/** Tag shape this plugin publishes under, so unrelated repo tags are ignored. */
const RELEASE_TAG_PREFIX = 'dsh-plugin-v'

/*
 * Only GitHub may serve the tarball.
 *
 * The asset URL arrives from a network response, so treating it as a plain
 * download target would let a compromised or mistaken API answer redirect the
 * installer anywhere.
 *
 * Two checks, because the advertised URL and the one that actually serves the
 * bytes are different hosts: a release asset is advertised on `github.com` and
 * redirects to a `*.githubusercontent.com` CDN host, whose exact name GitHub
 * has changed (`objects.` historically, `release-assets.` as observed). The
 * advertised host is pinned exactly; the landing host is matched by domain
 * suffix so a CDN rename cannot break updates, while still never leaving
 * GitHub-controlled DNS. The published digest is what actually authenticates
 * the bytes.
 */
const RELEASE_ASSET_HOSTS = new Set(['github.com'])

/** Whether a download may be served from this host. */
function isReleaseDownloadHost(hostname) {
  return RELEASE_ASSET_HOSTS.has(hostname)
    // A suffix match on the dotted domain: `evil-githubusercontent.com` and
    // `githubusercontent.com.evil.test` both fail it.
    || hostname.endsWith('.githubusercontent.com')
}

/** A release tarball is small; this cap exists so a bad URL cannot fill a disk. */
const MAX_ASSET_BYTES = 8 * 1024 * 1024

const RELEASE_TIMEOUT_MS = 20_000
const DOWNLOAD_TIMEOUT_MS = 120_000

/*
 * The dashboard repository, for the one-time auto-clone.
 *
 * The plugin needs a checkout of its own dashboard (fl-plugin-upgrade-workflow)
 * to run; requiring the user to clone it by hand and paste the path was a step
 * people missed. When `repoPath` is unset, the plugin clones this over HTTPS
 * into a default directory and uses it.
 *
 * Pinned, like the release source: the clone becomes code this plugin then
 * runs, so the URL must not be configurable from a profile setting or a
 * request. HTTPS so no key is needed for a public read-only clone.
 */
const DASHBOARD_REPO_URL = 'https://github.com/physics31415926/fl-plugin-upgrade-workflow.git'

/** How long a clone may take before it is abandoned. */
const CLONE_TIMEOUT_MS = 180_000

/**
 * The default checkout directory when `repoPath` is unset.
 *
 * Under the Harness home (`$DSH_HOME`, else `~/.dsh`), so the clone sits beside
 * the profile and credentials it belongs with and is obvious to find or
 * remove. A single, predictable location also means a second activation finds
 * the existing clone instead of making another.
 */
function defaultRepoPath() {
  const home = (typeof process.env.DSH_HOME === 'string' && process.env.DSH_HOME.trim())
    ? process.env.DSH_HOME.trim()
    : join(homedir(), '.dsh')
  return join(home, 'fl-plugin-upgrade-workflow')
}

/**
 * Timeout for upstream reads that block on a cold cache instead of answering
 * from one. `/api/adaptation-tasks` was measured at 40-50 s on a first read
 * (and ~2 s once warm), which the default would abort.
 */
const SLOW_UPSTREAM_TIMEOUT_MS = 90_000

function normalizeConfig(raw) {
  const value = raw && typeof raw === 'object' ? raw : {}
  /*
   * An explicit repoPath always wins. When it is unset, fall back to the
   * default checkout directory so the auto-clone has a fixed home and the
   * launcher looks there. `repoConfigured` (in the snapshot) still reflects
   * whether the user set one, so the panel can tell "default" from "chosen".
   */
  const explicitRepoPath = typeof value.repoPath === 'string' && value.repoPath.trim()
    ? resolvePath(value.repoPath.trim())
    : ''
  const repoPath = explicitRepoPath || defaultRepoPath()
  const port = Number.isInteger(value.port) && value.port >= 1 && value.port <= 65535
    ? value.port
    : 8766
  return {
    repoPath,
    // Whether the user set a path, as distinct from falling back to the
    // default. The auto-clone only ever writes the default directory; an
    // explicit path is the user's and is never cloned into.
    repoPathExplicit: Boolean(explicitRepoPath),
    port,
    pythonPath: typeof value.pythonPath === 'string' ? value.pythonPath.trim() : '',
    projectsConfig: typeof value.projectsConfig === 'string' ? value.projectsConfig.trim() : '',
    /*
     * Collection is part of the plugin, not a service the user runs.
     *
     * Device and PR collection live in the dashboard process, so the panel is
     * empty until something starts it. Defaulting this off made that the
     * user's job: install the plugin, then find and press Start. The data is
     * the reason the panel exists, so the process comes up with it.
     *
     * Only `=== false` disables it. An absent field means a profile written
     * before this default existed, and those should gain the behaviour; an
     * explicit `false` is a decision and is kept.
     */
    autoStart: value.autoStart !== false,
    /*
     * Environment variable name holding the GitHub token, resolved through the
     * Harness credential service.
     *
     * The dashboard reads `GITHUB_TOKEN` (then `GH_TOKEN`) from its own process
     * environment, and this plugin spawns that process, so a token the Harness
     * already holds can be handed over at spawn time instead of the user
     * exporting it into whatever shell happens to launch the app.
     *
     * A name, never a value: the secret stays in the Harness store, and this
     * config — which is plain YAML in the profile — only says which entry to
     * read. Leave empty to opt out entirely.
     */
    githubTokenEnv: typeof value.githubTokenEnv === 'string' && value.githubTokenEnv.trim()
      ? value.githubTokenEnv.trim()
      : 'GITHUB_TOKEN',
    /*
     * Whether to clone the dashboard repository when it is missing.
     *
     * On by default: the plugin needs its own dashboard checkout to run, and
     * requiring a manual clone was the step people missed. The clone only ever
     * targets the default directory and only when nothing is there — an
     * explicit `repoPath`, or an existing checkout, is never touched. Set
     * false to opt out and manage the checkout by hand.
     */
    autoClone: value.autoClone !== false,
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
 * Where one project's source checkout lives, read from the dashboard's own
 * config rather than asked for.
 *
 * `projects.json` points each project at a branch-monitor config, and that
 * file's `repo` names the checkout — usually relative to the dashboard repo,
 * which is why it is resolved against `repoPath` rather than the process cwd.
 *
 * Returns `null` when nothing is configured, so a caller can say "unknown"
 * instead of guessing a path and acting on it.
 */
function resolveProjectRepo(config, projectId) {
  if (!config.repoPath) return null
  const discovered = readProjectsFromDisk(config)
  if (!discovered.source) return null
  let entry = null
  try {
    const parsed = JSON.parse(readFileSync(discovered.source, 'utf8'))
    entry = (Array.isArray(parsed?.projects) ? parsed.projects : [])
      .find(p => p && p.id === projectId) ?? null
  } catch {
    return null
  }
  const monitorRef = typeof entry?.monitor_config === 'string' ? entry.monitor_config : ''
  if (!monitorRef) return null
  // The monitor path is itself relative to the config that named it.
  const monitorPath = isAbsolute(monitorRef)
    ? monitorRef
    : join(dirname(discovered.source), monitorRef)
  if (!existsSync(monitorPath)) return null
  let monitor
  try {
    monitor = JSON.parse(readFileSync(monitorPath, 'utf8'))
  } catch {
    return null
  }
  const repoRef = typeof monitor?.repo === 'string' ? monitor.repo.trim() : ''
  if (!repoRef) return null
  const repoPath = isAbsolute(repoRef) ? repoRef : resolvePath(join(dirname(monitorPath), repoRef))
  const branch = typeof monitor?.branch === 'string' ? monitor.branch : null
  const githubRepo = typeof monitor?.github_repo === 'string' ? monitor.github_repo : null
  return {
    path: repoPath,
    branch,
    githubRepo,
    /*
     * A sidebar title the user can recognise, with no path in it.
     *
     * Deliberately free of the branch. A workspace is a directory, and the
     * branch checked out in it changes with any `git checkout` the user or the
     * dashboard performs — a title claiming "0.4.0-dev" survives that change
     * and starts lying. The branch belongs on the conversations inside the
     * workspace, where each one really is about one branch.
     */
    title: entry?.label || projectId,
  }
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
 * Parse a release version into comparable parts.
 *
 * Accepts `1.2.3` and `1.2.3-rc.1`; anything else returns null so an
 * unexpected tag is ignored rather than guessed at. Prerelease identifiers are
 * kept as raw strings because only their presence and ordering matter here.
 */
function parseVersion(value) {
  const match = /^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$/.exec(String(value ?? '').trim())
  if (!match) return null
  return {
    major: Number(match[1]),
    minor: Number(match[2]),
    patch: Number(match[3]),
    pre: match[4] ? match[4].split('.') : null,
  }
}

/**
 * Semver-style ordering, returning negative when `a` precedes `b`.
 *
 * Only as much of semver as release selection needs: numeric triple first,
 * then the rule that a prerelease sorts *before* its own release, so
 * `0.2.0-rc.1` never looks newer than `0.2.0`. Without that rule an installed
 * release would be offered an "update" back to its own candidate.
 */
function compareVersions(a, b) {
  const left = parseVersion(a)
  const right = parseVersion(b)
  if (!left || !right) return 0
  for (const key of ['major', 'minor', 'patch']) {
    if (left[key] !== right[key]) return left[key] < right[key] ? -1 : 1
  }
  if (!left.pre && !right.pre) return 0
  if (!left.pre) return 1
  if (!right.pre) return -1
  for (let i = 0; i < Math.max(left.pre.length, right.pre.length); i += 1) {
    const x = left.pre[i]
    const y = right.pre[i]
    if (x === undefined) return -1
    if (y === undefined) return 1
    if (x === y) continue
    const nx = /^\d+$/.test(x)
    const ny = /^\d+$/.test(y)
    // Numeric identifiers compare numerically and always sort below
    // alphanumeric ones, which is what distinguishes rc.2 from rc.10.
    if (nx && ny) return Number(x) < Number(y) ? -1 : 1
    if (nx !== ny) return nx ? -1 : 1
    return x < y ? -1 : 1
  }
  return 0
}

/**
 * Pick the newest published plugin release from a GitHub releases payload.
 *
 * Drafts are not published and prereleases are not offered as automatic
 * updates, so both are skipped. Tag order from the API is not relied upon:
 * releases are sorted by creation date, which a retagged release can reverse.
 */
function selectRelease(payload) {
  if (!Array.isArray(payload)) return null
  let best = null
  for (const row of payload) {
    if (!row || typeof row !== 'object') continue
    if (row.draft === true || row.prerelease === true) continue
    const tag = typeof row.tag_name === 'string' ? row.tag_name : ''
    if (!tag.startsWith(RELEASE_TAG_PREFIX)) continue
    const version = tag.slice(RELEASE_TAG_PREFIX.length)
    if (!parseVersion(version)) continue
    /*
     * The asset must be a tarball served from a GitHub release host. An
     * unexpected host here is treated as no asset at all rather than being
     * followed, since this URL becomes an install source.
     */
    const asset = (Array.isArray(row.assets) ? row.assets : []).find(item => {
      const name = typeof item?.name === 'string' ? item.name : ''
      const url = typeof item?.browser_download_url === 'string' ? item.browser_download_url : ''
      if (!name.endsWith('.tgz') || !url) return false
      try {
        const parsed = new URL(url)
        // The advertised URL is pinned to `github.com` exactly; only the
        // redirect it leads to may land on a CDN host.
        return parsed.protocol === 'https:' && RELEASE_ASSET_HOSTS.has(parsed.hostname)
      } catch {
        return false
      }
    })
    if (!asset) continue
    const candidate = {
      version,
      tag,
      url: asset.browser_download_url,
      name: asset.name,
      size: Number.isFinite(asset.size) ? asset.size : null,
      // GitHub reports `sha256:<hex>` when it has one. Used to verify the
      // download, never to locate it.
      digest: typeof asset.digest === 'string' && /^sha256:[0-9a-f]{64}$/.test(asset.digest)
        ? asset.digest.slice(7)
        : null,
      publishedAt: typeof row.published_at === 'string' ? row.published_at : null,
      notes: typeof row.body === 'string' ? row.body.slice(0, 4000) : null,
    }
    if (!best || compareVersions(best.version, candidate.version) < 0) best = candidate
  }
  return best
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
/**
 * Compare two already-canonical directory paths.
 *
 * Windows paths are case-insensitive, so a registration made as `C:\Users`
 * must still match a `c:\users` spelling of the same directory. Separators are
 * normalized because the registry stores whatever `realpath` returned.
 */
function comparePaths(left, right) {
  if (typeof left !== 'string' || typeof right !== 'string') return false
  const normalize = value => value.replace(/[\\/]+$/, '').replace(/\//g, '\\')
  const a = normalize(left)
  const b = normalize(right)
  return process.platform === 'win32' ? a.toLowerCase() === b.toLowerCase() : a === b
}

/**
 * Drop a branch suffix an earlier version of this plugin wrote into a title.
 *
 * `create` reuses an existing registration without touching its title, so a
 * workspace adopted before the naming changed keeps "label · branch" forever.
 * This corrects exactly that one shape and nothing else: a title the user
 * renamed themselves, or one that never carried the branch, is left alone.
 *
 * Renaming is best-effort. A failure here must not make adoption fail, since
 * the registration itself already succeeded and is the thing the caller asked
 * for.
 */
async function retitleBranchNamed(workspace, resolved) {
  const branch = typeof resolved?.branch === 'string' ? resolved.branch : ''
  if (!branch || !resolved.title) return
  if (workspace.title !== `${resolved.title} · ${branch}`) return
  try {
    await workspace.setTitle(resolved.title)
  } catch {
    // Keep the stale title rather than failing the adoption.
  }
}

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
function summarizeBackend(backend, head) {
  const pick = value => (value && typeof value === 'object'
    ? { status: value.status ?? null, detail: value.detail ?? null, url: value.url ?? null }
    : null)
  const gate = backend?.gate && typeof backend.gate === 'object' ? backend.gate : null
  /*
   * The CI verdict is `ci`, not `current_ci`.
   *
   * Upstream reports two different facts. `ci` is the backend's adaptation CI
   * — the run on its PR, or the accepted/merged-PR run — and is the answer to
   * "did this backend's adaptation pass CI". `current_ci` is narrower: CI jobs
   * observed at the *current branch HEAD*, which for an already-merged backend
   * is almost always "unknown", because nobody reran per-backend jobs on the
   * branch tip. An earlier version forwarded `current_ci` first, so every
   * merged backend showed CI "unknown" while its adaptation had in fact
   * passed.
   *
   * This mirrors the dashboard's own `effectiveCi`: take `ci.status`, but
   * downgrade a branch-scoped pass to `stale` once the branch has moved past
   * the commit that passed. A merged-PR or accepted pass is not branch-scoped
   * and stays as reported.
   */
  const ci = backend?.ci && typeof backend.ci === 'object' ? backend.ci : null
  const effectiveCiStatus = ci
    && ci.status === 'passed'
    && ci.scope === 'branch'
    && ci.commit
    && head
    && ci.commit !== head
    ? 'stale'
    : (ci?.status ?? null)
  return {
    id: backend?.id ?? null,
    label: backend?.label ?? null,
    pr: backend?.pr ? { number: backend.pr.number, state: backend.pr.state, merged: backend.pr.merged, url: backend.pr.url } : null,
    integration: pick(backend?.integration),
    ci: ci
      ? { status: effectiveCiStatus, scope: ci.scope ?? null, detail: ci.detail ?? null, url: ci.url ?? null }
      : null,
    // The latest run at the branch tip, kept as a secondary signal. Only
    // meaningful once the adaptation CI is a merged-PR or accepted pass, which
    // is exactly when the dashboard surfaces it too.
    currentCi: backend?.current_ci && backend.current_ci !== backend?.ci
      ? pick(backend.current_ci)
      : null,
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
  const head = status.head ?? null
  return {
    branch: status.branch ?? null,
    head,
    head_url: status.head_url ?? null,
    engine: status.engine ?? null,
    upstream_version: status.upstream_version ?? null,
    observed_at: status.observed_at ?? null,
    branch_ci: status.branch_ci
      ? { status: status.branch_ci.status, detail: status.branch_ci.detail, url: status.branch_ci.url ?? null }
      : null,
    // The stale-pass downgrade needs the branch HEAD, so it is threaded in.
    backends: Array.isArray(status.backends) ? status.backends.map(backend => summarizeBackend(backend, head)) : [],
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

  /*
   * Auto-clone state, surfaced in the snapshot.
   *
   * `phase` is one of: idle (nothing to do / a checkout already present),
   * cloning (a clone is in flight), done (this activation cloned it), error
   * (the clone failed, with a redacted reason). The panel shows "fetching the
   * dashboard…" on a first run rather than a bare "not configured".
   */
  let cloneState = { phase: 'idle', error: null }
  const cloneSnapshot = () => ({ phase: cloneState.phase, error: cloneState.error })

  /**
   * Clone the dashboard repository into the default directory, once.
   *
   * Only ever runs when: auto-clone is enabled, the user did not set an
   * explicit `repoPath` (the default directory is ours to create; a chosen
   * one is not), and nothing is already at the target. It is a shallow HTTPS
   * clone of a public repository — no credentials, no history — into a path
   * fixed in code, so there is nothing a caller can redirect.
   *
   * A failure is recorded and swallowed: the panel still loads and the setup
   * prompt still explains the missing checkout, rather than the plugin failing
   * to activate.
   */
  async function cloneDashboardIfMissing() {
    if (!config.autoClone) return
    if (config.repoPathExplicit) return
    // Already there: a scripts/flflow.py is the marker resolveLaunch uses, so
    // treat its presence as "a usable checkout exists" and never re-clone.
    if (existsSync(join(config.repoPath, 'scripts', 'flflow.py'))) return
    // A non-empty directory that is not a checkout is the user's; do not
    // clobber it or clone into it.
    if (existsSync(config.repoPath)) {
      cloneState = { phase: 'error', error: 'the default directory exists but is not a dashboard checkout' }
      return
    }

    let git
    try {
      git = await ctx.subprocess.resolveExecutable('git')
    } catch {
      cloneState = { phase: 'error', error: 'git was not found on PATH; install Git or set repoPath to a manual checkout' }
      return
    }

    cloneState = { phase: 'cloning', error: null }
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), CLONE_TIMEOUT_MS)
    try {
      const handle = ctx.subprocess.spawn({
        // Shallow, single-branch, no checkout history: enough to run the
        // dashboard, fast, and small. The URL and destination are both fixed
        // constants, never caller input.
        argv: [git, 'clone', '--depth', '1', DASHBOARD_REPO_URL, config.repoPath],
        cwd: dirname(config.repoPath),
        stdio: {
          stdin: 'ignore',
          stdout: { maxBytes: OUTPUT_LIMIT },
          stderr: { maxBytes: OUTPUT_LIMIT },
        },
        graceMs: 5_000,
        signal: controller.signal,
      })
      const outcome = await handle.done
      if (outcome.exitCode === 0 && existsSync(join(config.repoPath, 'scripts', 'flflow.py'))) {
        cloneState = { phase: 'done', error: null }
      } else {
        // git's stderr can name a proxy or a path, so it is redacted.
        const err = redactText(readOutput(handle), 300)
        cloneState = { phase: 'error', error: err || `git clone exited with code ${outcome.exitCode ?? 'null'}` }
      }
    } catch (failure) {
      cloneState = {
        phase: 'error',
        error: controller.signal.aborted
          ? 'the clone timed out'
          : (redactText(failure?.message, 300) ?? 'the clone failed'),
      }
    } finally {
      clearTimeout(timer)
    }
  }

  /*
   * The dashboard's own stdout and stderr, as diagnostics for a failed start.
   *
   * Redacted, because this is the least predictable text the plugin forwards.
   * A Python traceback names every file on the way down, a config error quotes
   * the path it could not read, and an SSH failure can name a host — none of
   * which the browser needs in order to show that the process died. Keeping
   * the message while dropping the path is the whole point of `redactText`.
   */
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
    return redactText(parts.join('\n').slice(-8000), 8000) ?? ''
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

  /*
   * The installed version of this plugin, read from the manifest beside this
   * module rather than hardcoded, so a build cannot disagree with its own
   * package.json. Read once: the file does not change under a running plugin.
   */
  const installedVersion = (() => {
    try {
      const manifest = JSON.parse(readFileSync(new URL('./package.json', import.meta.url), 'utf8'))
      return typeof manifest?.version === 'string' ? manifest.version : null
    } catch {
      return null
    }
  })()

  /*
   * Whether this profile carries the plugin as a `link:` dependency.
   *
   * A linked install points at a checkout someone edits; installing a
   * published tarball over it would take that directory out of the loader and
   * leave their work orphaned, so the button refuses and says why.
   *
   * Decided by resolving this module's real path: pnpm materialises a linked
   * dependency as a junction to the checkout, which resolves *outside* the
   * profile's `node_modules`, while a tarball install resolves inside it. That
   * is a local filesystem fact, so it needs no network and cannot be
   * misreported by a registry.
   */
  const linkedInstall = (() => {
    try {
      const real = realpathSync(new URL('.', import.meta.url))
      return !/[\\/]node_modules[\\/]/.test(real)
    } catch {
      // If the question cannot be answered, do not offer an install that might
      // clobber a checkout.
      return true
    }
  })()

  /**
   * The newest release, cached between checks.
   *
   * Checking is user-initiated, so this is not about saving work the panel
   * does on its own; it is about what repeated clicking costs. The
   * unauthenticated GitHub API allows 60 requests an hour per address, shared
   * with everything else on it, and a release changes perhaps weekly, so an
   * uncached check would let one impatient minute exhaust the budget and turn
   * every later check into a rate-limit error.
   *
   * A failed check is cached for much less time: the usual cause is a network
   * that is briefly down, and the user is entitled to retry that soon.
   */
  let releaseCache = null

  /** Cache lifetime for a successful check; a release is not urgent news. */
  const RELEASE_CACHE_MS = 15 * 60_000

  /** Cache lifetime for a failed check, so a retry is not pointless. */
  const RELEASE_ERROR_CACHE_MS = 60_000

  /*
   * Floor between two network checks, however insistently the button is
   * pressed. `refresh` exists so a click is never answered from a stale cache
   * — otherwise the control would be a no-op that looks like it worked — but
   * an unconditional bypass makes the button a manual rate-limit burner. A
   * second click inside this window is answered from the cache instead.
   */
  const RELEASE_MIN_INTERVAL_MS = 30_000

  async function latestRelease({ refresh = false } = {}) {
    const age = releaseCache ? Date.now() - releaseCache.at : Infinity
    const ttl = releaseCache?.error ? RELEASE_ERROR_CACHE_MS : RELEASE_CACHE_MS
    const fresh = releaseCache && (refresh ? age < RELEASE_MIN_INTERVAL_MS : age < ttl)
    if (fresh) return releaseCache.value

    let value
    try {
      const response = await fetch(`${RELEASE_API}?per_page=20`, {
        headers: {
          Accept: 'application/vnd.github+json',
          'X-GitHub-Api-Version': '2022-11-28',
          'User-Agent': `dsh-fl-upgrade-workflow/${installedVersion ?? '0'}`,
        },
        signal: AbortSignal.timeout(RELEASE_TIMEOUT_MS),
      })
      if (!response.ok) {
        // A rate-limited check is the common case on a shared IP and reads very
        // differently from "no release", so it is reported as its own state.
        const limited = response.status === 403 && response.headers.get('x-ratelimit-remaining') === '0'
        value = { error: limited ? 'rate-limited' : `github-${response.status}` }
      } else {
        const release = selectRelease(await response.json())
        value = release ? { release } : { error: 'no-release' }
      }
    } catch (failure) {
      value = { error: failure?.name === 'TimeoutError' ? 'timeout' : 'network' }
    }
    releaseCache = { at: Date.now(), value, error: Boolean(value.error) }
    return value
  }

  /**
   * Describe the update situation for the panel.
   *
   * `installable` is the question the button cares about and is deliberately
   * separate from "a newer version exists": this profile may carry the plugin
   * as a linked checkout, where installing a tarball would silently replace a
   * developer's working directory with a published build. That case reports
   * the newer version and refuses to install it.
   */
  async function updateState(options) {
    const manager = ctx.get('pluginManager')
    const { release, error } = await latestRelease(options)
    const newer = Boolean(release && installedVersion && compareVersions(installedVersion, release.version) < 0)
    return {
      installed: installedVersion,
      checkError: error ?? null,
      latest: release
        ? {
          version: release.version,
          publishedAt: release.publishedAt,
          size: release.size,
          notes: release.notes,
          verifiable: Boolean(release.digest),
        }
        : null,
      newer,
      // Without the service there is no way to install anything, so the panel
      // shows the available version and points at the manual route.
      supported: Boolean(manager),
      linked: linkedInstall,
      installable: Boolean(manager) && newer && !linkedInstall,
    }
  }

  /** Serialises updates: two concurrent installs of one bundle cannot be safe. */
  let updating = null

  /**
   * Download the newest release and hand the tarball to the plugin manager.
   *
   * The tarball is fetched here rather than passed to the manager as a URL,
   * because the manager installs from a registry spec or a local path and
   * this release lives on neither. Verification happens before the file is
   * ever offered as an install source:
   *   - the URL host must be a GitHub release host (checked at selection),
   *   - the body must not exceed `MAX_ASSET_BYTES`,
   *   - the sha256 must equal the digest GitHub published, when it published
   *     one. A release whose digest is absent is still installed, since the
   *     transport is authenticated HTTPS to a pinned host, but the response
   *     says which guarantee applied.
   */
  async function runUpdate() {
    const manager = ctx.get('pluginManager')
    if (!manager) return { ok: false, error: 'this Harness build cannot install plugins' }
    if (linkedInstall) {
      return {
        ok: false,
        error: 'this plugin is installed from a local checkout; update it with git instead',
        linked: true,
      }
    }
    const { release, error } = await latestRelease({ refresh: true })
    if (!release) return { ok: false, error: error ?? 'no release found' }
    if (!installedVersion || compareVersions(installedVersion, release.version) >= 0) {
      return { ok: true, changed: false, installed: installedVersion, latest: release.version }
    }

    let response
    try {
      response = await fetch(release.url, {
        headers: { Accept: 'application/octet-stream', 'User-Agent': `dsh-fl-upgrade-workflow/${installedVersion}` },
        signal: AbortSignal.timeout(DOWNLOAD_TIMEOUT_MS),
        redirect: 'follow',
      })
    } catch (failure) {
      return { ok: false, error: failure?.name === 'TimeoutError' ? 'the download timed out' : 'the download failed' }
    }
    if (!response.ok) return { ok: false, error: `the download failed (HTTP ${response.status})` }
    /*
     * Redirects are followed (GitHub sends release downloads to its object
     * host), so the *final* URL is re-checked against the same allowlist. A
     * redirect chain ending anywhere else is refused before the body is used.
     */
    try {
      const landed = new URL(response.url || release.url)
      if (landed.protocol !== 'https:' || !isReleaseDownloadHost(landed.hostname)) {
        return { ok: false, error: 'the download was redirected off GitHub and was refused' }
      }
    } catch {
      return { ok: false, error: 'the download URL could not be verified' }
    }

    const body = Buffer.from(await response.arrayBuffer())
    if (body.byteLength > MAX_ASSET_BYTES) {
      return { ok: false, error: 'the release asset is larger than this plugin will install' }
    }
    if (release.digest) {
      const actual = createHash('sha256').update(body).digest('hex')
      if (actual !== release.digest) {
        return { ok: false, error: 'the download did not match the digest GitHub published' }
      }
    }

    /*
     * The manager installs from a path, so the verified bytes are written to
     * one. The name carries this process's pid so two Harness instances
     * sharing a temp directory cannot write the same file, and the mode keeps
     * it readable only by this user.
     */
    const target = join(tmpdir(), `dsh-fl-upgrade-${release.version}-${process.pid}.tgz`)
    try {
      await writeFile(target, body, { mode: 0o600 })
      const result = await manager.installBundle(target)
      /*
       * The manager reports a saved-but-not-live change rather than throwing,
       * so a missing `applied` is not assumed to mean success: only an
       * explicit `true` is. `nextStep` is the manager's own instruction for
       * finishing the change (a restart, usually) and is passed through for
       * the panel to show instead of being interpreted here.
       */
      const applied = result?.applied === true
      return {
        ok: applied,
        changed: applied,
        from: installedVersion,
        to: release.version,
        verified: Boolean(release.digest),
        nextStep: typeof result?.nextStep === 'string' ? result.nextStep : null,
        error: applied ? undefined : (typeof result?.nextStep === 'string'
          ? result.nextStep
          : 'the install did not complete'),
      }
    } catch (failure) {
      return { ok: false, error: redactText(failure?.message, 300) ?? 'the install failed' }
    } finally {
      // Best effort: a leftover tarball in temp is harmless, and failing to
      // remove it must not turn a successful install into an error.
      await rm(target, { force: true }).catch(() => {})
    }
  }

  function snapshot() {
    const launch = resolveLaunch(config)
    return {
      port: config.port,
      /*
       * Whether a usable checkout is present, never where it is.
       *
       * `config.repoPath` is always set now (an explicit path or the default
       * clone target), so the useful boolean is whether a checkout actually
       * exists on disk — that is what decides the setup prompt. The path, and
       * the account name inside it, never leave the Host.
       */
      repoConfigured: Boolean(config.repoPath) && existsSync(config.repoPath),
      // Whether the user chose the path, versus the default clone location.
      repoPathExplicit: config.repoPathExplicit,
      // Clone progress, so the panel can show "fetching the dashboard…" on a
      // first run instead of a bare "not configured".
      clone: cloneSnapshot(),
      configured: launch.configured === true,
      configReason: launch.configured ? null : (launch.reason ?? 'unknown'),
      launchSource: launch.source ?? null,
      // An adopted dashboard is reachable but not owned, so the panel offers
      // no stop control for it.
      adopted: adopted && current === null,
      running: current !== null || (adopted && current === null),
      managed: current !== null,
      startedAt: current?.startedAt ?? null,
      exit: current ? null : lastError,
      output: current ? readOutput(current.handle) : (lastError?.output ?? null),
      // Whether collection is producing data, as distinct from `running`.
      collection: collectionSnapshot(),
    }
  }

  /** Refresh the adoption flag; called before every state report. */
  async function syncAdoption() {
    adopted = current === null ? await probeUpstream() : false
    return adopted
  }

  /*
   * Per-collector health, keyed by the names in COLLECTOR_PATHS.
   *
   * A live process is not the same as working collection: the server answers
   * while an SSH fan-out fails on every GPU host, or while an expired token
   * makes every PR read return an error body. The header indicator reports
   * the difference, so each collector's last outcome is recorded as reads
   * pass through.
   *
   * Observed rather than subscribed: the dashboard publishes no collection
   * event, so the only evidence available is the reads the panel already
   * makes. A collector nobody has read yet stays absent instead of being
   * reported healthy.
   */
  const collectors = new Map()

  /**
   * Record one collector read outcome.
   *
   * `error` is already-redacted text or null. Success keeps its timestamp
   * separate from the attempt so the indicator can say "working, last data 2
   * minutes ago" during a transient failure rather than flipping to broken on
   * one bad round.
   */
  function noteCollector(path, outcome) {
    const name = COLLECTOR_PATHS.get(path)
    if (!name) return
    const previous = collectors.get(name)
    const at = Date.now()
    if (outcome.ok) {
      collectors.set(name, { okAt: at, triedAt: at, error: null })
      return
    }
    collectors.set(name, {
      okAt: previous?.okAt ?? null,
      triedAt: at,
      error: outcome.error ?? 'unknown',
    })
  }

  /**
   * The collection half of the state report.
   *
   * Timestamps are epoch milliseconds: the panel formats them against the
   * reader's clock, and sending preformatted strings would bake this
   * process's locale into the payload.
   */
  function collectionSnapshot() {
    const rows = {}
    for (const [name, value] of collectors) {
      rows[name] = { okAt: value.okAt, triedAt: value.triedAt, error: value.error }
    }
    return rows
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

    const githubEnv = await resolveGithubToken()

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
        /*
         * The token is passed in the environment, which is the only channel
         * the dashboard reads it from. Spread last so an inherited value is
         * replaced rather than shadowed, and omitted entirely when no token is
         * held — writing an empty variable would mask a `gh` login that
         * upstream prefers anyway.
         */
        ...(githubEnv ? { env: { ...process.env, ...githubEnv } } : {}),
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
    // The directory's own name is enough to recognise the checkout; the path
    // to it is not the browser's business.
    checks.push({ id: 'repo', ok: repoOk, detail: repoOk ? basename(config.repoPath) : null })
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
   * Whether setup still needs the user, and what for.
   *
   * The panel hides the setup entry once nothing is outstanding, so "nothing
   * is outstanding" has to mean more than "the dashboard runs". A deployment
   * with no agent credential runs fine and cannot start a task, which is a
   * worse place to be stranded than an unconfigured one, since the panel looks
   * finished. Deciding it here keeps the rule in one place.
   *
   * Only a missing *collectable* secret counts. Codex authenticates through
   * its own CLI and GitHub is read from the environment, so neither can be
   * finished from this panel and neither keeps the entry alive.
   */
  async function setupState(projectId) {
    const preflight = deployPreflight()
    const outstanding = preflight.checks.filter(check => !check.ok).map(check => check.id)
    if (preflight.ready) {
      try {
        const credentials = await credentialState(projectId || preflight.projects[0].id)
        const claude = credentials?.claude
        // An unavailable interpreter is not a missing secret: typing a key
        // does not install a package, so it must not block completion.
        if (claude && claude.available && !claude.configured) outstanding.push('claude-key')
        const jump = credentials?.jumpserver
        if (credentials?.machineSource === 'jumpserver' && jump?.enabled && !jump.authenticated) {
          outstanding.push('jumpserver-password')
        }
      } catch {
        // Credential readiness needs the dashboard. If it cannot be asked, the
        // deployment checks alone decide, rather than reporting a false need.
      }
    }
    return { complete: outstanding.length === 0, outstanding }
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

  /**
   * Where the ids of tasks this plugin created are remembered.
   *
   * Upstream records nothing about which client created a task: there is no
   * `created_by`, no client id, and unknown request fields are silently
   * dropped, so a tag cannot be smuggled in. The only durable handle is the id
   * the create call returns, so it is kept here and the panel filters on it.
   *
   * Kept under the dashboard checkout's `runs/` directory, which the repo
   * already git-ignores, so the record never becomes a tracked file. It holds
   * ids and nothing else: no paths, no credentials, no machine identity.
   */
  const createdStorePath = config.repoPath
    ? join(config.repoPath, 'runs', 'dsh-plugin-created-tasks.json')
    : null

  /** Ids this plugin created, per project. Shape: `{ [projectId]: string[] }`. */
  const readCreated = () => {
    if (!createdStorePath) return {}
    try {
      const parsed = JSON.parse(readFileSync(createdStorePath, 'utf8'))
      return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {}
    } catch {
      // No file yet, or an unreadable one: an empty record is the safe answer,
      // because this only ever narrows what the panel shows.
      return {}
    }
  }

  const rememberCreated = async (projectId, taskId) => {
    if (!createdStorePath) return
    const all = readCreated()
    const ids = Array.isArray(all[projectId]) ? all[projectId] : []
    if (ids.includes(taskId)) return
    ids.push(taskId)
    all[projectId] = ids
    await writeFile(createdStorePath, `${JSON.stringify(all, null, 2)}\n`, 'utf8')
  }

  /**
   * Which credentials the dashboard considers configured.
   *
   * Booleans and provider names only. Every source route carries values this
   * must not forward: `/api/jumpserver` returns a base URL, a username, an
   * identity-file path and the whole asset inventory, and the agent routes
   * name a third-party base URL. The panel needs to know what is missing, not
   * what the values are.
   *
   * A GitHub token is deliberately absent: no upstream route reports one,
   * because it is read from the dashboard's own process environment and never
   * stored. Claiming to detect it would be a lie, so the panel explains the
   * `gh auth login` path instead.
   */
  /*
   * The GitHub token, read from the Harness credential store at spawn time.
   *
   * This plugin never holds the secret: `credentials.resolve` is called once,
   * the value goes straight into the child's environment, and nothing here
   * stores, logs or returns it. The token is not sent to the dashboard over
   * HTTP either — that server writes every request line to a plaintext log, so
   * the environment is the only safe channel, and it happens to be the one
   * upstream already reads.
   *
   * Returns the environment entries to add, or null when no token is held.
   */
  async function resolveGithubToken() {
    const name = config.githubTokenEnv
    if (!name) return null
    const credentials = ctx.get('credentials')
    if (!credentials) return null
    try {
      const resolved = await credentials.resolve(name)
      const value = typeof resolved?.value === 'string' ? resolved.value.trim() : ''
      if (!value) return null
      /*
       * Both names are set. Upstream reads GITHUB_TOKEN first and falls back
       * to GH_TOKEN, while the `gh` CLI it shells out to prefers GH_TOKEN, so
       * setting only one leaves the other path unauthenticated.
       */
      return name === 'GITHUB_TOKEN'
        ? { GITHUB_TOKEN: value, GH_TOKEN: value }
        : { [name]: value, GITHUB_TOKEN: value, GH_TOKEN: value }
    } catch {
      // A credential store that cannot answer must not stop the dashboard:
      // unauthenticated GitHub reads still work, just at a lower rate limit.
      return null
    }
  }

  /**
   * Whether a GitHub token is available to hand to the dashboard.
   *
   * Reports presence and where it came from, never the value. Used by the
   * credential view, which previously could only state the rule because the
   * plugin had no way to know.
   */
  async function githubTokenState() {
    const name = config.githubTokenEnv
    if (!name) return { supported: false, configured: false, writable: false, source: null }
    const credentials = ctx.get('credentials')
    if (!credentials) return { supported: false, configured: false, writable: false, source: null }
    try {
      const info = await credentials.describe(name)
      return {
        supported: true,
        env: name,
        configured: info?.configured === true,
        // Reported for display, not as a gate. Per the service contract `set`
        // always writes its own managed source, so a token can always be saved
        // here; gating the field on this flag was what stranded users whose
        // token came from the environment.
        writable: info?.writable !== false,
        // Where the *currently effective* value comes from: `file` means our
        // managed store, anything else (env, a `.env` fallback) means ambient.
        // A saved value overrides ambient, which the panel explains.
        source: typeof info?.source === 'string' ? info.source : null,
        ambient: info?.configured === true && typeof info?.source === 'string' && info.source !== 'file',
      }
    } catch {
      return { supported: false, configured: false, writable: false, source: null }
    }
  }

  /*
   * Whether the GitHub CLI is signed in on this machine.
   *
   * The dashboard reads GitHub through `gh api` first and falls back to the
   * token only when that fails, so a signed-in `gh` is a complete way to
   * authenticate collection on its own — a stored token is then optional. The
   * panel used to look only at the credential store and reported "no token,
   * 60 per hour" to users whose `gh` was already giving them 5000.
   *
   * `gh auth status` exits 0 when a usable credential exists and non-zero
   * otherwise (including a revoked token). Only that boolean is kept: its
   * output names the account, which never leaves the Host. Cached briefly,
   * because the credential view polls and each check is a subprocess that may
   * make a network round trip.
   */
  const GH_STATUS_TTL_MS = 60_000
  const GH_STATUS_TIMEOUT_MS = 10_000
  let ghStatusCache = null
  let ghStatusFlight = null

  async function probeGhCli() {
    let gh
    try {
      gh = await ctx.subprocess.resolveExecutable('gh')
    } catch {
      return { installed: false, authenticated: false }
    }
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), GH_STATUS_TIMEOUT_MS)
    try {
      const handle = ctx.subprocess.spawn({
        argv: [gh, 'auth', 'status', '--hostname', 'github.com'],
        // The subprocess service needs a working directory; gh does not care
        // which, and the temp directory always exists.
        cwd: tmpdir(),
        stdio: {
          stdin: 'ignore',
          // Captured only so the pipe drains; never read or forwarded.
          stdout: { maxBytes: 4096 },
          stderr: { maxBytes: 4096 },
        },
        graceMs: 2_000,
        signal: controller.signal,
      })
      const outcome = await handle.done
      return { installed: true, authenticated: outcome.exitCode === 0 }
    } catch {
      // A timeout or spawn failure says nothing either way.
      return { installed: true, authenticated: null }
    } finally {
      clearTimeout(timer)
    }
  }

  async function ghCliState() {
    if (ghStatusCache && Date.now() - ghStatusCache.at < GH_STATUS_TTL_MS) return ghStatusCache.value
    if (!ghStatusFlight) {
      ghStatusFlight = probeGhCli()
        .then(value => {
          ghStatusCache = { at: Date.now(), value }
          return value
        })
        .finally(() => { ghStatusFlight = null })
    }
    return ghStatusFlight
  }

  /** The token state plus the gh sign-in, for the credential view. */
  async function githubState() {
    const [token, cli] = await Promise.all([githubTokenState(), ghCliState()])
    return { ...token, cli }
  }

  const credentialState = async projectId => {
    const query = `?project=${encodeURIComponent(projectId)}`
    const read = async path => {
      try {
        const reply = await callUpstream(config.port, 'GET', `${path}${query}`)
        if (reply.status >= 400) return null
        return JSON.parse(reply.text)
      } catch {
        // A credential view must not fail just because one probe did.
        return null
      }
    }
    const [providers, jump, caps, github] = await Promise.all([
      read('/api/agent-providers'),
      read('/api/jumpserver'),
      read('/api/capabilities'),
      githubState(),
    ])
    const agent = name => {
      const row = providers?.providers?.[name]
      if (!row) return null
      return {
        available: row.available === true,
        configured: row.configured === true,
        // `authenticated` is null for a key-based provider, which is not the
        // same as "not authenticated".
        authenticated: row.authenticated === true,
        // A provider can be configured and still unusable, so carry the
        // reason — redacted, since it can name an interpreter path.
        error: redactText(row.error, 200),
      }
    }
    return {
      codex: agent('codex'),
      claude: agent('claude'),
      flagscale: agent('flagscale'),
      // Which machine source is active decides whether JumpServer credentials
      // matter at all.
      machineSource: scalar(caps?.machine_source) || null,
      // Presence and origin only; the token itself never leaves the Host.
      github,
      jumpserver: jump
        ? {
          enabled: jump.enabled === true,
          authenticated: jump.authenticated === true,
          passwordSaved: jump.password_saved === true,
          canRemember: jump.can_remember_password === true,
          assetCount: Number.isFinite(jump.asset_count) ? jump.asset_count : 0,
          error: redactText(jump.error, 200),
        }
        : null,
    }
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

        /*
         * Credential readiness for the deploy view. A GET with no secret in
         * it, by design: the upstream server logs request lines to a plaintext
         * file, so this route only ever carries a project id.
         */
        if (req.method === 'GET' && action === 'credentials') {
          const projectId = scalar(url.searchParams.get('project'))
          if (!projectId) {
            sendJson(res, 400, { error: 'project is required' })
            return
          }
          sendJson(res, 200, await credentialState(projectId))
          return
        }
        /*
         * What a new task could be created with, and what currently blocks it.
         *
         * Every guard is resolved here from upstream rather than in the
         * browser, so a stale panel cannot offer an option the dashboard will
         * refuse. The reply names machines by id and label only: the source
         * route also carries SSH aliases, connection details and identity-file
         * paths, none of which the panel needs.
         */
        if (req.method === 'GET' && action === 'task-options') {
          const projectId = scalar(url.searchParams.get('project'))
          if (!projectId) {
            sendJson(res, 400, { error: 'project is required' })
            return
          }
          if (!current && !(await syncAdoption())) {
            sendJson(res, 503, { error: 'dashboard is not running' })
            return
          }
          let cards
          try {
            const reply = await callUpstream(
              config.port,
              'GET',
              `/api/codex-sessions?view=cards&project=${encodeURIComponent(projectId)}`,
              undefined,
              SLOW_UPSTREAM_TIMEOUT_MS,
            )
            if (reply.status >= 400) {
              sendJson(res, 502, { error: 'cannot read the machine list' })
              return
            }
            cards = JSON.parse(reply.text)
          } catch (failure) {
            sendJson(res, 502, { error: redactText(failure.message, 300) ?? 'cannot read the machine list' })
            return
          }
          let tasks = []
          try {
            tasks = await listTasks(projectId)
          } catch {
            // The pair guard degrades to "unknown" rather than blocking the
            // whole view; upstream enforces it again on create anyway.
          }
          /*
           * The branch list, so the panel can offer a dropdown instead of a
           * free-text field where a typo silently targets a non-existent
           * branch. It lives on branch-status, not on the sessions cards, so
           * it is read separately and degrades to just the default branch when
           * that read fails — a dropdown of one is still better than a box.
           */
          let branches = []
          try {
            const reply = await callUpstream(config.port, 'GET', `/api/branch-status?project=${encodeURIComponent(projectId)}`)
            if (reply.status < 400) {
              const parsed = JSON.parse(reply.text)
              if (Array.isArray(parsed?.branches)) {
                branches = parsed.branches.filter(b => typeof b === 'string')
              }
            }
          } catch {
            // Leave it empty; the client falls back to the default branch.
          }
          // A (backend, branch) pair that already has a live task cannot take
          // another one. Soft-deleted tasks free their pair, and they are
          // already absent from this list.
          const taken = new Set()
          for (const task of tasks) {
            if (task?.backend && task?.branch) taken.add(`${task.backend}\u0000${task.branch}`)
          }
          const policies = {}
          for (const [backend, policy] of Object.entries(cards?.backend_task_policies ?? {})) {
            if (!policy || typeof policy !== 'object') continue
            policies[backend] = {
              mode: scalar(policy.mode) || null,
              // A policy only bites when its branch matches the requested one.
              branch: scalar(policy.branch) || null,
              reason: redactText(policy.reason, 300),
              pr: typeof policy.pr === 'number' ? policy.pr : null,
              pr_url: typeof policy.pr_url === 'string' ? policy.pr_url : null,
            }
          }
          const machines = (Array.isArray(cards?.machine_profiles) ? cards.machine_profiles : [])
            .filter(row => row && typeof row.id === 'string')
            .map(row => ({
              id: row.id,
              // A label is operator-written and can name a host, so redact it.
              label: redactText(row.label, 120) ?? row.id.slice(0, 12),
              backend: scalar(row.backend) || null,
              role: scalar(row.role) || null,
              // Upstream rejects a non-compute machine for an adaptation task.
              usable: (scalar(row.role) || 'compute') === 'compute' && !row.configuration_error,
              // Every profile on this deployment has an empty remote_workspace,
              // which manual creation requires. Report it so the panel can say
              // why, instead of letting the user hit a 400.
              hasWorkspace: typeof row.remote_workspace === 'string' && row.remote_workspace.trim() !== '',
              // Name-inferred backends are not hardware-verified; automatic
              // mode re-detects over SSH and may disagree.
              backendVerified: !/推断|infer/i.test(String(row.backend_source ?? '')),
              configError: redactText(row.configuration_error, 200),
            }))
          sendJson(res, 200, {
            // `available` false means sessions are not configured at all.
            available: cards?.available === true,
            defaultBranch: scalar(cards?.default_branch) || null,
            // Every branch the dashboard knows, for the new-task dropdown. The
            // default is guaranteed present even when the list read failed.
            branches: (() => {
              const seen = branches.slice()
              const def = scalar(cards?.default_branch)
              if (def && !seen.includes(def)) seen.unshift(def)
              return seen
            })(),
            backends: Array.isArray(cards?.backends) ? cards.backends.filter(b => typeof b === 'string') : [],
            machines,
            policies,
            takenPairs: [...taken].map(key => {
              const [backend, branch] = key.split('\u0000')
              return { backend, branch }
            }),
            providers: Object.entries(cards?.providers ?? {}).reduce((out, [name, row]) => {
              out[name] = {
                available: row?.available === true,
                error: redactText(row?.error, 200),
              }
              return out
            }, {}),
            // No profile having a workspace means manual creation cannot work
            // on this deployment at all, which the panel must say plainly.
            anyManualPossible: machines.some(m => m.usable && m.hasWorkspace),
          })
          return
        }
        if (req.method === 'GET' && action === 'deploy-preflight') {
          sendJson(res, 200, deployPreflight())
          return
        }

        /*
         * Whether a newer plugin release exists.
         *
         * Read-only and cached, so the panel can ask on every mount. `refresh`
         * forces a new check for the explicit "check now" action.
         */
        if (req.method === 'GET' && action === 'update-state') {
          sendJson(res, 200, await updateState({ refresh: scalar(url.searchParams.get('refresh')) === '1' }))
          return
        }

        /*
         * Install the newest release over this plugin.
         *
         * POST because it replaces running code. It takes no parameters at
         * all: the release source is pinned in this module, so there is no
         * version, URL or path a caller could supply to redirect the install.
         */
        if (req.method === 'POST' && action === 'self-update') {
          // One at a time. A second press joins the running install rather
          // than starting a competing one against the same bundle.
          if (!updating) {
            updating = runUpdate().finally(() => { updating = null })
          }
          const outcome = await updating
          sendJson(res, outcome.ok ? 200 : 409, outcome)
          return
        }

        /*
         * Whether the panel should still offer its setup entry.
         *
         * Separate from `deploy-preflight`, which the setup view polls while
         * the user works in it. This one is asked once per panel mount to
         * decide whether that view is reachable at all, so it stays cheap and
         * carries no project inputs.
         */
        if (req.method === 'GET' && action === 'setup-state') {
          sendJson(res, 200, await setupState(scalar(url.searchParams.get('project'))))
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
          let upstream
          try {
            upstream = await callUpstream(
              config.port,
              'GET',
              `${path}${suffix}`,
              undefined,
              SLOW_READ_PATHS.has(path) ? SLOW_UPSTREAM_TIMEOUT_MS : UPSTREAM_TIMEOUT_MS,
            )
          } catch (failure) {
            // A transport failure is a collector failure too: the indicator
            // would otherwise keep reporting the last success while every
            // read times out.
            noteCollector(path, { ok: false, error: redactText(String(failure?.message ?? failure), 200) })
            throw failure
          }
          /*
           * Attribute this read to its collector.
           *
           * An HTTP 200 carrying `{error}` is the dashboard's way of saying a
           * collection round failed while the server itself is fine, so the
           * body decides the outcome rather than the status code.
           */
          if (COLLECTOR_PATHS.has(path)) {
            let failed = upstream.status >= 400 ? `upstream status ${upstream.status}` : null
            if (!failed) {
              try {
                const parsed = JSON.parse(upstream.text)
                if (parsed && parsed.error) failed = redactText(String(parsed.error), 200)
              } catch {
                // Unparseable but 2xx: treat as delivered, same as the
                // forwarding below, which passes it through unchanged.
              }
            }
            noteCollector(path, failed ? { ok: false, error: failed } : { ok: true })
          }
          // The panel renders the summary fields only, so compact the payload
          // here rather than shipping the full per-case matrix to the browser.
          let text = upstream.text
          // An upstream error body is a plain `{error}`; summarizing it would
          // strip that field and leave the panel with a shapeless object.
          if (upstream.status < 400 && url.searchParams.get('full') !== 'true') {
            try {
              const summary = summarizeUpstream(path, JSON.parse(upstream.text))
              // Mark which tasks this plugin created. Upstream records no
              // creator at all, so this local record is the only source, and
              // the panel uses it to default to its own tasks.
              if (path === '/api/adaptation-tasks' && Array.isArray(summary?.tasks)) {
                const mine = new Set(readCreated()[project] ?? [])
                summary.created_here = mine.size
                for (const task of summary.tasks) {
                  task.created_here = mine.has(task.id)
                }
              }
              text = JSON.stringify(summary)
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
         * What the sidebar currently holds for one worktree path.
         *
         * `resolveByPath` canonicalizes through `realpath` before it looks
         * anything up, so a path that no longer exists REJECTS rather than
         * returning undefined. The dashboard prunes a worktree when its task
         * finishes, which means a registration made earlier becomes
         * unresolvable by path even though it is still in the sidebar. Asking
         * the registry for its own list avoids the filesystem entirely, and
         * each entity reports `missing-dir` itself, so a stale row can be
         * labelled instead of silently reading as "never adopted".
         */
        const describeWorkspace = async path => {
          const registry = ctx.get('workspaceRegistry')
          let canonical = null
          try {
            canonical = await realpath(path)
          } catch {
            // The worktree is not on disk: either not created yet, or pruned.
            // Which one it is depends on whether a registration survives.
          }
          for (const workspace of registry.list()) {
            // `list()` paths are already canonical, so compare case-insensitively
            // on Windows and exactly elsewhere.
            const same = canonical
              ? comparePaths(workspace.path, canonical)
              : comparePaths(workspace.path, path)
            if (!same) continue
            const status = await workspace.status()
            return {
              exists: true,
              // An opaque uuid, which the Client needs to open the workspace
              // and start a conversation in it. Its service cannot enumerate
              // workspaces, so this is the only way it can learn the id.
              workspace: workspace.id,
              title: workspace.title,
              // A registration whose directory is gone is worse than no
              // registration: it sits in the sidebar and opens nothing.
              stale: status === 'missing-dir',
              missing: false,
            }
          }
          return { exists: false, workspace: null, title: null, stale: false, missing: canonical === null }
        }

        /*
         * Which project repositories exist on disk, and whether each is
         * already in the sidebar.
         *
         * The dashboard's own branch-monitor config names each project's
         * checkout, usually relative to the dashboard repo, so the path can be
         * derived rather than asked for. The browser receives booleans and a
         * display name only: the resolved path stays here.
         */
        if (req.method === 'GET' && action === 'repos') {
          if (!ctx.get('workspaceRegistry')) {
            sendJson(res, 200, { supported: false, repos: [] })
            return
          }
          const repos = []
          for (const project of readProjectsFromDisk(config).projects) {
            const resolved = resolveProjectRepo(config, project.id)
            if (!resolved) {
              repos.push({
                project: project.id,
                label: project.label,
                // Without a config entry there is nothing to detect, and the
                // panel must not offer to clone a repo it cannot name.
                known: false,
                present: false,
                adopted: false,
                name: null,
                remote: null,
              })
              continue
            }
            const described = await describeWorkspace(resolved.path)
            repos.push({
              project: project.id,
              label: project.label,
              known: true,
              present: existsSync(resolved.path),
              adopted: described.exists,
              workspace: described.workspace,
              stale: described.stale,
              // The directory's own name, not its path: enough to recognise
              // the checkout without disclosing where it sits.
              name: basename(resolved.path),
              branch: resolved.branch,
              // A clone needs a source, and the configured GitHub repo is
              // already public information in the dashboard's config.
              remote: resolved.githubRepo,
            })
          }
          sendJson(res, 200, { supported: true, repos })
          return
        }

        /*
         * Put one project's repository in the sidebar.
         *
         * Registration only: the directory must already exist. Cloning is a
         * separate, separately-confirmed action, because it writes to disk and
         * reaches the network.
         */
        if (req.method === 'POST' && action === 'adopt-repo') {
          const registry = ctx.get('workspaceRegistry')
          if (!registry) {
            sendJson(res, 501, { error: 'this Harness build has no workspace sidebar' })
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
          if (!projectId) {
            sendJson(res, 400, { error: 'project is required' })
            return
          }
          const resolved = resolveProjectRepo(config, projectId)
          if (!resolved) {
            sendJson(res, 404, { error: 'this project has no repository configured' })
            return
          }
          if (!existsSync(resolved.path)) {
            sendJson(res, 409, { error: 'this repository is not on disk yet', present: false })
            return
          }
          try {
            // `create` is idempotent per canonical path, so adopting twice
            // returns the same workspace instead of adding a duplicate.
            const workspace = await registry.create(resolved.path, resolved.title)
            await retitleBranchNamed(workspace, resolved)
            // The id is what the Client needs to open the workspace and start
            // a conversation in it. It is an opaque uuid, not a path, and the
            // Client service offers no way to enumerate workspaces, so the
            // Host has to hand it over explicitly.
            sendJson(res, 200, {
              ok: true,
              workspace: workspace.id,
              title: workspace.title,
              name: basename(resolved.path),
            })
          } catch (failure) {
            sendJson(res, 400, {
              ok: false,
              error: redactText(failure.message, 300) ?? 'this directory could not be added',
            })
          }
          return
        }

        /*
         * Clone one monitored project's repository into place.
         *
         * The panel already detects whether the checkout exists and shows the
         * clone command to copy. This runs that same clone on the user's click,
         * because it was the step people missed.
         *
         * Both endpoints come from the dashboard's own config, never from the
         * request: the destination is the `repo` path the branch-monitor config
         * already names (resolved against that config), and the source is the
         * configured `github_repo`. So there is nothing a caller can redirect —
         * no URL, no path, no ref. The request names only a project id.
         *
         * Refused when the destination already exists: this never overwrites a
         * checkout, and a half-finished directory is the user's to resolve.
         */
        if (req.method === 'POST' && action === 'clone-repo') {
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
          if (!projectId) {
            sendJson(res, 400, { error: 'project is required' })
            return
          }
          const resolved = resolveProjectRepo(config, projectId)
          if (!resolved) {
            sendJson(res, 404, { error: 'this project has no repository configured' })
            return
          }
          if (existsSync(resolved.path)) {
            // Already there is a success for the caller's purpose, not an error:
            // the panel's next read will show it as present.
            sendJson(res, 200, { ok: true, alreadyPresent: true })
            return
          }
          // Without a configured GitHub repo there is nothing to clone from,
          // and guessing one would be inventing a source.
          if (!resolved.githubRepo || !/^[\w.-]+\/[\w.-]+$/.test(resolved.githubRepo)) {
            sendJson(res, 409, { error: 'this project has no GitHub repository configured to clone from' })
            return
          }

          let git
          try {
            git = await ctx.subprocess.resolveExecutable('git')
          } catch {
            sendJson(res, 409, { error: 'git was not found on PATH' })
            return
          }

          // HTTPS, public read: no credential is involved or stored.
          const url = `https://github.com/${resolved.githubRepo}.git`
          const controller = new AbortController()
          const timer = setTimeout(() => controller.abort(), CLONE_TIMEOUT_MS)
          let failureText = null
          try {
            const handle = ctx.subprocess.spawn({
              argv: [git, 'clone', url, resolved.path],
              // The parent of the destination, which exists: the target itself
              // must not be created by the clone's cwd.
              cwd: dirname(resolved.path),
              stdio: {
                stdin: 'ignore',
                stdout: { maxBytes: OUTPUT_LIMIT },
                stderr: { maxBytes: OUTPUT_LIMIT },
              },
              graceMs: 5_000,
              signal: controller.signal,
            })
            const outcome = await handle.done
            if (outcome.exitCode !== 0) failureText = redactText(readOutput(handle), 300)
          } catch (failure) {
            failureText = controller.signal.aborted
              ? 'the clone timed out'
              : (redactText(failure?.message, 300) ?? 'the clone failed')
          } finally {
            clearTimeout(timer)
          }

          if (existsSync(resolved.path) && !failureText) {
            // Only the directory name goes back, never the path.
            sendJson(res, 200, { ok: true, name: basename(resolved.path) })
            return
          }
          sendJson(res, 502, {
            ok: false,
            error: failureText ?? 'the clone did not produce a checkout',
          })
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
            rows.push({ task: task.id, ...await describeWorkspace(path) })
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
              /*
               * A task worktree keeps the branch in its title, unlike a
               * project checkout.
               *
               * The difference is real, not an inconsistency. A project
               * checkout is a long-lived directory whose branch changes with
               * any `git checkout`, so a branch in its name goes stale. A task
               * worktree is created by the dashboard for one branch on one
               * backend and is pruned when that task ends — the branch is part
               * of what the directory permanently is, so naming it is what
               * distinguishes two sibling worktrees in the sidebar.
               */
              const workspace = await ctx.get('workspaceRegistry').create(path, taskLabel(task))
              sendJson(res, 200, { ok: true, title: workspace.title })
            } catch (failure) {
              // `create` rejects a path that is missing or not a directory,
              // which for a task worktree means the dashboard has not created
              // it yet or has already pruned it. Say which, because the two
              // need opposite actions from the user.
              const existing = await describeWorkspace(path)
              sendJson(res, 409, {
                ok: false,
                stale: existing.stale,
                error: existing.stale
                  ? 'the worktree this task was registered under no longer exists'
                  : redactText(failure.message, 400) ?? 'the worktree could not be registered',
              })
            }
            return
          }
          /*
           * Drop a sidebar registration whose directory is gone.
           *
           * Deliberately limited to that case: the route refuses while the
           * directory still exists, so it cannot be used to unregister a
           * workspace the user is working in. Deleting a registration keeps
           * the directory and every session log, so nothing is destroyed.
           */
          if (action === 'forget-workspace') {
            const registry = ctx.get('workspaceRegistry')
            if (!registry) {
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
            const path = typeof task.worktree === 'string' ? task.worktree : ''
            if (!path) {
              sendJson(res, 409, { error: 'this task has no local worktree yet' })
              return
            }
            let target
            for (const workspace of registry.list()) {
              if (comparePaths(workspace.path, path)) { target = workspace; break }
            }
            if (!target) {
              sendJson(res, 404, { error: 'this task has no sidebar entry' })
              return
            }
            if (await target.status() !== 'missing-dir') {
              sendJson(res, 409, { error: 'this workspace still exists; remove it from the sidebar instead' })
              return
            }
            await registry.delete(target.id)
            sendJson(res, 200, { ok: true })
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
           * Hand one credential to the dashboard's own encrypted store.
           *
           * This route never persists anything itself and never returns a
           * secret. The dashboard already owns a correct sink for each type it
           * accepts: both are POST bodies, sealed with Windows DPAPI under the
           * current user, written into a git-ignored directory, and read back
           * only as a boolean. Reimplementing any of that here would be
           * strictly worse, so this is a thin forwarder.
           *
           * Deliberately NOT accepted:
           *  - a GitHub token, because the dashboard has no store for one. It
           *    reads GITHUB_TOKEN/GH_TOKEN from its own environment and prefers
           *    an authenticated `gh`, so a field here would force this plugin
           *    to invent persistence for a secret. The panel detects and
           *    instructs instead.
           *  - an SSH private key or passphrase. That design is
           *    reference-only: config and key files are named by path and their
           *    bytes are never read. Accepting key material would break the
           *    invariant the dashboard maintains.
           *
           * The secret travels in the request body for a concrete reason: the
           * upstream control server logs every request LINE to a plaintext
           * file, so anything in a query string or path segment would be
           * written to disk unencrypted.
           */
          if (action === 'credential') {
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
            const kind = scalar(parsed?.kind)
            const secret = typeof parsed?.secret === 'string' ? parsed.secret : ''
            if (kind !== 'claude' && kind !== 'jumpserver' && kind !== 'github') {
              sendJson(res, 400, { error: 'kind must be claude, jumpserver or github' })
              return
            }
            if (!secret) {
              sendJson(res, 400, { error: 'secret must be a non-empty string' })
              return
            }
            // Upstream caps both at 4096 and rejects control characters.
            if (secret.length > 4096 || /[\u0000-\u001f]/.test(secret)) {
              sendJson(res, 400, { error: 'this value is not a valid credential' })
              return
            }

            /*
             * The GitHub token goes to the Harness credential store, not to the
             * dashboard.
             *
             * Upstream has no route that accepts it — it reads the token from
             * its own process environment — and its control server logs every
             * request line in plaintext, so forwarding the token over HTTP
             * would write it to disk. Storing it here means the next dashboard
             * start inherits it, which is the mechanism upstream already uses.
             */
            if (kind === 'github') {
              const credentials = ctx.get('credentials')
              if (!credentials || !config.githubTokenEnv) {
                sendJson(res, 501, { ok: false, error: 'this Harness build cannot store a GitHub token' })
                return
              }
              // Required like every other credential write: the reply carries
              // this project's readiness, and defaulting the id would report a
              // project the caller never named.
              const githubProject = scalar(parsed?.project)
              if (!githubProject) {
                sendJson(res, 400, { error: 'project is required' })
                return
              }
              /*
               * No refusal for an ambient token. The credential service's
               * `set` always writes its own managed source, and a stored value
               * takes precedence over ambient fallbacks (process env, `.env`).
               * So a token already present in the environment is not a reason
               * to reject the write — the saved one simply wins. Refusing it
               * (as an earlier version did with a 409) was the bug behind "I
               * cannot configure the token": a user whose shell exported
               * GITHUB_TOKEN could never save one here.
               */
              try {
                await credentials.set(config.githubTokenEnv, secret)
              } catch (failure) {
                sendJson(res, 500, {
                  ok: false,
                  error: redactText(failure.message, 300) ?? 'the token could not be stored',
                })
                return
              }
              /*
               * The running dashboard keeps the environment it was started
               * with, so a token stored now reaches collection only after a
               * restart. Saying so is the difference between "saved" and
               * "working".
               */
              sendJson(res, 200, {
                ok: true,
                restartRequired: current !== null || adopted,
                state: await credentialState(githubProject),
              })
              return
            }

            const projectId = scalar(parsed?.project)
            if (!projectId) {
              sendJson(res, 400, { error: 'project is required' })
              return
            }
            const target = kind === 'claude'
              ? { path: '/api/agent-settings', payload: { provider: 'claude', api_key: secret } }
              : {
                path: '/api/jumpserver/login',
                payload: {
                  password: secret,
                  // Persisting the password is the user's explicit choice, and
                  // only Windows can seal it. Default to memory-only.
                  remember_password: parsed?.remember === true,
                },
              }
            try {
              const reply = await callUpstream(
                config.port,
                'POST',
                `${target.path}?project=${encodeURIComponent(projectId)}`,
                JSON.stringify(target.payload),
                SLOW_UPSTREAM_TIMEOUT_MS,
              )
              if (reply.status >= 400) {
                // Upstream error text is written for an operator and may name a
                // host or a key path, so it is redacted before it is shown.
                let message = 'the dashboard rejected this credential'
                try {
                  const body = JSON.parse(reply.text)
                  message = redactText(body?.error, 300) ?? message
                } catch { /* a non-JSON failure body says nothing useful */ }
                sendJson(res, 400, { ok: false, error: message })
                return
              }
              // Confirm from the dashboard's own read route rather than
              // trusting the write. Neither reply contains the secret.
              sendJson(res, 200, { ok: true, state: await credentialState(projectId) })
            } catch (failure) {
              sendJson(res, 502, {
                ok: false,
                error: redactText(failure.message, 300) ?? 'the dashboard could not be reached',
              })
            }
            return
          }
          /*
           * Create an adaptation task. Prepare only; never start.
           *
           * `start` is deliberately never forwarded, under any input. Sending
           * it would make one request both create the task AND launch an agent
           * turn, spending model tokens and running commands on a remote GPU
           * host. Starting stays in the dashboard, where the user sees the full
           * context of what is about to run.
           *
           * Even prepare-only is not free: upstream adds a git worktree and
           * takes a cross-project machine lease, so the panel confirms first.
           *
           * Only `profile_id`, `branch`, `goal` and `provider` are forwarded.
           * The machine's SSH alias, connection details, remote workspace and
           * model paths are all derived upstream from the profile, so the
           * browser never sends or sees machine identity. `ssh_connection` is
           * never forwarded: upstream validates it against the saved profile
           * anyway and its presence forces a live SSH preflight.
           */
          /*
           * Set a machine profile's remote working directory.
           *
           * Manual task creation needs `remote_workspace` on the chosen
           * profile, and on a JumpServer-sourced deployment every profile
           * starts empty — so without this the panel can only ever say "no
           * usable machine". Upstream's `save_profile` merges onto the stored
           * profile and JumpServer refresh preserves a user-set workspace
           * (`previous.get("remote_workspace")`), so this one field, set once,
           * survives.
           *
           * The path is a remote absolute path on the GPU host, which only the
           * user knows; this never invents one. It is validated as a POSIX
           * absolute path and forwarded in the body, never a query string.
           */
          if (action === 'set-workspace') {
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
            const profileId = scalar(parsed?.profile)
            // Not `scalar`: a remote path is absolute and starts with '/', which
            // scalar rejects as a leading-dash hazard. Validate it directly.
            const workspace = typeof parsed?.workspace === 'string' ? parsed.workspace.trim() : ''
            if (!projectId || !profileId) {
              sendJson(res, 400, { error: 'project and profile are required' })
              return
            }
            if (!workspace) {
              sendJson(res, 400, { error: 'workspace is required' })
              return
            }
            // A remote POSIX absolute path: no control characters, no shell
            // metacharacters that a later SSH command line would reinterpret,
            // and it must begin at root so it is unambiguous on the host.
            if (!workspace.startsWith('/') || workspace.length > 400
              || /[\u0000-\u001f]/.test(workspace) || /["'`$\\]/.test(workspace)) {
              sendJson(res, 400, { error: 'workspace must be an absolute remote path without quotes or shell metacharacters' })
              return
            }
            if (!current && !(await syncAdoption())) {
              sendJson(res, 503, { error: 'dashboard is not running' })
              return
            }
            try {
              const reply = await callUpstream(
                config.port,
                'POST',
                `/api/codex-machines?project=${encodeURIComponent(projectId)}`,
                // Only the id and the one field: save_profile merges onto the
                // stored profile, so nothing else is disturbed.
                JSON.stringify({ id: profileId, remote_workspace: workspace }),
                SLOW_UPSTREAM_TIMEOUT_MS,
              )
              if (reply.status >= 400) {
                let message = 'the dashboard rejected this workspace path'
                try {
                  message = redactText(JSON.parse(reply.text)?.error, 300) ?? message
                } catch { /* non-JSON failure body */ }
                sendJson(res, 400, { ok: false, error: message })
                return
              }
              // Confirm from the saved profile rather than trusting the write;
              // report only whether it now carries a workspace, never the path.
              let saved = false
              try {
                saved = typeof JSON.parse(reply.text)?.remote_workspace === 'string'
                  && JSON.parse(reply.text).remote_workspace.trim() !== ''
              } catch { /* keep saved false */ }
              sendJson(res, 200, { ok: true, saved })
            } catch (failure) {
              sendJson(res, 502, {
                ok: false,
                error: redactText(failure.message, 300) ?? 'the dashboard could not be reached',
              })
            }
            return
          }

          if (action === 'create-task') {
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
            const profileId = scalar(parsed?.profile)
            const branch = scalar(parsed?.branch)
            const provider = scalar(parsed?.provider) || 'codex'
            const goal = typeof parsed?.goal === 'string' ? parsed.goal.trim() : ''
            if (!projectId || !profileId) {
              sendJson(res, 400, { error: 'project and profile are required' })
              return
            }
            if (!branch) {
              sendJson(res, 400, { error: 'branch is required' })
              return
            }
            // Upstream rejects a branch with whitespace or a leading dash, and
            // caps the goal at 6000.
            if (branch.length > 200 || /\s/.test(branch) || branch.startsWith('-')) {
              sendJson(res, 400, { error: 'branch is not a valid git ref' })
              return
            }
            if (goal.length > 6000) {
              sendJson(res, 400, { error: 'goal is too long (6000 characters maximum)' })
              return
            }
            if (provider !== 'codex' && provider !== 'claude' && provider !== 'flagscale') {
              // A provider cannot be changed after creation, so only the three
              // the dashboard actually implements are accepted. Whether a given
              // one is available right now is checked next, against upstream.
              sendJson(res, 400, { error: 'provider must be codex, claude or flagscale' })
              return
            }
            if (!current && !(await syncAdoption())) {
              sendJson(res, 503, { error: 'dashboard is not running' })
              return
            }
            /*
             * Refuse a provider upstream reports as unavailable, rather than
             * forwarding a create that stranding the task. The provider cannot
             * be changed afterward, so this is the last chance to catch it.
             */
            try {
              const probe = await callUpstream(config.port, 'GET', `/api/agent-providers?project=${encodeURIComponent(projectId)}`)
              if (probe.status < 400) {
                const row = JSON.parse(probe.text)?.providers?.[provider]
                if (row && row.available !== true) {
                  sendJson(res, 409, { error: `the ${provider} provider is not available on this deployment` })
                  return
                }
              }
            } catch {
              // If the probe cannot run, defer to upstream's own check on create.
            }
            const query = `?project=${encodeURIComponent(projectId)}`
            try {
              const reply = await callUpstream(
                config.port,
                'POST',
                `/api/codex-sessions${query}`,
                // Note the absent `start`: see the comment above.
                JSON.stringify({ profile_id: profileId, branch, provider, goal }),
                SLOW_UPSTREAM_TIMEOUT_MS,
              )
              if (reply.status >= 400) {
                let message = 'the dashboard refused to create this task'
                try {
                  message = redactText(JSON.parse(reply.text)?.error, 400) ?? message
                } catch { /* a non-JSON failure body says nothing useful */ }
                sendJson(res, 400, { ok: false, error: message })
                return
              }
              // The reply echoes the whole session, including the worktree
              // path and SSH alias. Keep the id, and let the projecting task
              // route supply everything the panel displays.
              const created = JSON.parse(reply.text)
              const taskId = scalar(created?.id)
              if (!taskId) {
                sendJson(res, 502, { ok: false, error: 'the dashboard did not return a task id' })
                return
              }
              // Upstream records no creator, so this is the only way the panel
              // can tell its own tasks apart later.
              await rememberCreated(projectId, taskId)
              sendJson(res, 200, { ok: true, task: taskId })
            } catch (failure) {
              // A timed-out create may still have succeeded upstream, and
              // there is no idempotency key, so never retry automatically.
              sendJson(res, 502, {
                ok: false,
                error: redactText(failure.message, 300) ?? 'the dashboard could not be reached',
              })
            }
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
        let upstream
        try {
          upstream = await callUpstream(config.port, 'GET', `${path}${suffix}`)
        } catch (failure) {
          noteCollector(path, { ok: false, error: redactText(String(failure?.message ?? failure), 200) })
          throw failure
        }
        // An Agent read is evidence about collection health too, so it feeds
        // the same record the panel's indicator reads.
        if (COLLECTOR_PATHS.has(path)) {
          let failed = upstream.status >= 400 ? `upstream status ${upstream.status}` : null
          if (!failed) {
            try {
              const parsed = JSON.parse(upstream.text)
              if (parsed && parsed.error) failed = redactText(String(parsed.error), 200)
            } catch {
              // Unparseable 2xx: delivered, same as the return below.
            }
          }
          noteCollector(path, failed ? { ok: false, error: failed } : { ok: true })
        }
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

  /*
   * On activation: clone the dashboard if it is missing, then autostart.
   *
   * The clone must finish before the start, or `start()` would look for a
   * checkout that is not there yet. Both are best-effort — a clone or start
   * failure is reported through `state`, never thrown at load — so the panel
   * always comes up and can explain what went wrong.
   *
   * Sequenced in one chain rather than awaited at the top level, because
   * `apply` is synchronous: the Loader does not wait on it, and blocking it on
   * a network clone would stall activation.
   */
  cloneDashboardIfMissing()
    .catch(() => {
      // Already recorded in cloneState; nothing more to do.
    })
    .then(() => {
      if (config.autoStart) {
        return start().catch(() => {
          // A failed autostart is reported through `state`, not thrown at load.
        })
      }
      return undefined
    })
}
