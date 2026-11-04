window.__ModuleLoader__.load({
  id: '@local/fl-upgrade-workflow',
  factory(require) {
    const React = require('react')
    const h = React.createElement

    /** The plugin's single sidebar entry and its matching main panel key. */
    const PANEL_ID = 'fl-upgrade'
    /** Matches the Host route registered at '/fl-upgrade' (no trailing slash). */
    const BASE = '/fl-upgrade/'

    const TEXT = {
      en: {
        title: 'FL Adaptation',
        heading: 'FL adaptation workbench',
        start: 'Start',
        stop: 'Stop',
        reload: 'Reload',
        running: 'Running',
        stopped: 'Stopped',
        starting: 'Starting',
        stopping: 'Stopping',
        notConfigured: 'Not configured',
        openDashboard: 'Open dashboard',
        adopted: 'External',
        adoptedHint: 'This dashboard was started outside the plugin, so it is read here but not managed.',
        noProjects: 'No project is configured.',
        branch: 'Branch',
        engine: 'engine',
        collected: 'collected',
        never: 'not collected',
        tabBackends: 'Backends',
        tabDevices: 'Devices',
        tabTasks: 'Tasks',
        backend: 'Backend',
        integration: 'Merge',
        ci: 'CI',
        acceptance: 'Acceptance',
        gate: 'Gate',
        noBackends: 'No backend status yet.',
        noDevices: 'No device reported.',
        noTasks: 'No task is recorded.',
        host: 'Host',
        verdict: 'State',
        cards: 'Free',
        task: 'Task',
        branchColumn: 'Branch',
        status: 'Status',
        source: 'Source',
        refresh: 'Re-collect',
        loading: 'Loading',
        error: 'Error',
        stale: 'cached',
        refreshing: 'collecting',
        processOutput: 'Process output',
        openTask: 'Open',
        branchLimit: 'The dashboard caches at most 12 branches per project and has reached that limit. Restart it to view more branches.',
        dashboardDown: 'The dashboard is not running.',
        fleet: 'hosts, free cards',
        // Deploy flow
        deployTitle: 'Deploy on this machine',
        deployIntro: 'This plugin reads a local Python dashboard from the fl-plugin-upgrade-workflow checkout. Configure and start it here.',
        deployNeedRepo: 'Set repoPath in the plugin configuration to the fl-plugin-upgrade-workflow checkout, then reload.',
        deployNeedScript: 'That path is not an fl-plugin-upgrade-workflow checkout (scripts/configure.py is missing).',
        deployPickProjects: 'Choose the projects to track, and give each one a local plugin checkout and target branch.',
        deployRepoPath: 'Plugin checkout path',
        deployRemote: 'Remote',
        deployBranch: 'Target branch',
        deployRun: 'Configure and start',
        deployRunning: 'Configuring…',
        deployHint: 'Requires Python 3.11+ and Git. Clone the plugin repository first:',
        deployDone: 'Configured. The dashboard is starting.',
        deployAlready: 'Already configured.',
        reqRepo: 'Repository',
        reqConfig: 'Configuration',
        reqReady: 'ready',
        reqMissing: 'missing',
        // Task drill-in
        backToTasks: 'All tasks',
        agentMessage: 'Latest agent message',
        plan: 'Plan',
        progress: 'Progress',
        talkToAgent: 'Talk to the agent',
        taskReadOnly: 'This task is observed read-only, so it cannot be driven from here. Open the dashboard to work on it.',
        provider: 'Agent backend',
        message: 'Message',
        messagePlaceholder: 'Tell the agent what to do next…',
        send: 'Send',
        sending: 'Sending',
        sentDone: 'Sent.',
        workBranch: 'Work branch',
        updated: 'updated',
        taskStale: 'stale',
        // Task grouping
        taskBranchFilter: 'Branch',
        taskAllBranches: 'All branches',
        noTasksForBranch: 'No tasks on this branch.',
        // Why the composer is unavailable
        taskNoSession: 'This task is observation-only and has no dashboard session, so it cannot be driven from here.',
        taskDesktopOwned: 'This task was handed off to the Codex desktop app. Continue it there.',
        taskMonitorMode: 'This backend is monitor-only on this branch, so the dashboard refuses new work.',
        providerFixed: 'fixed when the task was created',
        providerUnavailable: 'unavailable',
        turnActive: 'turn in flight',
        monitorOnly: 'monitor only',
        pendingApproval: 'The agent is waiting for an approval. Answer it in the dashboard to let the turn continue.',
        // Transcript
        transcript: 'Transcript',
        transcriptEmpty: 'no transcript yet',
        transcriptNone: 'No events recorded.',
        transcriptTruncated: 'Showing the most recent events.',
        evApproval: '[approval requested]',
        evQuestion: '[question for you]',
        evCommand: '[command run]',
        evFileChange: '[file changed]',
        evYourMessage: '[your message]',
        evTurn: '[turn]',
        evStep: '[step]',
        evStatus: '[status changed]',
        evDiff: '[diff updated]',
        evConnection: '[connection]',
        // Collection
        refreshBranch: 'Refresh branch',
        refreshBranchHint: 'Runs git fetch against the configured remote. Synchronous, and limited to one manual refresh per branch every 30 seconds.',
        refreshDevices: 'Refresh devices',
        refreshDevicesHint: 'Opens SSH sessions to the GPU hosts to re-read their counters. Ignored while a round is already in flight.',
        collectionNote: 'Background collection runs inside the dashboard and starts with it: branch snapshots refresh about every minute, device readings every 5 minutes. There is no separate switch; the buttons above only skip the wait.',
        // Workspace
        workspaceReady: 'Code is in the sidebar',
        workspaceAdd: 'Add code to sidebar',
        workspaceAddHint: 'Registers this task\u2019s git worktree as a workspace. Nothing is created, moved or deleted, and tasks sharing one path share one workspace.',
        workspaceAdding: 'Adding',
        workspaceNone: 'This task has no local worktree yet, so there is nothing to add to the sidebar.',
      },
      zh: {
        title: 'FL 适配',
        heading: 'FL 适配工作台',
        start: '启动',
        stop: '停止',
        reload: '刷新',
        running: '运行中',
        stopped: '已停止',
        starting: '启动中',
        stopping: '停止中',
        notConfigured: '尚未配置',
        openDashboard: '打开看板',
        adopted: '外部',
        adoptedHint: '该看板不是由插件启动的，这里只读取状态，不做进程管理。',
        noProjects: '尚未配置项目。',
        branch: '分支',
        engine: '引擎',
        collected: '采集于',
        never: '未采集',
        tabBackends: '后端',
        tabDevices: '设备',
        tabTasks: '任务',
        backend: '后端',
        integration: '合入',
        ci: 'CI',
        acceptance: '验收',
        gate: '门禁',
        noBackends: '暂无后端状态。',
        noDevices: '暂无设备数据。',
        noTasks: '暂无任务记录。',
        host: '机器',
        verdict: '状态',
        cards: '空闲卡',
        task: '任务',
        branchColumn: '分支',
        status: '状态',
        source: '来源',
        refresh: '重新采集',
        loading: '加载中',
        error: '错误',
        stale: '缓存',
        refreshing: '采集中',
        processOutput: '进程输出',
        openTask: '打开',
        branchLimit: '看板每个项目最多缓存 12 个分支，当前已达上限。重启看板后可查看更多分支。',
        dashboardDown: '看板未运行。',
        fleet: '台在线，空闲卡',
        // Deploy flow
        deployTitle: '在本机部署',
        deployIntro: '本插件读取 fl-plugin-upgrade-workflow 检出中的本地 Python 看板。可在此完成配置并启动。',
        deployNeedRepo: '请在插件配置中把 repoPath 设为 fl-plugin-upgrade-workflow 的检出路径，然后刷新。',
        deployNeedScript: '该路径不是 fl-plugin-upgrade-workflow 检出（缺少 scripts/configure.py）。',
        deployPickProjects: '选择需要跟踪的项目，并为每个项目填写本地插件检出路径和目标分支。',
        deployRepoPath: '插件检出路径',
        deployRemote: 'Remote',
        deployBranch: '目标分支',
        deployRun: '配置并启动',
        deployRunning: '配置中…',
        deployHint: '需要 Python 3.11+ 和 Git。请先 clone 插件仓库：',
        deployDone: '配置完成，看板正在启动。',
        deployAlready: '已配置。',
        reqRepo: '仓库',
        reqConfig: '配置',
        reqReady: '就绪',
        reqMissing: '缺失',
        // 任务详情
        backToTasks: '全部任务',
        agentMessage: 'Agent 最新消息',
        plan: '计划',
        progress: '进展',
        talkToAgent: '与 Agent 交互',
        taskReadOnly: '该任务为只读观察，无法在此驱动。请到看板中操作。',
        provider: 'Agent 后端',
        message: '消息',
        messagePlaceholder: '告诉 Agent 下一步做什么…',
        send: '发送',
        sending: '发送中',
        sentDone: '已发送。',
        workBranch: '工作分支',
        updated: '更新于',
        taskStale: '已过期',
        // 任务分组
        taskBranchFilter: '分支',
        taskAllBranches: '全部分支',
        noTasksForBranch: '该分支暂无任务。',
        // 不能交互的原因
        taskNoSession: '该任务为观察记录，没有对应的看板会话，无法在此驱动。',
        taskDesktopOwned: '该任务已转交 Codex 桌面端，请在那里继续。',
        taskMonitorMode: '该后端在此分支为仅监控模式，看板不接受新的适配工作。',
        providerFixed: '创建任务时确定，不可更改',
        providerUnavailable: '不可用',
        turnActive: '正在执行',
        monitorOnly: '仅监控',
        pendingApproval: 'Agent 正在等待审批，请到看板中处理，否则本轮不会继续。',
        // 对话记录
        transcript: '对话记录',
        transcriptEmpty: '尚无记录',
        transcriptNone: '没有事件记录。',
        transcriptTruncated: '仅显示最近的事件。',
        evApproval: '[请求审批]',
        evQuestion: '[向你提问]',
        evCommand: '[执行命令]',
        evFileChange: '[修改文件]',
        evYourMessage: '[你的消息]',
        evTurn: '[轮次]',
        evStep: '[步骤]',
        evStatus: '[状态变化]',
        evDiff: '[更新 diff]',
        evConnection: '[连接]',
        // 采集
        refreshBranch: '重新采集分支',
        refreshBranchHint: '对配置的远端执行 git fetch。同步执行，每个分支 30 秒内只接受一次手动刷新。',
        refreshDevices: '重新采集设备',
        refreshDevicesHint: '通过 SSH 连到 GPU 机器重读计数。该项目已有采集在进行时会被忽略。',
        collectionNote: '后台采集在看板进程内运行，随看板一起启动：分支快照约每分钟一次，设备读数每 5 分钟一次。没有单独的开关，上面的按钮只是不等下一轮。',
        // 工作区
        workspaceReady: '代码已在左侧',
        workspaceAdd: '把代码加到左侧',
        workspaceAddHint: '把该任务的 git worktree 注册为工作区。不会创建、移动或删除任何文件；共用同一路径的任务会归到同一个工作区。',
        workspaceAdding: '添加中',
        workspaceNone: '该任务还没有本地 worktree，暂时没有可加入左侧的代码。',
      },
    }

    /** Carries the plugin context to the panel tree without module-level state. */
    const PluginContext = React.createContext(null)

    /** Resolve the active locale's dictionary, falling back to English. */
    function useText() {
      const ctx = React.useContext(PluginContext)
      const locale = ctx ? ctx.get('locale') : null
      const [, bump] = React.useState(0)
      React.useEffect(() => {
        if (!locale || typeof locale.subscribe !== 'function') return undefined
        return locale.subscribe(() => bump(n => n + 1))
      }, [locale])
      return dictFor(locale)
    }

    /** Pick a dictionary for a locale service snapshot. */
    function dictFor(locale) {
      const active = locale && typeof locale.getSnapshot === 'function'
        ? String(locale.getSnapshot().active || 'en')
        : 'en'
      return active.toLowerCase().startsWith('zh') ? TEXT.zh : TEXT.en
    }

    const CSS = `
.flup-root {
  display: flex; flex-direction: column; height: 100%; min-height: 0;
  background: var(--dsw-alias-bg-base); color: var(--dsw-alias-label-primary);
  font-size: 13px; line-height: 1.5;
}
.flup-head {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  padding: 14px 20px; border-bottom: 1px solid var(--dsw-alias-border-l1);
}
.flup-title { font-size: 15px; font-weight: 600; margin: 0; }
.flup-spacer { flex: 1 1 auto; }
.flup-split { display: flex; flex: 1 1 auto; min-height: 0; }
.flup-rail {
  flex: none; width: 184px; border-right: 1px solid var(--dsw-alias-border-l1);
  padding: 10px 8px; overflow: auto; display: flex; flex-direction: column; gap: 2px;
}
/* Every control carries a visible border so it reads as clickable without
   hover. The active item is marked by a brand-colored border plus a left bar,
   which survives a theme where layer backgrounds sit close together. */
.flup-rail-btn {
  appearance: none; font: inherit; text-align: left; cursor: pointer;
  border: 1px solid var(--dsw-alias-border-l2); border-radius: 6px;
  background: var(--dsw-alias-bg-layer-1); color: var(--dsw-alias-label-primary);
  padding: 7px 10px 7px 9px; border-left-width: 3px;
  display: flex; flex-direction: column; gap: 1px; width: 100%;
}
.flup-rail-btn:hover { background: var(--dsw-alias-bg-layer-2); }
.flup-rail-btn[aria-current="true"] {
  background: var(--dsw-alias-bg-layer-2); font-weight: 500;
  border-color: var(--dsw-alias-brand-primary);
}
.flup-rail-btn:focus-visible { outline: 2px solid var(--dsw-alias-brand-primary); outline-offset: 1px; }
.flup-rail-sub { font-size: 11px; color: var(--dsw-alias-label-secondary); font-weight: 400; }
.flup-pane { flex: 1 1 auto; min-width: 0; display: flex; flex-direction: column; min-height: 0; }
.flup-sub {
  display: flex; align-items: center; gap: 6px 14px; flex-wrap: wrap;
  padding: 8px 20px; color: var(--dsw-alias-label-secondary); font-size: 12px;
  border-bottom: 1px solid var(--dsw-alias-border-l1);
}
.flup-sub b { color: var(--dsw-alias-label-primary); font-weight: 500; }
.flup-btn {
  appearance: none; border: 1px solid var(--dsw-alias-border-l2);
  background: var(--dsw-alias-bg-layer-1); color: var(--dsw-alias-label-primary);
  border-radius: 6px; padding: 4px 11px; font: inherit; font-size: 12px; cursor: pointer;
  text-decoration: none; white-space: nowrap;
}
.flup-btn:hover:not(:disabled) { background: var(--dsw-alias-bg-layer-2); }
.flup-btn:disabled { opacity: 0.5; cursor: default; }
.flup-btn:focus-visible { outline: 2px solid var(--dsw-alias-brand-primary); outline-offset: 1px; }
.flup-btn-primary { border-color: transparent; background: var(--dsw-alias-brand-primary); color: #fff; }
.flup-select, .flup-input {
  font: inherit; font-size: 12px; border-radius: 6px; padding: 4px 8px;
  border: 1px solid var(--dsw-alias-border-l2); background: var(--dsw-alias-bg-layer-1);
  color: var(--dsw-alias-label-primary); max-width: 100%;
}
.flup-select:focus-visible, .flup-input:focus-visible {
  outline: 2px solid var(--dsw-alias-brand-primary); outline-offset: 1px;
}
.flup-input { width: 100%; }
.flup-dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%; flex: none; }
.flup-state { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--dsw-alias-label-secondary); }
.flup-tabs { display: flex; gap: 6px; padding: 10px 20px 0; }
.flup-tab {
  appearance: none; font: inherit; font-size: 12px; cursor: pointer;
  border: 1px solid var(--dsw-alias-border-l2); border-radius: 6px;
  background: var(--dsw-alias-bg-layer-1); color: var(--dsw-alias-label-primary);
  padding: 5px 12px;
}
.flup-tab:hover { background: var(--dsw-alias-bg-layer-2); }
.flup-tab[aria-selected="true"] {
  background: var(--dsw-alias-bg-layer-2); font-weight: 500;
  border-color: var(--dsw-alias-brand-primary); box-shadow: inset 0 -2px 0 var(--dsw-alias-brand-primary);
}
.flup-tab:focus-visible { outline: 2px solid var(--dsw-alias-brand-primary); outline-offset: 1px; }
.flup-body { flex: 1 1 auto; min-height: 0; overflow: auto; padding: 12px 20px 28px; }
.flup-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.flup-table th {
  text-align: left; font-weight: 500; color: var(--dsw-alias-label-secondary);
  padding: 0 10px 6px 0; border-bottom: 1px solid var(--dsw-alias-border-l1); font-size: 11px;
  position: sticky; top: 0; background: var(--dsw-alias-bg-base);
}
.flup-table td { padding: 7px 10px 7px 0; border-bottom: 1px solid var(--dsw-alias-border-l1); }
.flup-table tr:last-child td { border-bottom: none; }
.flup-table td:first-child { color: var(--dsw-alias-label-primary); }
.flup-table-rows tbody tr:hover { background: var(--dsw-alias-bg-layer-1); }
.flup-btn-sm { padding: 2px 9px; font-size: 11px; }
/* A row title that opens a drill-in: underlined so it reads as a link, but a
   real button for keyboard and screen-reader users. */
.flup-link {
  appearance: none; border: none; background: none; font: inherit; font-size: 12px;
  color: var(--dsw-alias-brand-primary); cursor: pointer; padding: 0; text-align: left;
  text-decoration: underline; text-underline-offset: 2px;
}
.flup-link:focus-visible { outline: 2px solid var(--dsw-alias-brand-primary); outline-offset: 2px; }
/* Branch filter chips above the task list. */
.flup-filter { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; padding-bottom: 12px; }
.flup-chip {
  appearance: none; font: inherit; font-size: 11px; cursor: pointer;
  border: 1px solid var(--dsw-alias-border-l2); border-radius: 999px;
  background: var(--dsw-alias-bg-layer-1); color: var(--dsw-alias-label-primary);
  padding: 3px 11px; white-space: nowrap;
}
.flup-chip:hover { background: var(--dsw-alias-bg-layer-2); }
.flup-chip[aria-pressed="true"] {
  border-color: var(--dsw-alias-brand-primary); background: var(--dsw-alias-bg-layer-2); font-weight: 500;
}
.flup-chip:focus-visible { outline: 2px solid var(--dsw-alias-brand-primary); outline-offset: 1px; }
/* Task drill-in. */
.flup-detail-head { display: flex; align-items: center; gap: 8px; padding-bottom: 10px; }
.flup-detail-title { margin: 0 0 4px; font-size: 14px; font-weight: 600; color: var(--dsw-alias-label-primary); }
.flup-sub-flush { padding-left: 0; padding-right: 0; }
.flup-card {
  border: 1px solid var(--dsw-alias-border-l1); border-radius: 8px;
  padding: 12px 14px; margin-top: 12px; background: var(--dsw-alias-bg-layer-1);
}
.flup-card-title {
  margin: 0 0 8px; font-size: 12px; font-weight: 600; color: var(--dsw-alias-label-primary);
}
.flup-pre {
  margin: 0; font-size: 12px; line-height: 1.55; white-space: pre-wrap; word-break: break-word;
  color: var(--dsw-alias-label-primary); max-height: 320px; overflow: auto;
}
.flup-plan { margin: 0; padding-left: 20px; font-size: 12px; line-height: 1.8; }
.flup-plan-done { color: var(--dsw-alias-label-secondary); }
.flup-textarea { resize: vertical; min-height: 64px; line-height: 1.5; }
.flup-actions { display: flex; align-items: center; gap: 10px; }
.flup-err-inline { color: var(--dsw-alias-state-error-primary); }
.flup-note {
  margin: 0; padding: 8px 12px; font-size: 12px; line-height: 1.5;
  color: var(--dsw-alias-label-secondary); border-bottom: 1px solid var(--dsw-alias-border-l1);
}
.flup-warn {
  margin: 10px 0 0; font-size: 12px; padding: 8px 10px; border-radius: 6px;
  border: 1px solid var(--dsw-alias-state-warn-primary); color: var(--dsw-alias-label-primary);
}
.flup-events {
  margin: 0; padding: 0; list-style: none; font-size: 12px; line-height: 1.55;
  max-height: 380px; overflow: auto; display: flex; flex-direction: column; gap: 6px;
}
.flup-event { display: flex; gap: 8px; align-items: baseline; }
.flup-event-bare { opacity: 0.7; }
.flup-event-text { white-space: pre-wrap; word-break: break-word; min-width: 0; }
.flup-pill { display: inline-flex; align-items: center; gap: 5px; white-space: nowrap; }
.flup-num { font-variant-numeric: tabular-nums; color: var(--dsw-alias-label-secondary); }
.flup-tag {
  border-radius: 4px; padding: 1px 6px; font-size: 11px;
  background: var(--dsw-alias-bg-layer-2); color: var(--dsw-alias-label-secondary);
}
.flup-card {
  border: 1px solid var(--dsw-alias-border-l1); border-radius: 8px;
  background: var(--dsw-alias-bg-layer-1); padding: 13px 15px; margin-bottom: 10px;
}
.flup-card-title { font-size: 13px; font-weight: 600; margin: 0 0 6px; }
.flup-meta { color: var(--dsw-alias-label-secondary); font-size: 12px; }
.flup-empty { color: var(--dsw-alias-label-secondary); font-size: 12px; padding: 6px 0; }
.flup-code {
  display: block; background: var(--dsw-alias-bg-layer-2); border-radius: 6px;
  padding: 8px 10px; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 11px; white-space: pre-wrap; word-break: break-all; margin: 6px 0 0;
}
.flup-pre {
  max-height: 180px; overflow: auto; background: var(--dsw-alias-bg-layer-2);
  border-radius: 6px; padding: 8px 10px; font-size: 11px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  white-space: pre-wrap; word-break: break-all; margin: 0;
}
.flup-err { color: var(--dsw-alias-state-error-primary); font-size: 12px; }
.flup-ok { color: var(--dsw-alias-state-success-primary); font-size: 12px; }
.flup-link { color: var(--dsw-alias-brand-primary); }
.flup-form { display: flex; flex-direction: column; gap: 10px; }
.flup-field { display: flex; flex-direction: column; gap: 3px; }
.flup-label { font-size: 11px; color: var(--dsw-alias-label-secondary); }
.flup-check { display: flex; align-items: center; gap: 7px; font-size: 13px; font-weight: 500; }
.flup-grid { display: grid; grid-template-columns: 2fr 1fr 1fr; gap: 8px; }
@media (max-width: 720px) { .flup-grid { grid-template-columns: 1fr; } }
`

    /** Map a dashboard status word onto a theme state token. */
    function statusColor(status) {
      const value = String(status || '').toLowerCase()
      if (['passed', 'success', 'merged', 'free', 'online', 'ok'].includes(value)) {
        return 'var(--dsw-alias-state-success-primary)'
      }
      if (['failed', 'failure', 'error', 'down', 'blocked'].includes(value)) {
        return 'var(--dsw-alias-state-error-primary)'
      }
      if (['partial', 'pending', 'running', 'stale', 'open', 'unknown', 'busy'].includes(value)) {
        return 'var(--dsw-alias-state-warn-primary)'
      }
      return 'var(--dsw-alias-state-idle-primary)'
    }

    function Dot({ status }) {
      return h('span', { className: 'flup-dot', style: { background: statusColor(status) }, 'aria-hidden': true })
    }

    /** One status cell: a dot plus its word, linked when the source gives a URL. */
    function Status({ value }) {
      const text = value && typeof value === 'object' ? value.status : value
      if (!text) return h('span', { className: 'flup-meta' }, '—')
      const detail = value && typeof value === 'object' ? value.detail : null
      const url = value && typeof value === 'object' ? value.url : null
      const body = h(React.Fragment, null, h(Dot, { status: text }), String(text))
      return h('span', { className: 'flup-pill', title: detail ? String(detail) : undefined },
        url && typeof url === 'string' && /^https:\/\//.test(url)
          ? h('a', { className: 'flup-link', href: url, target: '_blank', rel: 'noreferrer noopener' }, body)
          : body,
      )
    }

    function localTime(value) {
      if (!value) return null
      const parsed = new Date(value)
      return Number.isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleString()
    }

    async function callJson(path, init) {
      const response = await fetch(path, { cache: 'no-store', ...init })
      const text = await response.text()
      let body = null
      try {
        body = text ? JSON.parse(text) : null
      } catch {
        body = { error: text.slice(0, 500) }
      }
      if (!response.ok) {
        const message = body && typeof body.error === 'string' ? body.error : `HTTP ${response.status}`
        const failure = new Error(message)
        failure.body = body
        throw failure
      }
      return body
    }

    const readPath = (path, params) => {
      const query = Object.entries(params || {})
        .filter(([, value]) => value !== undefined && value !== null && value !== '')
        .map(([key, value]) => `&${key}=${encodeURIComponent(String(value))}`)
        .join('')
      return `${BASE}read?path=${encodeURIComponent(path)}${query}`
    }

    /**
     * Statuses where a turn is moving, so the transcript is worth polling.
     * `unknown` is included: it is what the desktop observer reports for a
     * session it has not confirmed, and it does change.
     */
    const ACTIVE_STATUS = new Set([
      'starting', 'running', 'waiting_approval', 'waiting_input', 'interrupting',
    ])

    /**
     * Localized label for an event that carries no agent prose.
     *
     * Matched by suffix so provider-specific prefixes still resolve, with the
     * raw method as the fallback rather than hiding the event.
     */
    function methodLabel(method, t) {
      const name = String(method || '')
      if (/requestApproval$/.test(name)) return t.evApproval
      if (/requestUserInput$/.test(name)) return t.evQuestion
      if (/commandExecution/.test(name)) return t.evCommand
      if (/fileChange/.test(name)) return t.evFileChange
      if (/^dashboard\/message$/.test(name)) return t.evYourMessage
      if (/diff/.test(name)) return t.evDiff
      if (/^turn\//.test(name)) return t.evTurn
      if (/^thread\/status/.test(name)) return t.evStatus
      if (/^item\/(started|completed)$/.test(name)) return t.evStep
      if (/^connection\//.test(name)) return t.evConnection
      return name || '—'
    }

    function BackendTable({ status, t }) {
      const backends = (status && Array.isArray(status.backends)) ? status.backends : []
      if (!backends.length) return h('p', { className: 'flup-empty' }, t.noBackends)
      return h('table', { className: 'flup-table' },
        h('thead', null, h('tr', null,
          h('th', null, t.backend),
          h('th', null, t.integration),
          h('th', null, t.ci),
          h('th', null, t.acceptance),
          h('th', null, t.gate),
        )),
        h('tbody', null, backends.map(backend => {
          const gate = backend.gate
          return h('tr', { key: String(backend.id) },
            h('td', null,
              String(backend.label || backend.id),
              backend.pr && backend.pr.url
                ? h('a', {
                  className: 'flup-link',
                  style: { marginLeft: '6px', fontSize: '11px' },
                  href: String(backend.pr.url),
                  target: '_blank',
                  rel: 'noreferrer noopener',
                }, `#${backend.pr.number}`)
                : null,
            ),
            h('td', null, h(Status, { value: backend.integration })),
            h('td', null, h(Status, { value: backend.ci || backend.current_ci })),
            h('td', null, h(Status, {
              value: backend.acceptance && {
                status: backend.acceptance.status,
                detail: backend.acceptance.summary,
              },
            })),
            h('td', { className: 'flup-num' }, gate && gate.total ? `${gate.passed ?? 0}/${gate.total}` : '—'),
          )
        })),
      )
    }

    function DeviceTable({ devices, t }) {
      const hosts = (devices && Array.isArray(devices.hosts)) ? devices.hosts : []
      if (!hosts.length) return h('p', { className: 'flup-empty' }, t.noDevices)
      return h('table', { className: 'flup-table' },
        h('thead', null, h('tr', null,
          h('th', null, t.host),
          h('th', null, t.backend),
          h('th', null, t.verdict),
          h('th', null, t.cards),
        )),
        h('tbody', null, hosts.map(host => h('tr', { key: String(host.id) },
          h('td', null, String(host.label || host.id)),
          h('td', { className: 'flup-meta' }, String(host.backend || '—')),
          h('td', null, h('span', { className: 'flup-pill' }, h(Dot, { status: host.verdict }), String(host.verdict || '—'))),
          h('td', { className: 'flup-num' }, `${(host.accel && host.accel.free) ?? 0}/${(host.accel && host.accel.total) ?? 0}`),
        ))),
      )
    }

    /**
     * Task list. Each row opens the task's own view.
     *
     * The row is a real button rather than a click handler on the `tr`, so it
     * is reachable by keyboard and announced as actionable.
     */
    function TaskTable({ tasks, t, onOpen, branch, onBranch }) {
      const all = Array.isArray(tasks) ? tasks : []
      if (!all.length) return h('p', { className: 'flup-empty' }, t.noTasks)

      // The upstream task endpoint ignores a `branch` query parameter and
      // always returns every task for the project, so scope the list here.
      // Tasks do span branches, and mixing them hides which version a task
      // targets.
      const counts = new Map()
      for (const task of all) {
        const key = task.branch || '—'
        counts.set(key, (counts.get(key) || 0) + 1)
      }
      const branches = [...counts.keys()].sort()
      const rows = branch ? all.filter(task => (task.branch || '—') === branch) : all

      const picker = h('div', { className: 'flup-filter' },
        h('span', { className: 'flup-label' }, `${t.taskBranchFilter}:`),
        h('button', {
          className: 'flup-chip',
          type: 'button',
          'aria-pressed': !branch,
          onClick: () => onBranch(null),
        }, `${t.taskAllBranches} (${all.length})`),
        branches.map(name => h('button', {
          key: name,
          className: 'flup-chip',
          type: 'button',
          'aria-pressed': branch === name,
          onClick: () => onBranch(name),
        }, `${name} (${counts.get(name)})`)),
      )

      if (!rows.length) return h('div', null, picker, h('p', { className: 'flup-empty' }, t.noTasksForBranch))

      return h('div', null, picker, h('table', { className: 'flup-table flup-table-rows' },
        h('thead', null, h('tr', null,
          h('th', null, t.task),
          h('th', null, t.branchColumn),
          h('th', null, t.status),
          h('th', null, t.source),
          h('th', null, ''),
        )),
        h('tbody', null, rows.map((task, index) => h('tr', { key: String(task.id || index) },
          h('td', null, h('button', {
            className: 'flup-link',
            type: 'button',
            title: task.error ? String(task.error) : t.openTask,
            onClick: () => onOpen(task),
          }, String(task.title || task.id || '—'))),
          h('td', { className: 'flup-meta' }, String(task.branch || '—')),
          h('td', null, h('span', { className: 'flup-pill' }, h(Dot, { status: task.status }), String(task.status || '—'))),
          h('td', { className: 'flup-meta' }, String(task.source_label || task.provider || '')),
          h('td', { className: 'flup-num' }, h('button', {
            className: 'flup-btn flup-btn-sm',
            type: 'button',
            onClick: () => onOpen(task),
          }, t.openTask)),
        ))),
      ))
    }

    /**
     * One task, opened from the task list.
     *
     * Shows the agent's own plan and latest message, which provider is on it,
     * and lets the user send a follow-up. The dashboard owns task creation and
     * credentials; this view only talks to an existing session, so a task that
     * reports `controllable: false` is read-only here by design.
     */
    function TaskDetail({ project, task, t, onBack }) {
      const [providers, setProviders] = React.useState(null)
      const [draft, setDraft] = React.useState('')
      const [busy, setBusy] = React.useState(false)
      const [error, setError] = React.useState(null)
      const [sent, setSent] = React.useState(false)
      const [fresh, setFresh] = React.useState(task)
      const [events, setEvents] = React.useState(null)
      const [eventError, setEventError] = React.useState(null)
      const [workspace, setWorkspace] = React.useState(null)
      const [wsBusy, setWsBusy] = React.useState(false)
      const [wsError, setWsError] = React.useState(null)

      // A dashboard session id is 32 hex characters. Native observer rows and
      // read-only share rows never match, and have no transcript to read.
      const hasSession = /^[0-9a-f]{32}$/.test(String(task.id || ''))

      const load = React.useCallback(async () => {
        // A missing provider list only costs the health note, so let it fail
        // quietly; a failing task read is what the user needs to hear about.
        callJson(readPath('/api/agent-providers', { project: project.id }))
          .then(body => setProviders(body.providers || []))
          .catch(() => setProviders([]))
        try {
          const body = await callJson(readPath('/api/adaptation-tasks', { project: project.id }))
          const match = (body.tasks || []).find(row => row.id === task.id)
          if (match) setFresh(match)
          setError(null)
        } catch (failure) {
          setError(String(failure.message))
        }
      }, [project.id, task.id])

      // Whether this task's code already shows up in the sidebar. The reply
      // carries no filesystem path: the Host resolves the worktree itself.
      const loadWorkspace = React.useCallback(async () => {
        try {
          const body = await callJson(`${BASE}workspaces?project=${encodeURIComponent(project.id)}`)
          if (!body.supported) { setWorkspace({ supported: false }); return }
          const row = (body.tasks || []).find(entry => entry.task === task.id)
          setWorkspace({ supported: true, ...(row || { exists: false, missing: true }) })
        } catch {
          // Adoption is a convenience; a failed probe just hides the button.
          setWorkspace({ supported: false })
        }
      }, [project.id, task.id])

      const adopt = async () => {
        setWsBusy(true)
        setWsError(null)
        try {
          await callJson(`${BASE}adopt-workspace`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ project: project.id, task: task.id }),
          })
          await loadWorkspace()
        } catch (failure) {
          setWsError(String(failure.message))
        } finally {
          setWsBusy(false)
        }
      }

      // The transcript is a separate, cheaper call. Upstream pages backward
      // only, so there is no "events since": the newest page is re-read and
      // replaces what is shown.
      const loadEvents = React.useCallback(async () => {
        if (!hasSession) return
        try {
          const body = await callJson(`${BASE}transcript?${new URLSearchParams({
            project: project.id,
            task: task.id,
            limit: '40',
          })}`)
          setEvents(body)
          setEventError(null)
        } catch (failure) {
          setEventError(String(failure.message))
        }
      }, [project.id, task.id, hasSession])

      const row = fresh || task
      const live = ACTIVE_STATUS.has(String(row.status || ''))

      React.useEffect(() => { loadWorkspace() }, [loadWorkspace])

      React.useEffect(() => {
        load()
        loadEvents()
        // Upstream has no rate limit of its own and each transcript read costs
        // about a second regardless of page size, so poll only while the turn
        // is actually moving, and stop when the tab is hidden.
        const handle = setInterval(() => {
          if (document.hidden) return
          load()
          if (live) loadEvents()
        }, live ? 5_000 : 20_000)
        return () => clearInterval(handle)
      }, [load, loadEvents, live])

      const send = async () => {
        const text = draft.trim()
        if (!text) return
        setBusy(true)
        setError(null)
        setSent(false)
        try {
          // Deliberately no retry: a timed-out message may already have been
          // delivered, and upstream has no idempotency key.
          await callJson(`${BASE}task-message`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ project: project.id, task: task.id, message: text }),
          })
          setDraft('')
          setSent(true)
          await load()
          await loadEvents()
        } catch (failure) {
          setError(String(failure.message))
        } finally {
          setBusy(false)
        }
      }

      // Why the composer may be unavailable, most specific reason first. Each
      // of these is a hard refusal upstream, so say so rather than letting the
      // user discover it through a failed send.
      const blocked = !hasSession ? t.taskNoSession
        : row.desktop_owned ? t.taskDesktopOwned
        : row.policy_mode === 'monitor'
          ? [t.taskMonitorMode, row.policy_pr ? `PR #${row.policy_pr}` : null, row.policy_reason]
            .filter(Boolean).join(' · ')
          : row.controllable !== true ? t.taskReadOnly
            : null

      // The task's provider is fixed when the session is created and cannot be
      // changed afterwards, so this is status, not a choice.
      const mine = (providers || []).find(p => p.id === row.provider) || null

      return h('div', { className: 'flup-body' },
        h('div', { className: 'flup-detail-head' },
          h('button', { className: 'flup-btn', type: 'button', onClick: onBack }, `← ${t.backToTasks}`),
          h('span', { className: 'flup-spacer' }),
          h('button', { className: 'flup-btn', type: 'button', disabled: busy, onClick: load }, t.reload),
        ),
        h('h3', { className: 'flup-detail-title' }, String(row.title || row.id || '—')),
        h('div', { className: 'flup-sub flup-sub-flush' },
          h('span', { className: 'flup-pill' }, h(Dot, { status: row.status }), String(row.status || '—')),
          row.backend ? h('span', null, `${t.backend} `, h('b', null, String(row.backend))) : null,
          row.branch ? h('span', null, `${t.branchColumn} `, h('b', null, String(row.branch))) : null,
          row.work_branch ? h('span', null, `${t.workBranch} `, h('b', null, String(row.work_branch))) : null,
          row.provider ? h('span', null, `${t.provider} `, h('b', null, String(row.provider))) : null,
          row.source_label ? h('span', null, String(row.source_label)) : null,
          row.updated_at ? h('span', null, `${t.updated} ${localTime(row.updated_at)}`) : null,
          row.stale ? h('span', { className: 'flup-tag' }, t.taskStale) : null,
          row.turn_active ? h('span', { className: 'flup-tag' }, t.turnActive) : null,
          row.policy_mode === 'monitor' ? h('span', { className: 'flup-tag' }, t.monitorOnly) : null,
          row.policy_pr_url
            ? h('a', {
              className: 'flup-link',
              href: String(row.policy_pr_url),
              target: '_blank',
              rel: 'noreferrer noopener',
            }, row.policy_pr ? `PR #${row.policy_pr}` : 'PR')
            : null,
        ),
        // An approval blocks the turn; without this the task just looks hung.
        row.pending_count
          ? h('p', { className: 'flup-warn' }, `${t.pendingApproval} (${row.pending_count})`)
          : null,
        // Each task works in its own git worktree. Registering it as a
        // workspace puts that code in the sidebar so the task can be opened
        // directly. Registration only: no worktree is created or moved, and
        // two tasks pointing at one path resolve to a single workspace.
        workspace && workspace.supported
          ? h('p', { className: 'flup-note' },
            workspace.exists
              ? h(React.Fragment, null,
                h('span', { className: 'flup-ok' }, '✓ '),
                workspace.title ? `${t.workspaceReady}: ${workspace.title}` : t.workspaceReady)
              : workspace.missing
                ? t.workspaceNone
                : h(React.Fragment, null,
                  h('button', {
                    className: 'flup-btn flup-btn-sm',
                    type: 'button',
                    disabled: wsBusy,
                    title: t.workspaceAddHint,
                    onClick: adopt,
                  }, wsBusy ? `${t.workspaceAdding}…` : t.workspaceAdd),
                  ' ',
                  h('span', { className: 'flup-meta' }, t.workspaceAddHint)),
            wsError ? h('span', { className: 'flup-err-inline' }, ` · ${wsError}`) : null,
          )
          : null,
        row.error ? h('p', { className: 'flup-err' }, String(row.error)) : null,
        Array.isArray(row.plan) && row.plan.length
          ? h('div', { className: 'flup-card' },
            h('h4', { className: 'flup-card-title' }, t.plan),
            h('ol', { className: 'flup-plan' }, row.plan.map((step, i) => h('li', {
              key: String(i),
              className: step.status === 'completed' ? 'flup-plan-done' : null,
            }, h('span', { className: 'flup-pill' }, h(Dot, { status: step.status }), String(step.status || '—')), ' ', String(step.step || '—')))),
          )
          : null,
        row.last_agent_message
          ? h('div', { className: 'flup-card' },
            h('h4', { className: 'flup-card-title' },
              t.agentMessage,
              row.message_updated_at ? h('span', { className: 'flup-meta' }, ` · ${localTime(row.message_updated_at)}`) : null),
            h('pre', { className: 'flup-pre' }, String(row.last_agent_message)),
          )
          : null,
        row.progress_summary
          ? h('div', { className: 'flup-card' },
            h('h4', { className: 'flup-card-title' }, t.progress),
            h('pre', { className: 'flup-pre' }, String(row.progress_summary)),
          )
          : null,
        h('div', { className: 'flup-card' },
          h('h4', { className: 'flup-card-title' }, t.talkToAgent),
          blocked
            ? h('p', { className: 'flup-empty' }, blocked)
            : h('div', { className: 'flup-form' },
              // Provider is read-only: it is chosen when the session is
              // created and no route can change it afterwards.
              h('p', { className: 'flup-meta' },
                `${t.provider}: `,
                h('b', null, String(row.provider || '—')),
                mine && mine.model ? ` · ${mine.model}` : '',
                mine && mine.available === false
                  ? h('span', { className: 'flup-err-inline' }, ` · ${mine.error || t.providerUnavailable}`)
                  : '',
                ` · ${t.providerFixed}`),
              h('label', { className: 'flup-field' },
                h('span', { className: 'flup-label' }, t.message),
                h('textarea', {
                  className: 'flup-input flup-textarea',
                  rows: 4,
                  value: draft,
                  disabled: busy,
                  placeholder: t.messagePlaceholder,
                  onChange: event => setDraft(event.target.value),
                }),
              ),
              h('div', { className: 'flup-actions' },
                h('button', {
                  className: 'flup-btn flup-btn-primary',
                  type: 'button',
                  disabled: busy || !draft.trim(),
                  onClick: send,
                }, busy ? `${t.sending}…` : t.send),
                sent ? h('span', { className: 'flup-ok' }, t.sentDone) : null,
              ),
            ),
          error ? h('p', { className: 'flup-err' }, `${t.error}: ${error}`) : null,
        ),
        // Transcript last: it is the longest block, and the agent's own
        // summary above answers most questions without scrolling.
        hasSession
          ? h('div', { className: 'flup-card' },
            h('h4', { className: 'flup-card-title' }, t.transcript),
            eventError ? h('p', { className: 'flup-err' }, `${t.error}: ${eventError}`) : null,
            !events && !eventError ? h('p', { className: 'flup-empty' }, `${t.loading}…`) : null,
            events ? h(Transcript, { events, t }) : null,
          )
          : null,
      )
    }

    /**
     * The agent's event stream, newest last.
     *
     * Only `text` survives the Host projection: every other part of an event's
     * params carries absolute paths, captured stdout or raw diffs. An event
     * without text renders as its method label, which is enough to see that
     * the agent ran a command or asked for approval.
     */
    function Transcript({ events, t }) {
      const rows = Array.isArray(events.events) ? events.events : []
      if (!rows.length) {
        return h('p', { className: 'flup-empty' },
          events.status === 'unavailable' ? t.transcriptEmpty : t.transcriptNone)
      }
      return h('div', null,
        events.has_more ? h('p', { className: 'flup-meta' }, t.transcriptTruncated) : null,
        h('ol', { className: 'flup-events' }, rows.map((event, i) => h('li', {
          key: String(i),
          className: event.text ? 'flup-event' : 'flup-event flup-event-bare',
        },
          h('span', { className: 'flup-meta' }, localTime(event.at) || '—'),
          ' ',
          event.text
            ? h('span', { className: 'flup-event-text' }, String(event.text))
            : h('span', { className: 'flup-meta' }, methodLabel(event.method, t)),
        ))),
      )
    }

    /**
     * One project's view, including its branch picker.
     *
     * Branch is local state: the dashboard accepts any ref per request and
     * keeps a per-branch cache, so switching here does not disturb the
     * dashboard's own current branch or the other project.
     */
    function ProjectView({ project, running, t }) {
      const [branch, setBranch] = React.useState(null)
      const [branches, setBranches] = React.useState([])
      const [status, setStatus] = React.useState(null)
      const [devices, setDevices] = React.useState(null)
      const [tasks, setTasks] = React.useState(null)
      const [error, setError] = React.useState(null)
      const [busy, setBusy] = React.useState(false)
      const [tab, setTab] = React.useState('backends')
      const [openTask, setOpenTask] = React.useState(null)
      const [taskBranch, setTaskBranch] = React.useState(null)

      // Reset per-project state when the rail switches projects.
      React.useEffect(() => {
        setBranch(null)
        setOpenTask(null)
        setTaskBranch(null)
        setBranches([])
        setStatus(null)
        setDevices(null)
        setTasks(null)
        setError(null)
        setTab('backends')
      }, [project.id])

      // The branch list also reports the dashboard's current branch, which is
      // the default selection until the user picks another.
      React.useEffect(() => {
        if (!running) return undefined
        let alive = true
        callJson(readPath('/api/branches', { project: project.id }))
          .then(body => {
            if (!alive || !body) return
            setBranches(Array.isArray(body.branches) ? body.branches : [])
            setBranch(previous => previous ?? body.branch ?? null)
          })
          .catch(failure => { if (alive) setError(String(failure.message)) })
        return () => { alive = false }
      }, [project.id, running])

      const load = React.useCallback(async () => {
        if (!running) return
        try {
          const body = await callJson(readPath('/api/branch-status', { project: project.id, branch: branch || undefined }))
          setStatus(body)
          setError(body && body.error ? String(body.error) : null)
        } catch (failure) {
          setError(String(failure.message))
          return
        }
        const [deviceBody, taskBody] = await Promise.all([
          callJson(readPath('/api/devices', { project: project.id })).catch(() => null),
          callJson(readPath('/api/adaptation-tasks', { project: project.id })).catch(() => null),
        ])
        setDevices(deviceBody)
        setTasks(taskBody && Array.isArray(taskBody.tasks) ? taskBody.tasks : [])
      }, [project.id, branch, running])

      // Poll while mounted; a branch change reloads immediately through `load`.
      React.useEffect(() => {
        let alive = true
        const tick = () => { if (alive) load() }
        tick()
        const timer = setInterval(tick, 20_000)
        return () => {
          alive = false
          clearInterval(timer)
        }
      }, [load])

      /*
       * Force one collection round now.
       *
       * Background collection is already running inside the dashboard (it
       * starts with the process and cannot be started or stopped separately),
       * so these are "don't wait for the next round" actions, not switches.
       * Both are synchronous upstream and both reach outside this machine:
       * a branch refresh runs `git fetch` against the configured remote, and a
       * device refresh opens SSH sessions to the GPU hosts. Hence an explicit
       * button rather than anything automatic.
       */
      const refresh = React.useCallback(async kind => {
        setBusy(true)
        try {
          await callJson(`${BASE}refresh`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(kind === 'devices'
              ? { path: '/api/devices/refresh', project: project.id }
              : { path: '/api/branch-refresh', project: project.id, branch: branch || undefined }),
          })
          await load()
        } catch (failure) {
          setError(String(failure.message))
        } finally {
          setBusy(false)
        }
      }, [project.id, branch, load])

      if (!running) {
        return h('div', { className: 'flup-body' }, h('p', { className: 'flup-empty' }, t.dashboardDown))
      }

      // A drill-in replaces the whole project body: the branch picker above it
      // selects a monitored branch, which is a different axis from the task's
      // own target branch, and showing both at once reads as one control.
      if (openTask) {
        return h(TaskDetail, { project, task: openTask, t, onBack: () => setOpenTask(null) })
      }

      const collection = status && status.collection
      const tag = collection && collection.refreshing ? t.refreshing
        : (collection && collection.cached && collection.stale ? t.stale : null)

      const tabs = [
        ['backends', t.tabBackends],
        ['devices', t.tabDevices],
        ['tasks', t.tabTasks],
      ]

      return h(React.Fragment, null,
        h('div', { className: 'flup-sub' },
          h('label', { className: 'flup-pill' },
            h('span', { className: 'flup-label' }, `${t.branch}:`),
            h('select', {
              className: 'flup-select',
              value: branch || '',
              disabled: !branches.length,
              onChange: event => setBranch(event.target.value),
              'aria-label': t.branch,
            },
              // The current branch may sit outside the listed set; keep it
              // selectable so the control always reflects what is shown.
              (branch && !branches.includes(branch) ? [branch] : []).concat(branches).map(name =>
                h('option', { key: name, value: name }, name)),
            ),
          ),
          status && status.head
            ? h('span', null, status.head_url
              ? h('a', { className: 'flup-link', href: String(status.head_url), target: '_blank', rel: 'noreferrer noopener' }, String(status.head).slice(0, 10))
              : String(status.head).slice(0, 10))
            : null,
          status && status.upstream_version ? h('span', null, `${t.engine} `, h('b', null, String(status.upstream_version))) : null,
          h('span', null, `${t.collected} ${(status && localTime(status.observed_at)) || t.never}`),
          tag ? h('span', { className: 'flup-tag' }, tag) : null,
          h('span', { className: 'flup-spacer' }),
          // Background collection runs on its own; these force a round early.
          h('button', {
            className: 'flup-btn',
            type: 'button',
            disabled: busy,
            title: t.refreshBranchHint,
            onClick: () => refresh('branch'),
          }, t.refreshBranch),
          h('button', {
            className: 'flup-btn',
            type: 'button',
            disabled: busy,
            title: t.refreshDevicesHint,
            onClick: () => refresh('devices'),
          }, t.refreshDevices),
        ),
        // Collection has no start/stop: it begins with the dashboard process
        // and runs on fixed intervals. Say so, so the absent button is not
        // read as a missing feature.
        h('p', { className: 'flup-note' }, t.collectionNote),
        h('div', { className: 'flup-tabs', role: 'tablist' }, tabs.map(([id, text]) => h('button', {
          key: id,
          className: 'flup-tab',
          type: 'button',
          role: 'tab',
          'aria-selected': tab === id,
          onClick: () => setTab(id),
        }, text))),
        h('div', { className: 'flup-body' },
          // The dashboard caps its per-project snapshot cache at 12 branches
          // and then refuses new ones; say what that means and how to clear it.
          error
            ? h('p', { className: 'flup-err' }, /branch limit/i.test(error) ? t.branchLimit : `${t.error}: ${error}`)
            : null,
          !status && !error ? h('p', { className: 'flup-empty' }, `${t.loading}…`) : null,
          tab === 'backends' && status ? h(BackendTable, { status, t }) : null,
          tab === 'devices' ? h(DeviceTable, { devices, t }) : null,
          tab === 'tasks'
            ? h(TaskTable, { tasks, t, onOpen: setOpenTask, branch: taskBranch, onBranch: setTaskBranch })
            : null,
        ),
      )
    }

    /**
     * Deploy view: configure the checkout and start it, without a terminal.
     *
     * The repo's own non-interactive wizard does the work. Plugin checkout
     * paths cannot be discovered, so the user supplies them here.
     */
    function DeployView({ t, onDone }) {
      const [preflight, setPreflight] = React.useState(null)
      const [picked, setPicked] = React.useState({})
      const [busy, setBusy] = React.useState(false)
      const [error, setError] = React.useState(null)
      const [output, setOutput] = React.useState(null)
      const [done, setDone] = React.useState(false)

      React.useEffect(() => {
        let alive = true
        callJson(`${BASE}deploy-preflight`)
          .then(body => { if (alive) setPreflight(body) })
          .catch(failure => { if (alive) setError(String(failure.message)) })
        return () => { alive = false }
      }, [])

      const check = id => (preflight && preflight.checks || []).find(entry => entry.id === id)
      const repoCheck = check('repo')
      const scriptCheck = check('configure-script')

      const toggle = id => setPicked(current => {
        const next = { ...current }
        if (next[id]) delete next[id]
        else next[id] = { repoPath: '', remote: 'origin', branch: '' }
        return next
      })
      const edit = (id, field, value) => setPicked(current => ({
        ...current,
        [id]: { ...current[id], [field]: value },
      }))

      const submit = React.useCallback(async () => {
        setBusy(true)
        setError(null)
        setOutput(null)
        try {
          const result = await callJson(`${BASE}deploy`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              projects: Object.entries(picked).map(([id, value]) => ({ id, ...value })),
            }),
          })
          if (result && result.output) setOutput(String(result.output))
          if (result && result.ok) {
            setDone(true)
            if (onDone) onDone()
          } else {
            setError(String((result && result.error) || 'the deployment failed'))
          }
        } catch (failure) {
          setError(String(failure.message))
        } finally {
          setBusy(false)
        }
      }, [picked, onDone])

      const chosen = Object.entries(picked)
      const complete = chosen.length > 0 && chosen.every(([, value]) => value.repoPath.trim() && value.branch.trim())

      return h('div', { className: 'flup-body' },
        h('div', { className: 'flup-card' },
          h('h3', { className: 'flup-card-title' }, t.deployTitle),
          h('p', { className: 'flup-meta' }, t.deployIntro),
          h('p', { className: 'flup-meta', style: { marginTop: '8px' } },
            `${t.reqRepo}: `,
            h('span', { className: repoCheck && repoCheck.ok ? 'flup-ok' : 'flup-err' },
              repoCheck && repoCheck.ok ? t.reqReady : t.reqMissing),
            ' · ',
            `${t.reqConfig}: `,
            h('span', { className: check('configured') && check('configured').ok ? 'flup-ok' : 'flup-meta' },
              check('configured') && check('configured').ok ? t.deployAlready : t.reqMissing),
          ),
          !preflight ? h('p', { className: 'flup-empty' }, `${t.loading}…`) : null,
          preflight && repoCheck && !repoCheck.ok ? h('p', { className: 'flup-err' }, t.deployNeedRepo) : null,
          preflight && repoCheck && repoCheck.ok && scriptCheck && !scriptCheck.ok
            ? h('p', { className: 'flup-err' }, t.deployNeedScript)
            : null,
        ),

        preflight && scriptCheck && scriptCheck.ok
          ? h('div', { className: 'flup-card' },
            h('p', { className: 'flup-meta' }, t.deployPickProjects),
            h('div', { className: 'flup-form', style: { marginTop: '10px' } },
              (preflight.projects || []).map(project => h('div', { key: project.id },
                h('label', { className: 'flup-check' },
                  h('input', {
                    type: 'checkbox',
                    checked: Boolean(picked[project.id]),
                    onChange: () => toggle(project.id),
                  }),
                  String(project.label),
                ),
                picked[project.id]
                  ? h('div', { className: 'flup-grid', style: { marginTop: '7px', marginLeft: '24px' } },
                    h('div', { className: 'flup-field' },
                      h('span', { className: 'flup-label' }, t.deployRepoPath),
                      h('input', {
                        className: 'flup-input',
                        type: 'text',
                        value: picked[project.id].repoPath,
                        placeholder: project.repo,
                        onChange: event => edit(project.id, 'repoPath', event.target.value),
                      }),
                    ),
                    h('div', { className: 'flup-field' },
                      h('span', { className: 'flup-label' }, t.deployRemote),
                      h('input', {
                        className: 'flup-input',
                        type: 'text',
                        value: picked[project.id].remote,
                        onChange: event => edit(project.id, 'remote', event.target.value),
                      }),
                    ),
                    h('div', { className: 'flup-field' },
                      h('span', { className: 'flup-label' }, t.deployBranch),
                      h('input', {
                        className: 'flup-input',
                        type: 'text',
                        value: picked[project.id].branch,
                        onChange: event => edit(project.id, 'branch', event.target.value),
                      }),
                    ),
                  )
                  : null,
              )),
              h('p', { className: 'flup-meta' }, t.deployHint),
              h('code', { className: 'flup-code' },
                (preflight.projects || []).map(project => `git clone ${project.repo}`).join('\n')),
              h('div', null,
                h('button', {
                  className: 'flup-btn flup-btn-primary',
                  type: 'button',
                  disabled: busy || !complete,
                  onClick: submit,
                }, busy ? t.deployRunning : t.deployRun),
              ),
              done ? h('p', { className: 'flup-ok' }, t.deployDone) : null,
              error ? h('p', { className: 'flup-err' }, `${t.error}: ${error}`) : null,
              output ? h('pre', { className: 'flup-pre' }, output) : null,
            ),
          )
          : null,
      )
    }

    /**
     * The plugin's one panel: a project rail on the left, the selected view on
     * the right.
     *
     * Everything lives behind a single sidebar icon, so the sidebar stays as
     * crowded as it was before the plugin was installed.
     */
    function Panel() {
      const t = useText()
      const [state, setState] = React.useState(null)
      const [projects, setProjects] = React.useState([])
      const [selected, setSelected] = React.useState(null)
      const [error, setError] = React.useState(null)
      const [busy, setBusy] = React.useState(false)

      const load = React.useCallback(async () => {
        try {
          const next = await callJson(`${BASE}state`)
          setState(next)
          setError(null)
          const list = await callJson(`${BASE}projects`).catch(() => null)
          const rows = (list && Array.isArray(list.projects)) ? list.projects : []
          setProjects(rows)
          setSelected(current => {
            if (current && current !== 'deploy' && rows.some(row => row.id === current)) return current
            if (current === 'deploy') return current
            return rows.length ? rows[0].id : 'deploy'
          })
        } catch (failure) {
          setError(String(failure.message))
        }
      }, [])

      React.useEffect(() => {
        let alive = true
        const tick = () => { if (alive) load() }
        tick()
        // The panel-level poll only tracks lifecycle; project views poll their
        // own data, so this stays slow.
        const timer = setInterval(tick, 30_000)
        return () => {
          alive = false
          clearInterval(timer)
        }
      }, [load])

      const lifecycle = React.useCallback(async action => {
        setBusy(true)
        setError(null)
        try {
          const result = await callJson(`${BASE}${action}`, { method: 'POST' })
          if (result && result.state) setState(result.state)
          if (result && result.ok === false && result.error) setError(String(result.error))
          await load()
        } catch (failure) {
          setError(String(failure.message))
        } finally {
          setBusy(false)
        }
      }, [load])

      const running = Boolean(state && state.running)
      const configured = Boolean(state && state.configured)
      const adopted = Boolean(state && state.adopted)
      const project = projects.find(row => row.id === selected) || null

      return h('div', { className: 'flup-root' },
        // Component-local stylesheet: unmounting the panel removes it.
        h('style', null, CSS),
        h('div', { className: 'flup-head' },
          h('h2', { className: 'flup-title' }, t.heading),
          h('span', { className: 'flup-spacer' }),
          h('span', { className: 'flup-state', title: adopted ? t.adoptedHint : undefined },
            h(Dot, { status: running ? 'online' : (configured ? 'idle' : 'unknown') }),
            busy
              ? (running ? t.stopping : t.starting)
              : (adopted ? t.adopted : (running ? t.running : (configured ? t.stopped : t.notConfigured))),
          ),
          // An externally started dashboard offers no lifecycle button: the
          // plugin does not own that process.
          adopted ? null : h('button', {
            className: 'flup-btn flup-btn-primary',
            type: 'button',
            disabled: busy || !configured,
            onClick: () => lifecycle(running ? 'stop' : 'start'),
          }, running ? t.stop : t.start),
          h('button', { className: 'flup-btn', type: 'button', disabled: busy, onClick: load }, t.reload),
          running && state.port
            ? h('a', {
              className: 'flup-btn',
              href: `http://127.0.0.1:${state.port}/`,
              target: '_blank',
              rel: 'noreferrer noopener',
            }, t.openDashboard)
            : null,
        ),

        error ? h('div', { className: 'flup-sub' }, h('span', { className: 'flup-err' }, `${t.error}: ${error}`)) : null,

        h('div', { className: 'flup-split' },
          h('nav', { className: 'flup-rail', 'aria-label': t.heading },
            projects.map(row => h('button', {
              key: String(row.id),
              className: 'flup-rail-btn',
              type: 'button',
              'aria-current': selected === row.id,
              onClick: () => setSelected(row.id),
            },
              h('span', null, String(row.label || row.id)),
              row.default_branch ? h('span', { className: 'flup-rail-sub' }, String(row.default_branch)) : null,
            )),
            !projects.length && state ? h('p', { className: 'flup-empty', style: { padding: '0 10px' } }, t.noProjects) : null,
            h('button', {
              className: 'flup-rail-btn',
              type: 'button',
              'aria-current': selected === 'deploy',
              onClick: () => setSelected('deploy'),
              style: { marginTop: 'auto' },
            }, h('span', null, t.deployTitle)),
          ),

          h('div', { className: 'flup-pane' },
            !state
              ? h('div', { className: 'flup-body' }, h('p', { className: 'flup-empty' }, `${t.loading}…`))
              : selected === 'deploy'
                ? h(DeployView, { t, onDone: load })
                : project
                  ? h(ProjectView, { key: project.id, project, running, t })
                  : h('div', { className: 'flup-body' }, h('p', { className: 'flup-empty' }, t.noProjects)),
          ),
        ),

        state && !running && state.exit
          ? h('div', { className: 'flup-sub' },
            h('span', { className: 'flup-err' }, `${t.processOutput}: ${String(state.exit.reason || '')}`))
          : null,
      )
    }

    function Icon({ size }) {
      const edge = typeof size === 'number' ? size : 20
      return h('svg', {
        viewBox: '0 0 24 24',
        width: edge,
        height: edge,
        fill: 'none',
        stroke: 'currentColor',
        strokeWidth: 1.7,
        strokeLinecap: 'round',
        strokeLinejoin: 'round',
        'aria-hidden': true,
        style: { display: 'block' },
      },
        h('path', { d: 'M12 3.5v8' }),
        h('path', { d: 'M8.5 7 12 3.5 15.5 7' }),
        h('path', { d: 'M4 13.5h4.5v7H4zM9.75 10h4.5v10.5h-4.5zM15.5 15h4.5v5.5h-4.5z' }),
      )
    }

    return {
      inject: ['slots'],
      apply(ctx) {
        /** Provide the context to the panel tree. */
        const PanelRoot = props => h(PluginContext.Provider, { value: ctx }, h(Panel, props))

        const label = () => dictFor(ctx.get('locale')).title

        ctx.slots.inject('sidebar.panellist', () => ctx.slots.register(
          { name: 'sidebar.panellist', id: PANEL_ID, order: 20, label },
          Icon,
        ))

        ctx.slots.inject('main', () => ctx.slots.register(
          { name: 'main', key: PANEL_ID },
          PanelRoot,
        ))
      },
    }
  },
})
