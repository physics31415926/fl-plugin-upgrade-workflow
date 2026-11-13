window.__ModuleLoader__.load({
  id: '@physics31415926/fl-upgrade-workflow',
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
        restart: 'Restart dashboard',
        restarting: 'Restarting…',
        codeStale: 'Code updated — restart needed',
        codeStaleHint: (count, from, to) => `The dashboard is running code from when it started. ${to && from && to !== from ? `HEAD moved ${from} → ${to}. ` : ''}${count ? `${count} source file(s) changed since then. ` : ''}Restart to load it.`,
        codeUnknown: 'This dashboard is too old to report its code version; restart it once where it was started.',
        prCandidates: 'No PR matched automatically. Open PRs on this branch that name this backend:',
        noPrCandidates: 'No adaptation PR found on this branch.',
        pin: 'Pin',
        unpin: 'Unpin',
        pinning: 'Saving…',
        pinned: 'pinned',
        pinnedHint: 'Chosen by hand for this branch; outranks the config and automatic matching.',
        unpinHint: 'Go back to the configured or automatically matched PR.',
        draft: 'draft',
        gateStale: commit => `Gate evidence exists, but for older commit ${commit}; the gate counts only runs at the current branch HEAD.`,
        gateMissing: 'No gate evidence has been uploaded for the current branch HEAD.',
        running: 'Running',
        stopped: 'Stopped',
        starting: 'Starting',
        stopping: 'Stopping',
        cloning: 'Fetching the dashboard…',
        notConfigured: 'Not configured',
        openDashboard: 'Open dashboard',
        adopted: 'External',
        adoptedHint: 'This dashboard was started outside the plugin, so it is read here but not managed.',
        collecting: 'Collecting',
        collectStale: 'Collection failing',
        collectWaiting: 'No data yet',
        collectOkHint: 'Collection is delivering data. Last success: ',
        collectStaleHint: 'Collection is failing. Last error: ',
        collectWaitingHint: 'The collectors have not delivered data yet.',
        collectNeverOk: 'no successful round yet',
        collectDevices: 'devices',
        collectBranches: 'branches',
        collectTasks: 'tasks',
        updateAvailable: 'Update to',
        updateInstalling: 'Updating',
        updateCurrent: 'Up to date',
        updateCheck: 'Check for updates',
        updateDone: 'Updated to',
        updateRestart: 'Reload the window to run the new version.',
        updateLinkedHint: 'This plugin runs from a local checkout, so a release cannot be installed over it. Update it with git.',
        updateUnsupportedHint: 'This Harness build cannot install plugins from the panel.',
        updateCheckFailed: 'The update check failed',
        updateRateLimited: 'GitHub rate-limited the update check. Try again later.',
        updateNotes: 'Release notes',
        updateInstalledIs: 'Installed version',
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
        ciLatestRun: 'Latest run at branch HEAD',
        acceptance: 'Acceptance',
        gate: 'Gate',
        noBackends: 'No backend status yet.',
        noDevices: 'No device reported.',
        noTasks: 'No task is recorded.',
        devicesRailSub: 'shared by all projects',
        statFreeHosts: 'Idle hosts',
        statFreeCards: 'Free cards',
        statUnknown: 'Unreadable cards',
        statStale: 'Stale hosts',
        allBackends: 'All hardware',
        // Marks which half of a bilingual vendor name this dictionary wants.
        cjkNames: false,
        devicesNote: 'The GPU fleet is not tied to a project or a branch: every project draws from the same machines, so this view has no branch selector.',
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
        deployRailSetup: 'Start here: nothing is configured yet',
        deployRailCreds: 'Running, but a key is still missing',
        deployRailDone: 'Setup and credentials',
        deployIntro: 'This plugin reads a local Python dashboard from the fl-plugin-upgrade-workflow checkout. Configure and start it here.',
        deployRedoIntro: 'Setup is done. Run this again only to repoint the dashboard at a different checkout or branch.',
        deployNeedRepo: 'This plugin needs a local checkout of fl-plugin-upgrade-workflow, and none is configured yet.',
        deployWhereConfig: 'Clone it, then set repoPath to its absolute path: Plugins → FL adaptation workbench → configure. Reopen this panel afterwards.',
        deployNeedScript: 'The configured directory is not an fl-plugin-upgrade-workflow checkout: it has no scripts/configure.py.',
        deployFoundDir: 'Configured directory',
        deployPickProjects: 'Choose the projects to track. A detected checkout is used automatically; for one not on disk, type its path.',
        deployDetected: 'detected',
        deployNotDetected: 'not on disk',
        deployUsingDetected: 'Using checkout',
        deployBranchPlaceholder: 'Select a branch',
        deployHint: 'Requires Python 3.11+ and Git.',
        deployRepoPath: 'Plugin checkout path',
        deployRemote: 'Remote',
        deployBranch: 'Target branch',
        deployRun: 'Configure and start',
        deployRunning: 'Configuring…',
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
        workspaceSection: 'Code',
        workspaceStale: 'This task\u2019s code was added to the sidebar earlier, but its directory no longer exists \u2014 the dashboard prunes a worktree once its task ends. The sidebar entry opens nothing.',
        workspaceStaleTag: 'directory gone',
        workspaceForget: 'Remove the dead entry',
        workspaceForgetHint: 'Removes the sidebar registration only. The directory is already gone, and session history is kept.',
        workspaceForgetting: 'Removing',
        taskDesktopTag: 'desktop',
        taskObserverTag: 'observer',
        repoTitle: 'Project repository',
        repoUnknown: 'This project has no repository recorded in the dashboard config, so there is nothing to detect.',
        repoMissing: 'The configured checkout is not on disk yet.',
        repoCloneHint: 'It clones into the path the dashboard already configured for this project, from its configured GitHub repository.',
        repoClone: 'Clone it now',
        repoCloning: 'Cloning',
        repoCloneFailed: 'the clone failed',
        repoAdopted: 'In the sidebar.',
        repoNotAdopted: 'Detected on disk, not in the sidebar yet.',
        repoAdopt: 'Add to sidebar',
        repoStartSession: 'Start a conversation here',
        repoSessionHint: 'Opens a new conversation in this workspace, already pointed at the checkout and its tracked branch.',
        repoNoSession: 'this workspace is not available for a conversation yet',
        repoTrackedBranch: 'the tracked branch',
        repoSessionPrompt: 'I am working on {project}, tracked on branch {branch}. This workspace is its checkout. Start by reading the repository layout and telling me what state the branch is in.',
        taskScope: 'Show',
        taskScopeMine: 'created here',
        taskScopeAll: 'all in the dashboard',
        noTasksMine: 'No tasks have been created from this panel yet. Create one above, or switch to \u201call in the dashboard\u201d to see the ones that already exist.',
        createOpen: 'New task',
        createTitle: 'New adaptation task',
        createClose: 'Close',
        createIntro: 'Prepares a task. It does not start it: starting spends model tokens and runs commands on the remote host, so it stays in the dashboard.',
        createMachine: 'Machine',
        createPickMachine: 'pick a machine',
        createNoBackend: 'backend unknown',
        createBranch: 'Branch',
        createProvider: 'Agent',
        createGoal: 'Goal (optional)',
        createGoalHint: 'What this adaptation should achieve. Left empty, the dashboard uses its default.',
        createCost: 'Preparing adds a git worktree in the dashboard checkout and reserves this machine, which blocks other projects from using it until the task is deleted.',
        createRun: 'Prepare task',
        createRunning: 'Preparing',
        createDone: 'prepared',
        createNext: 'Prepared, not started. Open it below to review, then start it from the dashboard.',
        createFailed: 'the dashboard refused to create this task',
        createNeedsWorkspace: 'This machine has no remote working directory recorded, which preparing a task requires. Set one below.',
        createWorkspaceLabel: 'Remote working directory',
        createWorkspaceHint: 'e.g. /workspace/adapt',
        createWorkspaceSave: 'Save workspace',
        createWorkspaceSaving: 'Saving',
        createWorkspaceFailed: 'the dashboard rejected this path',
        createWorkspaceWhy: 'An absolute path on the remote GPU host where the task checks out and runs. It is saved on the machine profile and reused; JumpServer refreshes keep it.',
        createNoWorkspaces: 'No machine profile on this deployment has a remote working directory yet. Pick a machine above and set one — tasks can be prepared as soon as one machine has it.',
        createMonitorOnly: 'This backend is monitor-only on this branch: an adaptation PR already exists, so new tasks are refused.',
        createPairTaken: 'This backend already has a task on this branch. Only one is allowed at a time; delete the existing one first.',
        createBackendGuess: 'This machine\u2019s backend was inferred from its name rather than detected, so it may be wrong.',
        credTitle: 'Credentials',
        credIntro: 'The configure step writes repository paths and branches only, so credentials are collected here. Each value goes straight to an encrypted store \u2014 the dashboard\u2019s own for its providers, the Harness credential service for the GitHub token \u2014 and is never kept by this plugin, echoed back, or saved in the browser.',
        credPlaceholder: 'paste the value',
        credSave: 'Save',
        credSaving: 'Saving',
        credSaved: 'saved',
        credFailed: 'the dashboard rejected this value',
        credReady: 'ready',
        credUnavailable: 'unavailable',
        credCodexOk: 'signed in through its own CLI, nothing to enter here',
        credCodexNo: 'not signed in \u2014 run the Codex CLI login',
        credClaudeOk: 'key configured',
        credClaudeNo: 'no key configured',
        credClaudeDone: 'Claude key configured and accepted',
        credJumpDone: 'JumpServer signed in',
        credReplace: 'Replace',
        credCancel: 'Cancel',
        credClaudeLabel: 'Claude API key',
        credClaudeHint: 'Stored by the dashboard, sealed with Windows DPAPI under your user account, inside a git-ignored directory. Read back only as a yes/no.',
        credJumpOk: 'signed in, assets available',
        credJumpNo: 'not signed in \u2014 device collection will return nothing',
        credJumpLabel: 'JumpServer password',
        credJumpHint: 'Exchanged for a session token. Without \u201cremember\u201d it is held in memory only and never written to disk.',
        credRemember: 'Remember this password (sealed to this Windows account)',
        credGithubUnknown: 'cannot be detected \u2014 the dashboard reads it from its own environment',
        credGithubHint: 'Signing in with the GitHub CLI also works and takes precedence over the token. Without either, PR and CI columns may read \u201cunknown\u201d and anonymous requests are limited to 60 per hour.',
        credGithubLabel: 'GitHub token',
        credGithubFieldHint: 'Stored by the Harness credential service, not by this plugin and not by the dashboard. It reaches collection through the dashboard\u2019s process environment, never over HTTP, because that server writes every request line to a plaintext log. A fine-grained token with read access to the two plugin repositories is enough.',
        credGithubAmbientHint: 'A token is already set in the environment. You can still save one here \u2014 a stored token takes precedence over the environment \u2014 or leave this empty to keep using the environment one.',
        credGithubDone: 'GitHub token stored',
        credGithubStored: 'token stored in the Harness credential store',
        credGithubAmbient: 'token supplied by the environment',
        credGithubNone: 'no token \u2014 anonymous reads are limited to 60 per hour',
        credGithubRestart: 'Saved. Restart the dashboard to let collection use the new token: a running process keeps the environment it started with.',
        credGithubCli: 'signed in with the GitHub CLI',
        credGithubCliAndToken: 'a stored token is the fallback',
        credGithubCliDone: 'GitHub CLI signed in \u2014 no token needed',
        credGithubAddToken: 'Add a token anyway',
        credSshHint: 'SSH private keys are deliberately never entered here. The dashboard references key files by path and never reads their contents, so accepting key material would break that invariant and give this plugin a secret it has no way to protect. Point the machine profile at an existing key file instead.',
        credSafety: 'Note that the dashboard has no authentication of its own and is reachable by any process on this machine. Only enter credentials on a machine you trust.',
      },
      zh: {
        title: 'FL 适配',
        heading: 'FL 适配工作台',
        start: '启动',
        stop: '停止',
        reload: '刷新',
        restart: '重启看板',
        restarting: '重启中…',
        codeStale: '代码已更新，需要重启',
        codeStaleHint: (count, from, to) => `看板仍在运行启动时的代码。${to && from && to !== from ? `HEAD 已从 ${from} 变为 ${to}。` : ''}${count ? `启动后有 ${count} 个源文件改动。` : ''}重启后生效。`,
        codeUnknown: '该看板版本较旧，无法报告代码版本；请在启动它的地方手动重启一次。',
        prCandidates: '没有自动匹配到 PR。本分支上提到该后端的开放 PR：',
        noPrCandidates: '本分支上没有找到适配 PR。',
        pin: '固定',
        unpin: '取消固定',
        pinning: '保存中…',
        pinned: '已固定',
        pinnedHint: '在本分支上手动指定，优先于配置和自动匹配。',
        unpinHint: '恢复为配置或自动匹配的 PR。',
        draft: '草稿',
        gateStale: commit => `已有门禁证据，但属于旧提交 ${commit}；门禁只认当前分支 HEAD 上的运行。`,
        gateMissing: '当前分支 HEAD 上还没有上传过门禁证据。',
        running: '运行中',
        stopped: '已停止',
        starting: '启动中',
        stopping: '停止中',
        cloning: '正在拉取看板…',
        notConfigured: '尚未配置',
        openDashboard: '打开看板',
        adopted: '外部',
        adoptedHint: '该看板不是由插件启动的，这里只读取状态，不做进程管理。',
        collecting: '采集中',
        collectStale: '采集异常',
        collectWaiting: '暂无数据',
        collectOkHint: '采集正常。最近一次成功：',
        collectStaleHint: '采集失败。最近错误：',
        collectWaitingHint: '采集器还没有返回过数据。',
        collectNeverOk: '尚无成功记录',
        collectDevices: '设备',
        collectBranches: '分支',
        collectTasks: '任务',
        updateAvailable: '更新到',
        updateInstalling: '更新中',
        updateCurrent: '已是最新',
        updateCheck: '检查更新',
        updateDone: '已更新到',
        updateRestart: '重新加载窗口即可运行新版本。',
        updateLinkedHint: '当前插件是从本地检出加载的，不能用发布包覆盖。请用 git 更新。',
        updateUnsupportedHint: '当前 Harness 构建不支持从面板安装插件。',
        updateCheckFailed: '检查更新失败',
        updateRateLimited: 'GitHub 限流了更新检查，稍后再试。',
        updateNotes: '更新说明',
        updateInstalledIs: '当前版本',
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
        ciLatestRun: '分支 HEAD 上的最新运行',
        acceptance: '验收',
        gate: '门禁',
        noBackends: '暂无后端状态。',
        noDevices: '暂无设备数据。',
        noTasks: '暂无任务记录。',
        devicesRailSub: '所有项目共用',
        statFreeHosts: '空闲机器',
        statFreeCards: '空闲卡',
        statUnknown: '读不到状态的卡',
        statStale: '数据已过期的机器',
        allBackends: '全部硬件',
        cjkNames: true,
        devicesNote: '设备不属于某个项目或某个分支：所有项目用的是同一批机器，所以这个视图没有分支选择。',
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
        deployRailSetup: '从这里开始：还没有配置',
        deployRailCreds: '已在运行，但还缺密钥',
        deployRailDone: '部署与凭据',
        deployIntro: '本插件读取 fl-plugin-upgrade-workflow 检出中的本地 Python 看板。可在此完成配置并启动。',
        deployRedoIntro: '部署已完成。只有要把看板指向别的检出或分支时才需要再跑一次。',
        deployNeedRepo: '插件需要一份 fl-plugin-upgrade-workflow 的本地检出，现在还没有配置。',
        deployWhereConfig: '先 clone 下来，再把 repoPath 填成它的绝对路径：插件 → FL 适配工作台 → 配置。填完重新打开本面板。',
        deployNeedScript: '配置的目录不是 fl-plugin-upgrade-workflow 的检出：里面没有 scripts/configure.py。',
        deployFoundDir: '当前配置的目录',
        deployPickProjects: '选择需要跟踪的项目。已识别到本地检出的会自动使用；不在磁盘上的需要手动填写路径。',
        deployDetected: '已识别',
        deployNotDetected: '未找到本地检出',
        deployUsingDetected: '使用已识别的检出',
        deployBranchPlaceholder: '选择分支',
        deployHint: '需要 Python 3.11+ 和 Git。',
        deployRepoPath: '插件检出路径',
        deployRemote: 'Remote',
        deployBranch: '目标分支',
        deployRun: '配置并启动',
        deployRunning: '配置中…',
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
        workspaceSection: '代码',
        workspaceStale: '这个任务的代码之前加到过左侧，但目录已经不存在了 —— 任务结束后看板会清理 worktree。左侧那一项现在打不开任何东西。',
        workspaceStaleTag: '目录已不存在',
        workspaceForget: '移除失效的条目',
        workspaceForgetHint: '只移除左侧的注册记录。目录本来就已经不存在，会话历史也会保留。',
        workspaceForgetting: '移除中',
        taskDesktopTag: '桌面端',
        taskObserverTag: '只读',
        repoTitle: '项目仓库',
        repoUnknown: '看板配置里没有记录这个项目的仓库，所以无法自动识别。',
        repoMissing: '配置里的仓库目录还不在本地。',
        repoCloneHint: '会克隆到看板已为该项目的配置指定的路径，源是它的 GitHub 仓库配置。',
        repoClone: '立即克隆',
        repoCloning: '克隆中',
        repoCloneFailed: '克隆失败',
        repoAdopted: '已在左侧列表中。',
        repoNotAdopted: '已在本地识别到，但还没加入左侧列表。',
        repoAdopt: '加入左侧列表',
        repoStartSession: '在这里开始对话',
        repoSessionHint: '会在这个工作区里新建一个对话，上下文已经指向该仓库和它跟踪的分支。',
        repoNoSession: '这个工作区暂时还不能新建对话',
        repoTrackedBranch: '所跟踪的分支',
        repoSessionPrompt: '我在做 {project}，跟踪的是 {branch} 分支。这个工作区就是它的代码仓库。请先看一下仓库结构，然后告诉我这个分支目前是什么状态。',
        taskScope: '显示',
        taskScopeMine: '本插件创建的',
        taskScopeAll: '看板里的全部',
        noTasksMine: '还没有从这个面板创建过任务。可以在上面新建一个，或者切到“看板里的全部”查看已有的任务。',
        createOpen: '新建任务',
        createTitle: '新建适配任务',
        createClose: '关闭',
        createIntro: '只做准备，不会启动。启动会消耗模型额度并在远端机器上执行命令，所以启动这一步保留在看板里。',
        createMachine: '机器',
        createPickMachine: '选择机器',
        createNoBackend: '后端未知',
        createBranch: '分支',
        createProvider: '智能体',
        createGoal: '目标（可选）',
        createGoalHint: '这次适配要达成什么。留空则使用看板的默认目标。',
        createCost: '准备任务会在看板仓库里新增一个 git worktree，并占用这台机器，在任务被删除之前其他项目都无法使用它。',
        createRun: '准备任务',
        createRunning: '准备中',
        createDone: '已准备',
        createNext: '已准备，尚未启动。可以在下方打开查看，然后在看板里启动。',
        createFailed: '看板拒绝创建这个任务',
        createNeedsWorkspace: '这台机器没有记录远端工作目录，而准备任务需要它。在下面填一个即可。',
        createWorkspaceLabel: '远端工作目录',
        createWorkspaceHint: '例如 /workspace/adapt',
        createWorkspaceSave: '保存工作目录',
        createWorkspaceSaving: '保存中',
        createWorkspaceFailed: '看板拒绝了这个路径',
        createWorkspaceWhy: '远端 GPU 机器上任务检出并运行的绝对路径。保存在机器配置上并复用；JumpServer 刷新也会保留。',
        createNoWorkspaces: '当前部署里还没有任何机器记录远端工作目录。在上面选一台机器填一个即可 —— 只要有一台填了就能准备任务。',
        createMonitorOnly: '这个后端在该分支上是只监控状态：已经有适配 PR，因此不再接受新任务。',
        createPairTaken: '这个后端在该分支上已经有任务了。同一时间只允许一个，请先删除已有的那个。',
        createBackendGuess: '这台机器的后端是按名称推断的，不是实际探测结果，可能不准。',
        credTitle: '凭据',
        credIntro: '配置那一步只写仓库路径和分支，所以凭据在这里单独收集。填入的值都会直接进入加密存储 —— 看板自己的提供方存在看板那边，GitHub token 存在 Harness 凭据服务里 —— 插件不保留、不回显，也不写入浏览器。',
        credPlaceholder: '粘贴内容',
        credSave: '保存',
        credSaving: '保存中',
        credSaved: '已保存',
        credFailed: '看板拒绝了这个值',
        credReady: '就绪',
        credUnavailable: '不可用',
        credCodexOk: '已通过自己的 CLI 登录，这里不需要填',
        credCodexNo: '未登录 —— 请用 Codex CLI 登录',
        credClaudeOk: '密钥已配置',
        credClaudeNo: '未配置密钥',
        credClaudeDone: 'Claude 密钥已配置并通过验证',
        credJumpDone: 'JumpServer 已登录',
        credReplace: '更换',
        credCancel: '取消',
        credClaudeLabel: 'Claude API 密钥',
        credClaudeHint: '由看板保存，用 Windows DPAPI 以当前用户身份加密，存放在已被 git 忽略的目录里。读回时只有“是否已配置”。',
        credJumpOk: '已登录，资产可用',
        credJumpNo: '未登录 —— 设备采集会拿不到任何数据',
        credJumpLabel: 'JumpServer 密码',
        credJumpHint: '用于换取会话令牌。不勾选“记住”时只存在内存里，不落盘。',
        credRemember: '记住这个密码（加密绑定到当前 Windows 账户）',
        credGithubUnknown: '无法探测 —— 看板从自己的环境变量里读',
        credGithubHint: '也可以用 GitHub CLI 登录，它的优先级高于 token。两者都没有的话，PR 和 CI 列可能显示 unknown，匿名请求限额是每小时 60 次。',
        credGithubLabel: 'GitHub token',
        credGithubFieldHint: '由 Harness 的凭据服务加密保存，不经过本插件也不经过看板。它通过看板进程的环境变量传入，不走 HTTP —— 因为那个服务器会把每个请求行写进明文日志。一个对两个插件仓库有读权限的细粒度 token 就够了。',
        credGithubAmbientHint: '环境里已经有一个 token。你仍然可以在这里存一个 —— 存进来的会覆盖环境里的 —— 或者留空继续用环境里的那个。',
        credGithubDone: 'GitHub token 已保存',
        credGithubStored: 'token 已存入 Harness 凭据库',
        credGithubAmbient: 'token 由环境提供',
        credGithubNone: '未配置 token —— 匿名读取限额每小时 60 次',
        credGithubRestart: '已保存。重启看板后采集才会用上新 token：运行中的进程保留着启动时的环境变量。',
        credGithubCli: '已通过 GitHub CLI 登录',
        credGithubCliAndToken: '另存有 token 作后备',
        credGithubCliDone: '已通过 GitHub CLI 登录 —— 无需 token',
        credGithubAddToken: '仍然添加 token',
        credSshHint: 'SSH 私钥刻意不在这里输入。看板只按路径引用密钥文件，从不读取内容；接收私钥内容会破坏这个不变量，并让插件持有一个它无法妥善保护的密钥。请在机器配置里指向一个已存在的密钥文件。',
        credSafety: '请注意看板自身没有任何认证，本机上任何进程都能访问它。只在你信任的机器上填写凭据。',
      },
    }

    /** Carries the plugin context to the panel tree without module-level state. */
    const PluginContext = React.createContext(null)

    /*
     * Last-known view data, surviving the remount that switching views causes.
     *
     * Each view in the pane carries a React key so its slide-in replays on
     * every switch. That remount is deliberate, but it also discards the
     * component's state, so returning to a view showed a skeleton and
     * re-fetched from scratch — the flicker on every rail click, with the old
     * data still perfectly good.
     *
     * Holding the data outside the component tree separates the two: the
     * animation still replays, while the view renders its previous content
     * immediately and refreshes behind it. The entries are plain summarized
     * payloads, the same shapes already sent to this browser, so nothing here
     * is sensitive beyond what the panel is displaying.
     *
     * Module scope rather than a ref: the panel itself unmounts when the
     * sidebar closes, and reopening it should not start cold either.
     */
    const VIEW_CACHE = new Map()

    /** Drop the cache for one view, or all of it when no key is given. */
    function dropCache(key) {
      if (key === undefined) VIEW_CACHE.clear()
      else VIEW_CACHE.delete(key)
    }

    /**
     * `useState`, but seeded from and written through to VIEW_CACHE.
     *
     * `key` identifies the view instance (`devices`, `project:vllm`) and
     * `field` the datum within it, so two projects keep separate caches and a
     * project's branch list does not overwrite its task list.
     *
     * The initial value is used only when nothing is cached; a cached entry
     * wins so the first render after a remount already has content. Writes go
     * to the cache before the state update so a remount that happens before
     * React re-renders still observes the newest value.
     */
    function useCached(key, field, initial) {
      const slot = `${key}\u0000${field}`
      const [value, setValue] = React.useState(() => (
        VIEW_CACHE.has(slot) ? VIEW_CACHE.get(slot) : initial
      ))
      const update = React.useCallback(next => {
        setValue(previous => {
          const resolved = typeof next === 'function' ? next(previous) : next
          VIEW_CACHE.set(slot, resolved)
          return resolved
        })
      }, [slot])
      return [value, update]
    }

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

    /**
     * Give a conversation a readable title.
     *
     * A session is named by holding a reference to it and calling `rename`.
     * The reference must be released, so `using` does that even if the rename
     * throws. `source` has to be one of the known reference sources; `gateway`
     * is the one that fits a UI-initiated operation.
     *
     * Naming is best-effort: it is cosmetic, and the conversation itself is
     * already usable. A failure must not stop the session from opening, so it
     * is swallowed rather than surfaced as an error the user cannot act on.
     */
    async function renameSession(ctx, sessionId, title) {
      const sessions = ctx && ctx.get('sessions')
      if (!sessions || !sessionId || !title) return false
      try {
        return await sessions.using(sessionId, { source: 'gateway' }, async reference => {
          const binding = await reference.ready
          const result = await binding.session.rename(String(title).slice(0, 200))
          // `rename` reports refusal in its result rather than by throwing.
          return Boolean(result && result.ok !== false)
        })
      } catch {
        return false
      }
    }

    /**
     * Shorten a bilingual vendor name to the half that matches the dictionary.
     *
     * The bastion labels its hardware "NVIDIA / 英伟达", which is helpful in a
     * table cell and far too wide for a filter chip: thirteen of them wrapped
     * to six lines at a narrow panel width. Showing one half halves the row.
     *
     * Only an exact "latin / CJK" shape is split. Anything else is returned
     * untouched, so a vendor that adds a third name or drops the separator
     * keeps its full label rather than being silently truncated.
     */
    function shortBackend(name, t) {
      const text = String(name || '')
      const parts = text.split('/').map(part => part.trim()).filter(Boolean)
      if (parts.length !== 2) return text
      const cjk = parts.findIndex(part => /[\u4e00-\u9fff]/.test(part))
      if (cjk < 0) return text
      return t.cjkNames ? parts[cjk] : parts[1 - cjk]
    }

    /** How many hue steps the stylesheet declares. */
    const HUE_COUNT = 12

    /**
     * Assign each name its own hue.
     *
     * Hashing the name was the first attempt and was wrong here: against the
     * real fleet of twelve vendors it used seven hues and put NVIDIA, Hygon
     * and AMD on one colour. A hash spreads a large unknown key space; this
     * key space is small, known, and entirely visible at once, where what
     * matters is that neighbours differ.
     *
     * So hues go by position in a name-sorted copy of the set, which
     * guarantees distinctness up to twelve. Sorting by NAME, not by the host
     * count the fleet is ordered by, is the point: a vendor acquiring one
     * machine changes its row position, and a colour that moves when nothing
     * meaningful changed is worse than no colour. Adding a vendor can shift
     * the assignment, which is rare and visible, and the labels carry the
     * meaning regardless.
     *
     * Past twelve, names wrap and two vendors share a hue. The fleet has
     * twelve; this keeps a thirteenth from being colourless.
     */
    function huePalette(names) {
      const unique = [...new Set((names || []).map(name => String(name || '')).filter(Boolean))]
      unique.sort()
      const palette = new Map()
      unique.forEach((name, index) => {
        palette.set(name, `var(--flup-hue-${(index % HUE_COUNT) + 1})`)
      })
      return palette
    }

    const CSS = `
.flup-root {
  display: flex; flex-direction: column; height: 100%; min-height: 0;
  /* Declared below in this same rule; custom properties resolve at use, not
     in source order, so the forward reference is fine. */
  background: var(--flup-surface); color: var(--dsw-alias-label-primary);
  font-size: 13px; line-height: 1.5;

  /*
   * The panel's palette is derived from theme tokens rather than invented.
   *
   * Every theme colour is declared requiresLightAndDark, so the
   * theme is what keeps text legible when the user switches. A fixed hex hue
   * would stay put while everything around it moved, and be wrong in one of
   * the two themes. So richer surfaces come from color-mix() against the
   * theme's own background: the hue still belongs to the theme, only the
   * amount is ours. The --flup-on-brand value is the single exception, and
   * precisely because the brand fill underneath it does not change.
   */
  --flup-on-brand: #fff;

  /*
   * The panel's own accent, in three strengths.
   *
   * --dsw-alias-brand-primary is not an accent colour despite its name: it
   * resolves to #0f1115 in light and #f9fafb in dark, a near-black and a
   * near-white. As a fill or a border it gives black buttons and black focus
   * rings, which read as deliberate while the panel was greyscale and read as
   * foreign next to the muted vendor plates. Twenty-six declarations used it.
   *
   * The accent is the plate family's own hue: the theme's success colour held
   * at the plates' 35% saturation, so a button, a focus ring and a vendor
   * plate are visibly one palette. Lightness comes from the theme, so it
   * follows a theme switch.
   *
   * --flup-ink is the readable strength, for text and thin borders. It is
   * not simply the accent at higher saturation — that was the obvious attempt
   * and it fails, because no single colour clears 4.5:1 against both a white
   * page and a near-black one; the requirement runs in opposite directions,
   * and the theme only solves it for its own tokens by shipping two values.
   * Mixing the accent into the theme's label colour does solve it, since
   * label-primary is already near-black in light and near-white in dark, so
   * the ink darkens or lightens to suit without naming either case. At 75%
   * accent it stays clearly coloured and still reads 5.0:1 on the worst of
   * the four surfaces.
   *
   * --flup-fill is for a filled button, whose label is white in both
   * themes. The accent is too light for that at 3.4:1, so the fill raises
   * saturation and drops lightness until white clears AA, at 4.8:1.
   */
  --flup-accent: hsl(from var(--dsw-alias-state-success-primary) h 35% l);
  --flup-ink: color-mix(in srgb, var(--flup-accent) 75%, var(--dsw-alias-label-primary));
  --flup-fill: hsl(from var(--dsw-alias-state-success-primary) h 50% 34%);

  /*
   * Surfaces carry a trace of the same hue, so the panel reads as one warm
   * family rather than coloured chips floating on neutral grey.
   *
   * The amount falls as a surface rises, which is what makes layering work at
   * all in the light theme: bg-base, bg-layer-1 and bg-layer-2 all resolve to
   * #fff there, so a card had no fill difference from the page behind it and
   * depended entirely on its border. 7/3/1 gives a card an 18-step lift in
   * light and 29 in dark, while the least contrasted text still reads 17.6:1
   * and 13.2:1 — far too faint to threaten legibility, and much quieter than
   * the 40% vendor plate, which stays the quietest coloured thing that
   * carries information.
   */
  --flup-surface: color-mix(in srgb, var(--flup-accent) 7%, var(--dsw-alias-bg-base));
  --flup-surface-1: color-mix(in srgb, var(--flup-accent) 3%, var(--dsw-alias-bg-layer-1));
  --flup-surface-2: color-mix(in srgb, var(--flup-accent) 1%, var(--dsw-alias-bg-layer-2));

  --flup-wash-brand: color-mix(in srgb, var(--flup-accent) 14%, var(--dsw-alias-bg-layer-1));
  --flup-wash-ok: color-mix(in srgb, var(--dsw-alias-state-success-primary) 10%, var(--dsw-alias-bg-layer-1));
  --flup-wash-warn: color-mix(in srgb, var(--dsw-alias-state-warn-primary) 10%, var(--dsw-alias-bg-layer-1));
  --flup-wash-error: color-mix(in srgb, var(--dsw-alias-state-error-primary) 10%, var(--dsw-alias-bg-layer-1));
  --flup-edge-brand: color-mix(in srgb, var(--flup-accent) 55%, transparent);
  --flup-ease: cubic-bezier(0.22, 0.61, 0.36, 1);

  /*
   * A hue wheel rotated off the theme's success colour.
   *
   * The theme offers five meaningful colours and four already mean something
   * (error, warning, success, idle), which left one hue for everything else
   * and made a fleet of twelve vendors render as twelve identical grey chips.
   * Fixed hexes would fix the sameness and break theming: a literal stays put
   * when the user switches light and dark, and is wrong in one of them.
   *
   * Relative color syntax avoids that, but only if the base colour actually
   * has a hue to rotate. The first attempt here used brand-primary, which
   * resolves to #0f1115 in light and #f9fafb in dark: a near-black and a
   * near-white, 17% and 20% saturated. Rotating the hue of an almost
   * achromatic colour returns almost the same colour, so all twelve steps came
   * out visually identical — the closest pair was 2 of 765 possible channel
   * distance apart in light, and 0 in dark. The wheel was real in the CSS and
   * invisible on screen.
   *
   * success-primary is #22c55e: the most restrained of the three saturated
   * tokens the theme has, and its lightness reads on both backgrounds.
   *
   * Saturation is pinned to 35% rather than inherited. Inherited it is 71%,
   * which is poster paint once twelve of them sit in one row; 35% is the khaki
   * register, since muted earth tones are defined by low saturation rather
   * than by one particular hue. Lightness still comes from the theme, so the
   * wheel still follows a theme change, and the hue is still the theme's own.
   *
   * The angles are an even 30 degrees apart, which is the answer a search
   * gave rather than the one I guessed. I first hand-picked uneven angles on
   * the theory that equal steps bunch up in the blue-green region; measuring
   * the result put the closest pair 43 apart out of a possible 765, while
   * plain even spacing puts it at 81. sRGB is not perceptually uniform, but
   * not unevenly enough to beat the symmetry by hand.
   *
   * No color-mix fallback precedes these any more. It was pointless: mixing
   * two tokens always produces a valid colour, so the fallback always parsed
   * and therefore always won in exactly the renderers that could not do the
   * real thing, hiding the problem instead of solving it. Chromium here is
   * 152; relative color has shipped since 119.
   */
  --flup-hue-1: hsl(from var(--dsw-alias-state-success-primary) calc(h + 0) 35% l);
  --flup-hue-2: hsl(from var(--dsw-alias-state-success-primary) calc(h + 30) 35% l);
  --flup-hue-3: hsl(from var(--dsw-alias-state-success-primary) calc(h + 60) 35% l);
  --flup-hue-4: hsl(from var(--dsw-alias-state-success-primary) calc(h + 90) 35% l);
  --flup-hue-5: hsl(from var(--dsw-alias-state-success-primary) calc(h + 120) 35% l);
  --flup-hue-6: hsl(from var(--dsw-alias-state-success-primary) calc(h + 150) 35% l);
  --flup-hue-7: hsl(from var(--dsw-alias-state-success-primary) calc(h + 180) 35% l);
  --flup-hue-8: hsl(from var(--dsw-alias-state-success-primary) calc(h + 210) 35% l);
  --flup-hue-9: hsl(from var(--dsw-alias-state-success-primary) calc(h + 240) 35% l);
  --flup-hue-10: hsl(from var(--dsw-alias-state-success-primary) calc(h + 270) 35% l);
  --flup-hue-11: hsl(from var(--dsw-alias-state-success-primary) calc(h + 300) 35% l);
  --flup-hue-12: hsl(from var(--dsw-alias-state-success-primary) calc(h + 330) 35% l);

  /*
   * One spacing scale, and one radius scale.
   *
   * These replace a long tail of ad-hoc values: eighteen distinct spacings
   * under 40px and seven radii, most of them written inline, a few pixels
   * apart and chosen one element at a time. Nothing lined up with anything
   * else, which is what made the panel feel untidy without any single thing
   * being wrong. Four steps and three radii cover every case here, and being
   * few is the point — a scale no one can hold in their head gets ignored.
   */
  --flup-s1: 4px;
  --flup-s2: 8px;
  --flup-s3: 12px;
  --flup-s4: 20px;
  --flup-r1: 5px;
  --flup-r2: 8px;
  --flup-r3: 999px;
}
/*
 * Vertical rhythm as classes, so markup stops carrying inline margins.
 *
 * An inline style silently outranks every rule in this stylesheet, so each one
 * was a small hole in the design system; these close them while keeping the
 * markup readable about its own spacing.
 */
.flup-mt2 { margin-top: var(--flup-s2); }
.flup-mt3 { margin-top: var(--flup-s3); }
.flup-mb2 { margin-bottom: var(--flup-s2); }
.flup-pb3 { padding-bottom: var(--flup-s3); }
.flup-flat { margin: 0; }
.flup-row { margin: var(--flup-s1) 0; }
/* A label and its value on one line, used wherever a row needs a leading dot. */
.flup-line { display: flex; align-items: center; gap: var(--flup-s2); }
.flup-inline-note { margin-left: var(--flup-s1); font-size: 11px; }
.flup-muted { color: var(--dsw-alias-label-secondary); }
.flup-pr-candidates { display: flex; flex-direction: column; gap: var(--flup-s1); margin-top: var(--flup-s1); font-size: 11px; }
.flup-pr-candidate { display: flex; align-items: center; gap: var(--flup-s1); min-width: 0; }
.flup-pr-title { flex: 1; min-width: 0; max-width: 320px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--dsw-alias-label-secondary); }
/* Fields that belong to the checkbox above them, indented to say so. */
.flup-nest { margin-left: var(--flup-s4); }
.flup-rail-empty { padding: 0 var(--flup-s3); }
/*
 * A cell that spaces its own tags.
 *
 * Each tag used to carry its own left margin, so adding one meant remembering
 * the number; a gap here cannot drift. Baseline alignment keeps the tags on
 * the text's own line rather than floating against the cell's box.
 */
.flup-tags { display: flex; align-items: baseline; gap: var(--flup-s1); flex-wrap: wrap; }

/*
 * Motion is used for three things only: to show that something is genuinely
 * live, to fill the wait before data arrives, and to let a new row settle in
 * rather than appear from nothing. Anything purely decorative stays still.
 */
@keyframes flup-pulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.45; transform: scale(0.82); }
}
@keyframes flup-halo {
  0% { box-shadow: 0 0 0 0 currentColor; opacity: 0.55; }
  70% { box-shadow: 0 0 0 6px currentColor; opacity: 0; }
  100% { box-shadow: 0 0 0 0 currentColor; opacity: 0; }
}
@keyframes flup-spin { to { transform: rotate(360deg); } }
@keyframes flup-rise {
  from { opacity: 0; transform: translateY(4px); }
  to { opacity: 1; transform: none; }
}
@keyframes flup-sweep {
  from { background-position: 200% 0; }
  to { background-position: -200% 0; }
}
@keyframes flup-grow {
  from { transform: scaleX(0); }
}
/*
 * A dashboard that animates is a dashboard someone has to sit in front of.
 * Honour the OS setting: drop every animation and transition, and leave the
 * static styling to carry the same information. Nothing below encodes meaning
 * in motion alone, so this removes polish, not content.
 */
@media (prefers-reduced-motion: reduce) {
  .flup-root *, .flup-root *::before, .flup-root *::after {
    animation-duration: 0.001ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.001ms !important;
  }
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
  border: 1px solid var(--dsw-alias-border-l2); border-radius: var(--flup-r2);
  background: var(--flup-surface-1); color: var(--dsw-alias-label-primary);
  padding: 7px 10px 7px 9px; border-left-width: 3px;
  display: flex; flex-direction: column; gap: 1px; width: 100%;
  transition: background 140ms var(--flup-ease), border-color 140ms var(--flup-ease), transform 140ms var(--flup-ease);
}
.flup-rail-btn:hover { background: var(--flup-surface-2); transform: translateX(1px); }
.flup-rail-btn:active { transform: translateX(1px) scale(0.99); }
.flup-rail-btn[aria-current="true"] {
  background: var(--flup-surface-2); background: var(--flup-wash-brand);
  font-weight: 500; border-color: var(--flup-ink);
}
.flup-rail-btn:focus-visible { outline: 2px solid var(--flup-ink); outline-offset: 1px; }
/*
 * The deploy entry sits apart from the project list, because it acts on the
 * dashboard itself rather than on a project. It carries the brand fill while
 * nothing is configured, since the panel is empty until it is used, and falls
 * back to an outline once a dashboard exists and it is merely available.
 */
.flup-rail-cta {
  border-color: var(--flup-ink);
  color: var(--flup-ink);
  font-weight: 500;
}
.flup-rail-cta-urgent {
  background: var(--flup-fill);
  border-color: var(--flup-ink);
  color: var(--flup-on-brand);
  font-weight: 500;
}
.flup-rail-cta-urgent:hover { background: var(--flup-fill); filter: brightness(1.08); }
.flup-rail-cta-urgent .flup-rail-sub { color: var(--flup-on-brand); opacity: 0.85; }
.flup-rail-sub { font-size: 11px; color: var(--dsw-alias-label-secondary); font-weight: 400; }
/*
 * Separates the project entries from the fleet entry below them. The two
 * groups answer different questions — one project on one branch, versus the
 * machines shared by all of them — and a rule is what says they are not
 * siblings in the same list.
 */
.flup-rail-divider {
  display: block; height: 1px; margin: 8px 10px 4px;
  background: var(--dsw-alias-border-l1);
}
/*
 * The way back to a finished setup screen.
 *
 * Quieter than a rail entry on purpose: setup is done, so this is a door
 * rather than a destination. It replaces the entry in the same bottom
 * position, which keeps the rail from reflowing when setup completes.
 */
.flup-rail-link {
  appearance: none; background: none; border: 0; cursor: pointer;
  text-align: left; padding: 8px 10px; font: inherit; font-size: 11px;
  color: var(--dsw-alias-label-secondary); border-radius: var(--flup-r2);
}
.flup-rail-link:hover { color: var(--dsw-alias-label-primary); background: var(--flup-surface-2); }
.flup-rail-link:focus-visible { outline: 2px solid var(--flup-ink); outline-offset: 1px; }
.flup-pane { flex: 1 1 auto; min-width: 0; display: flex; flex-direction: column; min-height: 0; }
.flup-sub {
  display: flex; align-items: center; gap: 8px 14px; flex-wrap: wrap;
  padding: 8px 20px; color: var(--dsw-alias-label-secondary); font-size: 12px;
  border-bottom: 1px solid var(--dsw-alias-border-l1);
}
.flup-sub b { color: var(--dsw-alias-label-primary); font-weight: 500; }
.flup-btn {
  appearance: none; border: 1px solid var(--dsw-alias-border-l2);
  background: var(--flup-surface-1); color: var(--dsw-alias-label-primary);
  border-radius: var(--flup-r2); padding: 4px 11px; font: inherit; font-size: 12px; cursor: pointer;
  text-decoration: none; white-space: nowrap;
  transition: background 130ms var(--flup-ease), border-color 130ms var(--flup-ease),
    transform 130ms var(--flup-ease), filter 130ms var(--flup-ease);
}
.flup-btn:hover:not(:disabled) {
  background: var(--flup-surface-2); border-color: var(--flup-ink);
  transform: translateY(-1px);
}
.flup-btn:active:not(:disabled) { transform: translateY(0) scale(0.98); }
.flup-btn:disabled { opacity: 0.5; cursor: default; }
.flup-btn:focus-visible { outline: 2px solid var(--flup-ink); outline-offset: 1px; }
/*
 * Button weight carries meaning, so the levels stay visually distinct:
 * primary is the filled action a view exists for, accent is an outlined action
 * that reaches outside this machine (a git fetch, an SSH round), and the plain
 * button is local navigation. Danger is reserved for removing something.
 */
.flup-btn-primary {
  border-color: transparent; background: var(--flup-fill);
  color: var(--flup-on-brand); font-weight: 500;
}
.flup-btn-primary:hover:not(:disabled) { filter: brightness(1.08); background: var(--flup-fill); }
.flup-btn-accent {
  border-color: var(--flup-ink); color: var(--flup-ink);
  background: var(--flup-surface-1);
}
.flup-btn-accent:hover:not(:disabled) { background: var(--flup-surface-2); }
.flup-btn-danger { border-color: var(--dsw-alias-state-error-primary); color: var(--dsw-alias-state-error-primary); }
.flup-btn-danger:hover:not(:disabled) {
  background: var(--flup-surface-2); border-color: var(--dsw-alias-state-error-primary);
}
.flup-select, .flup-input {
  font: inherit; font-size: 12px; border-radius: var(--flup-r2); padding: 4px 8px;
  border: 1px solid var(--dsw-alias-border-l2); background: var(--flup-surface-1);
  color: var(--dsw-alias-label-primary); max-width: 100%;
}
.flup-select:focus-visible, .flup-input:focus-visible {
  outline: 2px solid var(--flup-ink); outline-offset: 1px;
}
.flup-input { width: 100%; }
.flup-dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%; flex: none; }
/*
 * Only a genuinely in-flight status pulses. That makes motion mean one thing
 * in this panel — work is happening right now — so a glance at a long task
 * list finds the live rows without reading any text. Settled states stay
 * still, including the failed ones, which do not need to attract the eye
 * forever. The halo is a separate element so the dot's own colour stays flat.
 */
.flup-dot-live { position: relative; animation: flup-pulse 1.6s var(--flup-ease) infinite; }
.flup-dot-live::after {
  content: ''; position: absolute; inset: 0; border-radius: 50%;
  color: inherit; animation: flup-halo 1.6s var(--flup-ease) infinite;
}
.flup-state { display: inline-flex; align-items: center; gap: 8px; font-size: 12px; color: var(--dsw-alias-label-secondary); }
/*
 * The collection indicator, set apart from the process indicator beside it.
 *
 * Two bare dot-and-word pairs in a row read as one broken sentence, so this
 * one is enclosed: the border makes it a distinct claim about a distinct
 * subject. The help cursor advertises the tooltip, which carries the
 * timestamp and the error that the badge itself has no room for.
 */
.flup-collect {
  padding: 3px 9px; border-radius: var(--flup-r3); cursor: help;
  border: 1px solid var(--dsw-alias-border-l2);
}
.flup-tabs { display: flex; gap: 8px; padding: 10px 20px 0; }
.flup-tab {
  appearance: none; font: inherit; font-size: 12px; cursor: pointer;
  border: 1px solid var(--dsw-alias-border-l2); border-radius: var(--flup-r2);
  background: var(--flup-surface-1); color: var(--dsw-alias-label-primary);
  padding: 5px 12px; position: relative; overflow: hidden;
  transition: background 130ms var(--flup-ease), border-color 130ms var(--flup-ease);
}
.flup-tab:hover { background: var(--flup-surface-2); }
.flup-tab[aria-selected="true"] {
  background: var(--flup-surface-2); background: var(--flup-wash-brand);
  font-weight: 500;
  border-color: var(--flup-ink); box-shadow: inset 0 -2px 0 var(--flup-accent);
}
/* The underline grows from the left on selection, so the eye is carried from
   the tab that was active to the one that now is. */
.flup-tab[aria-selected="true"]::after {
  content: ''; position: absolute; left: 0; right: 0; bottom: 0; height: 2px;
  background: var(--flup-fill); transform-origin: left;
  animation: flup-grow 220ms var(--flup-ease);
}
.flup-tab:focus-visible { outline: 2px solid var(--flup-ink); outline-offset: 1px; }
/*
 * Switching rail entries slides the new view in.
 *
 * Without it the pane swaps instantly and two screens are hard to tell apart
 * at a glance, especially between projects whose layouts match. The movement
 * is small and horizontal, which reads as "a different thing in the same
 * place" rather than as a page load. The body is keyed on the selected entry
 * in the markup, so React remounts it and the animation re-runs per switch.
 */
@keyframes flup-slide {
  from { opacity: 0; transform: translateX(6px); }
  to { opacity: 1; transform: none; }
}
.flup-body {
  flex: 1 1 auto; min-height: 0; overflow: auto;
  padding: var(--flup-s3) var(--flup-s4) 28px;
  animation: flup-slide 200ms var(--flup-ease) both;
}
.flup-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.flup-table th {
  text-align: left; font-weight: 500; color: var(--dsw-alias-label-secondary);
  padding: 0 10px 6px 0; border-bottom: 1px solid var(--dsw-alias-border-l1); font-size: 11px;
  position: sticky; top: 0; background: var(--flup-surface);
}
.flup-table td { padding: 7px 10px 7px 0; border-bottom: 1px solid var(--dsw-alias-border-l1); }
.flup-table tr:last-child td { border-bottom: none; }
.flup-table td:first-child { color: var(--dsw-alias-label-primary); }
.flup-table-rows tbody tr { transition: background 110ms var(--flup-ease), box-shadow 110ms var(--flup-ease); }
/*
 * Rows fade in as they arrive.
 *
 * Tables here refill from a poll, so without this the whole body blinks and
 * the user cannot tell a refresh from a change. The stagger is per row and
 * capped at six, which covers a first screenful; later rows share the last
 * delay so a long list does not animate in slow motion.
 *
 * The fill-mode matters: the row must hold its opening state during its
 * delay, otherwise it flashes at full opacity first and then starts.
 */
.flup-table-rows tbody tr { animation: flup-rise 180ms var(--flup-ease) both; }
.flup-table-rows tbody tr:nth-child(2) { animation-delay: 25ms; }
.flup-table-rows tbody tr:nth-child(3) { animation-delay: 50ms; }
.flup-table-rows tbody tr:nth-child(4) { animation-delay: 75ms; }
.flup-table-rows tbody tr:nth-child(5) { animation-delay: 100ms; }
.flup-table-rows tbody tr:nth-child(n+6) { animation-delay: 125ms; }
/* A hovered row gets a brand edge as well as a fill, so the pointer's position
   is unambiguous in a dense table where adjacent rows are the same height. */
.flup-table-rows tbody tr:hover {
  background: var(--flup-surface-1);
  box-shadow: inset 2px 0 0 var(--dsw-alias-border-l2);
  box-shadow: inset 2px 0 0 var(--flup-edge-brand);
}
.flup-btn-sm { padding: 2px 9px; font-size: 11px; }
/* A row title that opens a drill-in: underlined so it reads as a link, but a
   real button for keyboard and screen-reader users. */
.flup-link {
  appearance: none; border: none; background: none; font: inherit; font-size: 12px;
  color: var(--flup-ink); cursor: pointer; padding: 0; text-align: left;
  text-decoration: underline; text-underline-offset: 2px;
}
.flup-link:focus-visible { outline: 2px solid var(--flup-ink); outline-offset: 2px; }
/* Branch filter chips above the task list. */
.flup-filter { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; padding-bottom: 12px; }
/* The same row used outside a body block, which supplies its own gutter. */
.flup-filter-inset { padding: 2px 20px 0; }
/* A second line inside a table cell: the card model under its host name. */
.flup-cell-sub { display: block; }
.flup-chip {
  appearance: none; font: inherit; font-size: 11px; cursor: pointer;
  border: 1px solid var(--dsw-alias-border-l2); border-radius: var(--flup-r3);
  background: var(--flup-surface-1); color: var(--dsw-alias-label-primary);
  padding: 3px 11px; white-space: nowrap;
  transition: background 120ms var(--flup-ease), border-color 120ms var(--flup-ease),
    color 120ms var(--flup-ease), transform 120ms var(--flup-ease);
}
.flup-chip:active { transform: scale(0.96); }
.flup-chip:hover { background: var(--flup-surface-2); }
.flup-chip[aria-pressed="true"] {
  border-color: var(--flup-ink);
  background: var(--flup-surface-2); background: var(--flup-wash-brand);
  color: var(--flup-ink); font-weight: 500;
}
/*
 * A vendor chip wears its vendor's colour.
 *
 * Twelve hardware vendors used to render as twelve identical grey chips, so
 * telling them apart meant reading every label. The hue is set per chip from
 * the wheel above, and stays with that vendor across sorts and reloads
 * because it is hashed from the vendor's own name rather than taken from list
 * position.
 *
 * Unselected chips show the hue as a dot and a tinted edge, not as a fill:
 * twelve saturated pills would compete with the task tables that actually
 * carry status. Selecting one fills it, which keeps selection the loudest
 * thing in the row.
 *
 * The label never relies on the colour, so this remains legible to anyone who
 * cannot distinguish the hues.
 */
/*
 * The hue is the chip's backing plate, not a dot on it.
 *
 * A 7px dot was the first attempt and was too small to register: the eye has
 * to find it before the colour can mean anything, which defeats the purpose
 * of colour-coding. The plate is the whole chip, so the vendor reads at a
 * glance without being looked for.
 *
 * 60% of the hue against layer-1 is the measured limit, not a taste call. It
 * is where the twelve plates stay separable from each other — closest pair 24
 * of a possible 765 — while the label keeps 6.7:1 contrast in light and 5.5:1
 * in dark, both well past the 4.5:1 that small text needs. Mixing against a
 * theme surface is also what makes one declaration work in both themes: the
 * same 60% lands on #fff in light and #232324 in dark.
 */
.flup-chip-hue {
  border-color: color-mix(in srgb, var(--flup-chip-hue) 70%, transparent);
  background: color-mix(in srgb, var(--flup-chip-hue) 60%, var(--flup-surface-1));
  color: var(--dsw-alias-label-primary);
}
.flup-chip-hue:hover {
  background: color-mix(in srgb, var(--flup-chip-hue) 75%, var(--flup-surface-1));
  border-color: var(--flup-chip-hue);
}
/*
 * Selection deepens the plate and adds a ring.
 *
 * The plate is already coloured, so selection cannot be "now it has colour".
 * A fully saturated fill was tried and is wrong at this saturation: 35% hues
 * do not carry white text. Instead the plate goes denser and gains an outer
 * ring, which reads as selected without changing the text colour or breaking
 * the muted register the rest of the row keeps.
 */
.flup-chip-hue[aria-pressed="true"] {
  border-color: var(--flup-chip-hue);
  background: color-mix(in srgb, var(--flup-chip-hue) 88%, var(--flup-surface-1));
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--flup-chip-hue) 35%, transparent);
  font-weight: 500;
}
/*
 * The same vendor hue inside a table, so a row and the chip that filters it
 * read as the same thing. Quieter than the chip: a column of twelve colours
 * must not outshout the status column next to it.
 */
/*
 * A quieter plate than the chip, at 40% rather than 60%.
 *
 * A column of twelve plates sits beside the status column, which carries real
 * state and has to stay the loudest thing in the row. 40% keeps the vendor
 * recognisable — closest pair 15, label contrast 9.8:1 light and 7.8:1 dark —
 * while reading as a tint rather than a highlight.
 */
.flup-vendor {
  display: inline-block; padding: 1px var(--flup-s2); border-radius: var(--flup-r3);
  background: color-mix(in srgb, var(--flup-chip-hue) 40%, var(--flup-surface-1));
  color: var(--dsw-alias-label-primary);
}
.flup-chip:focus-visible { outline: 2px solid var(--flup-ink); outline-offset: 1px; }
/* Task drill-in. */
.flup-detail-head { display: flex; align-items: center; gap: 8px; padding-bottom: 10px; }
.flup-detail-title { margin: 0 0 4px; font-size: 14px; font-weight: 600; color: var(--dsw-alias-label-primary); }
.flup-sub-flush { padding-left: 0; padding-right: 0; }
.flup-card {
  border: 1px solid var(--dsw-alias-border-l1); border-radius: var(--flup-r2);
  padding: var(--flup-s3) 14px; margin-bottom: var(--flup-s3);
  background: var(--flup-surface-1);
  animation: flup-rise 220ms var(--flup-ease) both;
}
/*
 * Cards arrive in sequence rather than all at once.
 *
 * Identical timing on every card reads as one block flashing into place, which
 * is what made an otherwise animated panel feel static. A small stagger lets
 * the eye follow the order the screen is actually in. It stops at the fourth
 * card: past roughly 120ms of total delay the last one feels late rather than
 * sequenced, and a long screen would spend its first half-second empty.
 */
.flup-card:nth-child(2) { animation-delay: 40ms; }
.flup-card:nth-child(3) { animation-delay: 80ms; }
.flup-card:nth-child(n+4) { animation-delay: 120ms; }
.flup-card-title {
  margin: 0 0 8px; font-size: 12px; font-weight: 600; color: var(--dsw-alias-label-primary);
}
.flup-pre {
  margin: 0; font-size: 12px; line-height: 1.55; white-space: pre-wrap; word-break: break-word;
  color: var(--dsw-alias-label-primary); max-height: 320px; overflow: auto;
  background: var(--flup-surface-2); border-radius: var(--flup-r2); padding: 8px 10px;
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
  margin: 10px 0 0; font-size: 12px; padding: 8px 10px; border-radius: var(--flup-r2);
  border: 1px solid var(--dsw-alias-state-warn-primary); color: var(--dsw-alias-label-primary);
}
.flup-warn p { margin: 0; }
/*
 * A blocking setup problem rather than a caution.
 *
 * Tinted as well as bordered: this is the one thing on a first-run screen the
 * user has to act on, so it should read as a stop, not as a footnote. The text
 * carries the instruction, so the colour is never the only signal.
 */
.flup-warn-err {
  border-color: var(--dsw-alias-state-error-primary);
  background: var(--flup-surface-2);
  background: color-mix(in srgb, var(--dsw-alias-state-error-primary) 7%, var(--flup-surface-1));
}
.flup-warn-lead { font-weight: 600; }
.flup-code-inline {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 11px; padding: 1px 5px; border-radius: var(--flup-r1);
  background: var(--flup-surface-2); word-break: break-all;
}
.flup-events {
  margin: 0; padding: 0; list-style: none; font-size: 12px; line-height: 1.55;
  max-height: 380px; overflow: auto; display: flex; flex-direction: column; gap: 8px;
}
.flup-event { display: flex; gap: 8px; align-items: baseline; }
.flup-event-bare { opacity: 0.7; }
.flup-event-text { white-space: pre-wrap; word-break: break-word; min-width: 0; }
.flup-pill { display: inline-flex; align-items: center; gap: var(--flup-s1); white-space: nowrap; }
.flup-num { font-variant-numeric: tabular-nums; color: var(--dsw-alias-label-secondary); }
/*
 * A tag states a fact about a row, so its colour has to mean something: a
 * neutral tag is informational, and the three variants below map to the theme's
 * own state tokens so they follow light/dark and stay legible. Each coloured
 * tag keeps its text label — colour is never the only signal.
 */
.flup-tag {
  border-radius: var(--flup-r1); padding: 1px 6px; font-size: 11px;
  background: var(--flup-surface-2); color: var(--dsw-alias-label-secondary);
  border: 1px solid transparent;
}
.flup-tag-warn {
  color: var(--dsw-alias-state-warn-primary);
  border-color: var(--dsw-alias-state-warn-primary);
  background: transparent;
}
.flup-tag-error {
  color: var(--dsw-alias-state-error-primary);
  border-color: var(--dsw-alias-state-error-primary);
  background: transparent;
}
.flup-tag-active {
  color: var(--flup-ink);
  border-color: var(--flup-ink);
  background: transparent;
}

.flup-meta { color: var(--dsw-alias-label-secondary); font-size: 12px; }
.flup-empty { color: var(--dsw-alias-label-secondary); font-size: 12px; padding: 6px 0; }
.flup-code {
  display: block; background: var(--flup-surface-2); border-radius: var(--flup-r2);
  padding: 8px 10px; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 11px; white-space: pre-wrap; word-break: break-all; margin: 8px 0 0;
}
.flup-err { color: var(--dsw-alias-state-error-primary); font-size: 12px; }
.flup-ok { color: var(--dsw-alias-state-success-primary); font-size: 12px; }
/*
 * A left edge in the state colour, so a card's condition reads before its
 * text, now with a matching wash across the surface. The wash is mixed from
 * the same token as the edge, which keeps the two from drifting apart and
 * means a card never shows one state at its edge and another in its fill.
 */
/*
 * Each tinted rule states the plain surface first and the mixed one second.
 * color-mix() needs a recent engine; where it is missing the second
 * declaration is dropped as invalid and the card keeps its coloured edge on a
 * plain background, which is exactly how this panel looked before the tints.
 * The edge, the tag and the text carry the state either way.
 */
.flup-card-warn {
  border-left: 3px solid var(--dsw-alias-state-warn-primary);
  background: var(--flup-surface-1); background: var(--flup-wash-warn);
}
.flup-card-ok {
  border-left: 3px solid var(--dsw-alias-state-success-primary);
  background: var(--flup-surface-1); background: var(--flup-wash-ok);
}
.flup-card-accent {
  border-left: 3px solid var(--flup-ink);
  background: var(--flup-surface-1); background: var(--flup-wash-brand);
}
.flup-card-error {
  border-left: 3px solid var(--dsw-alias-state-error-primary);
  background: var(--flup-surface-1); background: var(--flup-wash-error);
}
/*
 * Placeholder bars shown while a request is in flight, sized like the content
 * they stand in for so the layout does not jump when real data replaces them.
 * The sweep is what distinguishes "still loading" from "loaded and empty",
 * which a static grey block cannot say on its own.
 */
.flup-skel-wrap { padding: 4px 0 2px; }
.flup-skel {
  border-radius: var(--flup-r1); height: 11px; margin: 8px 0;
  /* The sweep rides the tinted surfaces too, or a loading placeholder is the
     one grey rectangle on a warm page. */
  background: linear-gradient(90deg,
    var(--flup-surface-2) 25%,
    var(--flup-wash-brand) 37%,
    var(--flup-surface-2) 63%);
  background-size: 200% 100%;
  animation: flup-sweep 1.3s ease-in-out infinite;
}
/* An inline spinner for a button that is waiting on a round trip. */
.flup-spin {
  display: inline-block; width: 10px; height: 10px; flex: none;
  border: 2px solid currentColor; border-right-color: transparent; border-radius: 50%;
  animation: flup-spin 620ms linear infinite; vertical-align: -1px;
}
.flup-form { display: flex; flex-direction: column; gap: 10px; }
.flup-field { display: flex; flex-direction: column; gap: 3px; }
.flup-label { font-size: 11px; color: var(--dsw-alias-label-secondary); }
.flup-check { display: flex; align-items: center; gap: 8px; font-size: 13px; font-weight: 500; }
.flup-grid { display: grid; grid-template-columns: 2fr 1fr 1fr; gap: 8px; }
@media (max-width: 720px) { .flup-grid { grid-template-columns: 1fr; } }
`

    /**
     * Map a dashboard status word onto a theme state token.
     *
     * Covers three vocabularies at once: branch/CI results, device states, and
     * task statuses. A word this does not know falls through to the idle token
     * rather than guessing, so a new upstream status reads as neutral instead
     * of wrongly green or red.
     */
    function statusColor(status) {
      const value = String(status || '').toLowerCase()
      if (['passed', 'success', 'merged', 'free', 'online', 'ok', 'completed'].includes(value)) {
        return 'var(--dsw-alias-state-success-primary)'
      }
      if (['failed', 'failure', 'error', 'down', 'blocked'].includes(value)) {
        return 'var(--dsw-alias-state-error-primary)'
      }
      if (['partial', 'pending', 'stale', 'open', 'unknown', 'busy', 'interrupted', 'waiting_approval', 'waiting_input'].includes(value)) {
        return 'var(--dsw-alias-state-warn-primary)'
      }
      // A turn genuinely in flight is the brand accent, not a warning: it is
      // the normal healthy state of a task that is working.
      if (['running', 'starting', 'interrupting', 'prepared'].includes(value)) {
        return 'var(--dsw-alias-brand-primary)'
      }
      return 'var(--dsw-alias-state-idle-primary)'
    }

    /**
     * Statuses that mean a turn is executing right now.
     *
     * Deliberately narrower than the set `statusColor` paints with the brand
     * accent: `prepared` is brand-coloured because it is healthy, but nothing
     * is running yet, so it must not pulse. Keeping this list short is what
     * stops motion from becoming ambient noise.
     */
    const LIVE_STATUSES = ['running', 'starting', 'interrupting']

    function Dot({ status }) {
      const live = LIVE_STATUSES.includes(String(status || '').toLowerCase())
      const color = statusColor(status)
      return h('span', {
        className: live ? 'flup-dot flup-dot-live' : 'flup-dot',
        // `color` drives the halo through currentColor; `background` is the dot.
        style: live ? { background: color, color } : { background: color },
        'aria-hidden': true,
      })
    }

    /**
     * Placeholder bars for a block that is still loading.
     *
     * Announced through `role="status"` with a real label, because the sweep
     * says "loading" only to someone who can see it. Bars vary in width so a
     * stack of them reads as content taking shape rather than a progress bar.
     */
    function Skeleton({ rows = 3, label }) {
      const widths = ['92%', '74%', '83%', '61%', '88%']
      return h('div', { className: 'flup-skel-wrap', role: 'status', 'aria-label': label || undefined },
        Array.from({ length: Math.max(1, rows) }, (unused, index) =>
          h('div', { key: index, className: 'flup-skel', style: { width: widths[index % widths.length] } })),
      )
    }

    /** A button label that shows a spinner while its request is in flight. */
    function Pending({ label }) {
      return h(React.Fragment, null, h('span', { className: 'flup-spin', 'aria-hidden': true }), ` ${label}`)
    }

    /** One status cell: a dot plus its word, linked when the source gives a URL. */
    function Status({ value, extra }) {
      const text = value && typeof value === 'object' ? value.status : value
      if (!text) return h('span', { className: 'flup-meta' }, '—')
      const detail = value && typeof value === 'object' ? value.detail : null
      const url = value && typeof value === 'object' ? value.url : null
      // `extra` is a secondary line appended under the primary detail in the
      // hover title — used for the branch-tip CI run beside the verdict.
      const title = [detail ? String(detail) : null, extra ? String(extra) : null]
        .filter(Boolean).join('\n') || undefined
      const body = h(React.Fragment, null, h(Dot, { status: text }), String(text))
      return h('span', { className: 'flup-pill', title },
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

    /**
     * Reduce the per-collector records to the one verdict the header shows.
     *
     * Health is reported pessimistically on purpose: the indicator exists to
     * answer "is the data I am looking at current", and a fleet that collects
     * branches fine while every GPU host is unreachable is not healthy. So any
     * failing collector makes the whole indicator say so, and its error is the
     * one named in the tooltip.
     *
     * A collector that has never been read is not evidence either way and is
     * skipped; when that is all of them the verdict is `waiting`.
     */
    function collectionVerdict(collection) {
      const rows = collection && typeof collection === 'object' ? Object.entries(collection) : []
      if (!rows.length) return { state: 'waiting', failing: [], okAt: null, error: null }
      const failing = rows.filter(([, value]) => value && value.error)
      // The most recent success across collectors: what "last data" means to
      // someone reading the panel as a whole.
      const okAt = rows.reduce((latest, [, value]) => {
        const at = value && typeof value.okAt === 'number' ? value.okAt : null
        return at !== null && (latest === null || at > latest) ? at : latest
      }, null)
      if (failing.length) {
        return {
          state: 'stale',
          failing: failing.map(([name]) => name),
          okAt,
          error: String(failing[0][1].error),
        }
      }
      return { state: okAt === null ? 'waiting' : 'ok', failing: [], okAt, error: null }
    }

    /**
     * The collection indicator in the panel header.
     *
     * Separate from the process indicator beside it, because the two answer
     * different questions and routinely disagree: the process is up while
     * collection fails. It renders nothing while the dashboard is down, where
     * the process indicator already explains the situation and a second
     * "no data" badge would only repeat it.
     */
    function CollectionBadge({ collection, running, t }) {
      if (!running) return null
      const verdict = collectionVerdict(collection)
      const label = verdict.state === 'ok'
        ? t.collecting
        : (verdict.state === 'stale' ? t.collectStale : t.collectWaiting)
      const names = verdict.failing
        .map(name => t[`collect${name.charAt(0).toUpperCase()}${name.slice(1)}`] || name)
        .join(', ')
      const title = verdict.state === 'ok'
        ? `${t.collectOkHint}${localTime(verdict.okAt)}`
        : (verdict.state === 'stale'
          ? `${names ? `${names}: ` : ''}${t.collectStaleHint}${verdict.error}`
            + (verdict.okAt === null ? ` (${t.collectNeverOk})` : ` (${t.collected} ${localTime(verdict.okAt)})`)
          : t.collectWaitingHint)
      return h('span', { className: 'flup-state flup-collect', title },
        // `failed` and `unknown` are the vocabulary statusColor already
        // paints; a word outside its lists would fall through to idle grey.
        h(Dot, { status: verdict.state === 'ok' ? 'online' : (verdict.state === 'stale' ? 'failed' : 'unknown') }),
        label,
      )
    }

    /**
     * Update control for the plugin itself.
     *
     * Only present when there is something to do: with no newer release this
     * renders nothing rather than a permanently greyed-out button, because the
     * header is shared with the controls that run the dashboard and a dead
     * control there reads as a broken one.
     *
     * The states that cannot install (a linked checkout, a build without the
     * plugin manager) still surface the available version as plain text, so
     * the information is not hidden from someone who can act on it another
     * way.
     */
    function UpdateButton({ t }) {
      /*
       * Nothing happens until the user asks.
       *
       * An earlier version checked on mount and then showed a button only when
       * a newer release existed. That had two faults. It reached GitHub every
       * time the panel mounted, for data that changes perhaps weekly; and on
       * an up-to-date install it rendered nothing at all, so "is there a new
       * version?" had no answer anywhere in the UI — the honest answer, "you
       * are current", was exactly the case that showed nothing.
       *
       * So the control starts as a plain "check for updates" and every network
       * read is a direct consequence of a click.
       */
      const [state, setState] = React.useState(null)
      const [checking, setChecking] = React.useState(false)
      const [busy, setBusy] = React.useState(false)
      const [done, setDone] = React.useState(null)
      const [failed, setFailed] = React.useState(null)

      /*
       * `refresh=1` bypasses the Host's cache.
       *
       * A click means "tell me now", so a cached answer would make the button
       * a no-op that looks like it worked. The Host still caches between
       * clicks, which is what keeps repeated presses from spending the
       * unauthenticated rate limit.
       */
      const check = React.useCallback(async () => {
        setChecking(true)
        setFailed(null)
        try {
          setState(await callJson(`${BASE}update-state?refresh=1`))
        } catch (failure) {
          setState(null)
          setFailed(failure.message)
        } finally {
          setChecking(false)
        }
      }, [])

      const install = React.useCallback(async () => {
        setBusy(true)
        setFailed(null)
        try {
          const result = await callJson(`${BASE}self-update`, { method: 'POST' })
          setDone(result)
        } catch (failure) {
          setFailed(failure.message)
        } finally {
          setBusy(false)
        }
      }, [])

      if (done && done.changed) {
        // The new code is on disk but this page still runs the old module, so
        // the honest report is "installed, reload to use it".
        return h('span', { className: 'flup-state', title: t.updateRestart },
          h(Dot, { status: 'online' }),
          `${t.updateDone} ${done.to}`,
        )
      }

      // Before the first click, and while one is in flight.
      if (!state) {
        return h('button', {
          className: 'flup-btn',
          type: 'button',
          disabled: checking,
          title: failed ? `${t.updateCheckFailed}: ${failed}` : undefined,
          onClick: check,
        }, checking ? h(Pending, { label: t.updateCheck }) : t.updateCheck)
      }

      /*
       * The check reached the Host but the Host could not reach GitHub. This
       * is distinct from "no update": the question went unanswered, so the
       * control stays clickable and says why rather than claiming currency.
       */
      if (state.checkError) {
        const reason = state.checkError === 'rate-limited'
          ? t.updateRateLimited
          : `${t.updateCheckFailed} (${state.checkError})`
        return h('button', {
          className: 'flup-btn',
          type: 'button',
          disabled: checking,
          title: reason,
          onClick: check,
        }, checking ? h(Pending, { label: t.updateCheck }) : t.updateCheck)
      }

      // Checked, and already current. Clickable again, because a release can
      // appear at any time and this is the only way to ask.
      if (!state.newer) {
        return h('button', {
          className: 'flup-btn',
          type: 'button',
          disabled: checking,
          // Punctuation lives here, not in the string: a translated label that
          // ended in a full-width colon rendered a double gap before the
          // version.
          title: state.installed ? `${t.updateInstalledIs}: ${state.installed}` : undefined,
          onClick: check,
        }, checking ? h(Pending, { label: t.updateCheck }) : t.updateCurrent)
      }

      const version = state.latest ? state.latest.version : ''
      if (!state.installable) {
        // A newer release exists but this install cannot take it: a linked
        // checkout, or a build without the plugin manager. Report the version
        // as text so the information is not hidden from someone who can act
        // on it another way.
        const hint = state.linked ? t.updateLinkedHint : t.updateUnsupportedHint
        return h('span', { className: 'flup-state', title: hint },
          h(Dot, { status: 'unknown' }),
          `${t.updateAvailable} ${version}`,
        )
      }
      return h('button', {
        className: 'flup-btn flup-btn-primary',
        type: 'button',
        disabled: busy,
        // The release notes are the only description of what the update
        // changes, so they ride along as the button's tooltip.
        title: failed
          ? failed
          : [state.latest && state.latest.notes ? `${t.updateNotes}:\n${state.latest.notes}` : null]
            .filter(Boolean).join('\n\n') || undefined,
        onClick: install,
      }, busy ? h(Pending, { label: t.updateInstalling }) : `${t.updateAvailable} ${version}`)
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

    /**
     * The PR cell's secondary line: an unpin control for a pinned PR, or,
     * when nothing matched, the open PRs on this branch that name the
     * backend, each pinnable in one click.
     */
    function PrPicker({ backend, t, onPin, pinning }) {
      if (!onPin) return null
      const busy = pinning === backend.id
      if (backend.pr && backend.pr.selectedBy === 'pinned') {
        return h('button', {
          type: 'button',
          className: 'flup-btn flup-btn-sm flup-inline-note',
          disabled: busy,
          title: t.unpinHint,
          onClick: () => onPin(String(backend.id), null),
        }, busy ? t.pinning : t.unpin)
      }
      if (backend.pr) return null
      const candidates = Array.isArray(backend.prCandidates) ? backend.prCandidates : []
      if (!candidates.length) return h('div', { className: 'flup-inline-note flup-muted' }, t.noPrCandidates)
      return h('div', { className: 'flup-pr-candidates' },
        h('span', { className: 'flup-muted' }, t.prCandidates),
        candidates.map(row => h('div', { key: row.number, className: 'flup-pr-candidate' },
          row.url
            ? h('a', { className: 'flup-link', href: String(row.url), target: '_blank', rel: 'noreferrer noopener', title: row.title }, `#${row.number}`)
            : h('span', null, `#${row.number}`),
          h('span', { className: 'flup-pr-title', title: row.title }, row.title + (row.draft ? ` (${t.draft})` : '')),
          h('button', {
            type: 'button',
            className: 'flup-btn flup-btn-sm',
            disabled: busy,
            onClick: () => onPin(String(backend.id), row.number),
          }, busy ? t.pinning : t.pin),
        )),
      )
    }

    /**
     * The gate counts only evidence produced at the exact branch HEAD, so
     * "0/20" alone hides the difference between "never run" and "ran on an
     * older commit". Lead with the verdict and explain it in the tooltip.
     */
    function GateCell({ gate, t }) {
      if (!gate) return '—'
      const count = gate.total ? `${gate.passed ?? 0}/${gate.total}` : null
      const detail = gate.detail
        || (gate.status === 'stale' && gate.latestCommit ? t.gateStale(gate.latestCommit)
          : gate.status === 'missing' ? t.gateMissing
            : null)
      return h(React.Fragment, null,
        h(Status, { value: { status: gate.status, detail } }),
        count && gate.status !== 'missing' && gate.status !== 'stale'
          ? h('span', { className: 'flup-inline-note' }, count)
          : null,
      )
    }

    function BackendTable({ status, t, onPin, pinning }) {
      const backends = (status && Array.isArray(status.backends)) ? status.backends : []
      if (!backends.length) return h('p', { className: 'flup-empty' }, t.noBackends)
      // Same vendor colours as the fleet view. A backend here and a machine
      // vendor there are the same hardware, so they should look related.
      const hues = huePalette(backends.map(backend => backend.label || backend.id))
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
              h('span', {
                className: 'flup-vendor',
                style: { '--flup-chip-hue': hues.get(String(backend.label || backend.id)) || 'var(--dsw-alias-state-idle-primary)' },
              }, String(backend.label || backend.id)),
              backend.pr && backend.pr.url
                ? h('a', {
                  className: 'flup-link flup-inline-note',
                  href: String(backend.pr.url),
                  target: '_blank',
                  rel: 'noreferrer noopener',
                }, `#${backend.pr.number}`)
                : null,
              backend.pr && backend.pr.selectedBy === 'pinned'
                ? h('span', { className: 'flup-tag flup-inline-note', title: t.pinnedHint }, t.pinned)
                : null,
              h(PrPicker, { backend, t, onPin, pinning }),
            ),
            h('td', null, h(Status, { value: backend.integration })),
            // `backend.ci` is the resolved adaptation-CI verdict from the host
            // (a merged-PR or accepted pass, or a stale downgrade). The
            // branch-tip run, when it differs, rides along in the tooltip
            // rather than replacing the verdict.
            h('td', null, h(Status, {
              value: backend.ci,
              extra: backend.currentCi && backend.currentCi.detail
                ? `${t.ciLatestRun}: ${backend.currentCi.detail}`
                : null,
            })),
            h('td', null, h(Status, {
              value: backend.acceptance && {
                status: backend.acceptance.status,
                detail: backend.acceptance.summary,
              },
            })),
            h('td', { className: 'flup-num' }, h(GateCell, { gate, t })),
          )
        })),
      )
    }

    /**
     * The GPU fleet, as its own view.
     *
     * Devices sit on a different axis from everything else in this panel. A
     * backend's PRs and a task's progress both belong to one project on one
     * branch; a machine belongs to neither. The dashboard confirms this: the
     * device payload is byte-identical across projects, and the route takes no
     * branch at all. So this reads the fleet once rather than appearing under
     * each project, where a branch picker sat above data the branch did not
     * affect.
     *
     * It still needs a project id to call through, because the dashboard
     * namespaces its routes that way, but which project is asked makes no
     * difference to the answer.
     */
    function DeviceView({ t, project, running }) {
      // Cached across the remount that switching views causes, so returning
      // to the fleet shows the last roster instead of a skeleton.
      const [devices, setDevices] = useCached('devices', 'devices', null)
      const [error, setError] = useCached('devices', 'error', null)
      const [busy, setBusy] = React.useState(false)
      // null means every backend. Filtering happens here because the device
      // route rejects any query key but `project`, so there is nothing to
      // delegate: the fleet arrives whole and is narrowed on arrival.
      // Cached too: a vendor filter is a deliberate choice and losing it on
      // every visit is the same annoyance as losing the data.
      const [backend, setBackend] = useCached('devices', 'backend', null)

      const load = React.useCallback(async () => {
        if (!running || !project) return
        try {
          const body = await callJson(readPath('/api/devices', { project: project.id }))
          setDevices(body)
          setError(body && body.error ? String(body.error) : null)
        } catch (failure) {
          setError(String(failure.message))
        }
      }, [project && project.id, running])

      React.useEffect(() => {
        let alive = true
        // Skip a round while the panel is not on screen: this reaches the
        // dashboard, which in turn holds counters for 57 hosts.
        const tick = () => { if (alive && !document.hidden) load() }
        tick()
        const timer = setInterval(tick, 30_000)
        return () => {
          alive = false
          clearInterval(timer)
        }
      }, [load])

      /*
       * Force a collection round now.
       *
       * This opens SSH sessions to every GPU host, so it stays an explicit
       * button. Background collection already runs inside the dashboard on a
       * fixed interval and cannot be switched off from here.
       */
      const refresh = React.useCallback(async () => {
        if (!project) return
        setBusy(true)
        try {
          await callJson(`${BASE}refresh`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: '/api/devices/refresh', project: project.id }),
          })
          await load()
        } catch (failure) {
          setError(String(failure.message))
        } finally {
          setBusy(false)
        }
      }, [project && project.id, load])

      if (!running) {
        return h('div', { className: 'flup-body' }, h('p', { className: 'flup-empty' }, t.dashboardDown))
      }

      const allHosts = (devices && Array.isArray(devices.hosts)) ? devices.hosts : []
      // Backends in fleet order of size, so the vendors with most machines
      // come first rather than in whatever order the bastion listed them.
      const backends = [...allHosts.reduce((seen, host) => {
        const key = host.backend || ''
        if (key) seen.set(key, (seen.get(key) || 0) + 1)
        return seen
      }, new Map())].sort((a, b) => b[1] - a[1] || String(a[0]).localeCompare(String(b[0])))
      // A filter pinned to a backend that has left the fleet would show an
      // empty table with no way to tell why, so fall back to showing all.
      const known = backends.some(([name]) => name === backend)
      const active = known ? backend : null
      const hosts = active ? allHosts.filter(host => host.backend === active) : allHosts
      // Built from the WHOLE fleet, so selecting a vendor does not recolour
      // the rows that remain. A palette derived from the filtered list would
      // make every single-vendor view the same first hue.
      const hues = huePalette(allHosts.map(host => host.backend))

      /*
       * The fleet in one line, recomputed from the hosts on screen.
       *
       * The upstream summary describes the whole fleet, so it cannot be used
       * once a backend is selected: it would report 57 hosts above a table of
       * four. Every field it carries is derivable from the host list, and I
       * verified each one against the live payload, so recomputing keeps the
       * headline and the table describing the same machines.
       *
       * One field is not derivable: `online` counts hosts the collector
       * reached, and the `reachable` flag behind it is stripped before the
       * payload leaves the dashboard. Rather than approximate it from the
       * verdict, the headline reports stale hosts instead — a host whose data
       * is old is the thing worth acting on, and that flag does ship.
       */
      const cards = metric => hosts.reduce((total, host) => total + Math.max(0, Number((host.accel && host.accel[metric]) || 0)), 0)
      const freeHosts = hosts.filter(host => host.verdict === 'free' && !host.stale).length
      const staleHosts = hosts.filter(host => host.stale).length
      const unknownCards = cards('unknown')

      const stats = [
        [t.statFreeHosts, freeHosts, hosts.length],
        [t.statFreeCards, cards('free'), cards('total')],
      ]

      return h(React.Fragment, null,
        h('div', { className: 'flup-sub' },
          stats.map(([label, value, total]) => h('span', { key: label },
            `${label} `,
            h('b', null, `${value}/${total}`))),
          // Cards whose state could not be read, and hosts whose data is old.
          // Both are stated only when non-zero: zero is the normal case, and a
          // permanent "unknown 0" trains the eye to skip the whole line.
          unknownCards
            ? h('span', { className: 'flup-warn' }, `${t.statUnknown} `, h('b', null, String(unknownCards)))
            : null,
          staleHosts
            ? h('span', { className: 'flup-warn' }, `${t.statStale} `, h('b', null, String(staleHosts)))
            : null,
          h('span', null, `${t.collected} ${(devices && localTime(devices.updated_at)) || t.never}`),
          devices && devices.refreshing ? h('span', { className: 'flup-tag flup-tag-active' }, t.refreshing) : null,
          h('span', { className: 'flup-spacer' }),
          h('button', {
            className: 'flup-btn flup-btn-accent',
            type: 'button',
            disabled: busy,
            title: t.refreshDevicesHint,
            onClick: refresh,
          }, busy ? h(Pending, { label: t.refreshDevices }) : t.refreshDevices),
        ),
        h('p', { className: 'flup-note' }, t.devicesNote),
        /*
         * Backend filter.
         *
         * Hardware is the axis that matters here, and the one axis the fleet
         * genuinely has: a machine belongs to a vendor permanently, where it
         * belongs to no project and no branch. Each chip carries its host
         * count, so the fleet's shape is readable before anything is clicked
         * and an empty vendor is visibly empty rather than a dead end.
         */
        backends.length > 1
          ? h('div', { className: 'flup-filter flup-filter-inset', role: 'group', 'aria-label': t.backend },
            h('button', {
              className: 'flup-chip',
              type: 'button',
              'aria-pressed': active === null,
              onClick: () => setBackend(null),
            }, `${t.allBackends} (${allHosts.length})`),
            backends.map(([name, count]) => h('button', {
              key: name,
              className: 'flup-chip flup-chip-hue',
              type: 'button',
              // The one inline style the hue needs: the value is per vendor,
              // so it cannot live in a static stylesheet. The stylesheet owns
              // every rule that reads it.
              style: { '--flup-chip-hue': hues.get(String(name)) || 'var(--dsw-alias-state-idle-primary)' },
              'aria-pressed': active === name,
              // The chip shows one half of the bilingual name to keep the row
              // short; the title carries the full one the bastion reports, so
              // nothing is lost for anyone checking which vendor this is.
              title: String(name),
              onClick: () => setBackend(active === name ? null : name),
            }, `${shortBackend(name, t)} (${count})`)),
          )
          : null,
        h('div', { className: 'flup-body' },
          error ? h('p', { className: 'flup-err' }, `${t.error}: ${error}`) : null,
          !devices && !error ? h(Skeleton, { rows: 6, label: t.loading }) : null,
          devices ? h(DeviceTable, { hosts, t, hues }) : null,
        ),
      )
    }

    /**
     * One row per GPU host.
     *
     * Takes the hosts to show rather than the whole payload, so the caller
     * owns the filtering and this cannot disagree with the counts above it.
     */
    function DeviceTable({ hosts: rows, t, hues }) {
      const hosts = Array.isArray(rows) ? rows : []
      if (!hosts.length) return h('p', { className: 'flup-empty' }, t.noDevices)
      // Built from the caller's full fleet when provided, so a filtered table
      // keeps the colours the unfiltered chip row showed.
      const palette = hues || huePalette(hosts.map(host => host.backend))
      return h('table', { className: 'flup-table' },
        h('thead', null, h('tr', null,
          h('th', null, t.host),
          h('th', null, t.backend),
          h('th', null, t.verdict),
          h('th', null, t.cards),
        )),
        h('tbody', null, hosts.map(host => h('tr', { key: String(host.id) },
          h('td', null,
            String(host.label || host.id),
            // The card model is the detail that makes a host choosable: four
            // free cards means something different on an A100 than elsewhere.
            host.accel && host.accel.card_name
              ? h('span', { className: 'flup-meta flup-cell-sub' }, String(host.accel.card_name))
              : null,
          ),
          // Same hue as the vendor's filter chip, so a filtered table and the
          // chip that filtered it visibly belong together.
          h('td', { className: 'flup-meta' },
            host.backend
              ? h('span', {
                className: 'flup-vendor',
                style: { '--flup-chip-hue': palette.get(String(host.backend)) || 'var(--dsw-alias-state-idle-primary)' },
              }, shortBackend(host.backend, t))
              : '—'),
          h('td', null,
            h('span', { className: 'flup-pill' }, h(Dot, { status: host.verdict }), String(host.verdict || '—')),
            // Stale data looks identical to fresh data otherwise, and acting
            // on a stale free count is how two tasks land on one machine.
            host.stale ? h('span', { className: 'flup-tag flup-tag-warn' }, t.stale) : null,
          ),
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
    function TaskTable({ tasks, t, onOpen, branch, onBranch, mineOnly, onMineOnly }) {
      const all = Array.isArray(tasks) ? tasks : []
      if (!all.length) return h('p', { className: 'flup-empty' }, t.noTasks)

      // The upstream task endpoint ignores a `branch` query parameter and
      // always returns every task for the project, so scope the list here.
      // Tasks do span branches, and mixing them hides which version a task
      // targets.
      // Ownership narrows the list before branches are counted, so the chip
      // numbers describe what is actually on screen.
      const mineCount = all.filter(task => task.created_here).length
      const owned = mineOnly ? all.filter(task => task.created_here) : all

      const counts = new Map()
      for (const task of owned) {
        const key = task.branch || '—'
        counts.set(key, (counts.get(key) || 0) + 1)
      }
      const branches = [...counts.keys()].sort()
      const rows = branch ? owned.filter(task => (task.branch || '—') === branch) : owned

      const scope = h('div', { className: 'flup-filter' },
        h('span', { className: 'flup-label' }, `${t.taskScope}:`),
        h('button', {
          className: 'flup-chip',
          type: 'button',
          'aria-pressed': mineOnly,
          onClick: () => onMineOnly(true),
        }, `${t.taskScopeMine} (${mineCount})`),
        h('button', {
          className: 'flup-chip',
          type: 'button',
          'aria-pressed': !mineOnly,
          onClick: () => onMineOnly(false),
        }, `${t.taskScopeAll} (${all.length})`),
      )

      const picker = h(React.Fragment, null, scope, h('div', { className: 'flup-filter' },
        h('span', { className: 'flup-label' }, `${t.taskBranchFilter}:`),
        h('button', {
          className: 'flup-chip',
          type: 'button',
          'aria-pressed': !branch,
          onClick: () => onBranch(null),
        }, `${t.taskAllBranches} (${owned.length})`),
        branches.map(name => h('button', {
          key: name,
          className: 'flup-chip',
          type: 'button',
          'aria-pressed': branch === name,
          onClick: () => onBranch(name),
        }, `${name} (${counts.get(name)})`)),
      ))

      if (!rows.length) {
        return h('div', null, picker, h('p', { className: 'flup-empty' },
          // "Nothing created here yet" and "nothing on this branch" need
          // different wording: the first is the normal first-run state.
          mineOnly && !mineCount ? t.noTasksMine : t.noTasksForBranch))
      }

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
          // The cell spaces its own tags, so each tag no longer carries a
          // margin of its own and they cannot drift apart from one another.
          h('td', { className: 'flup-meta flup-tags' },
            String(task.source_label || task.provider || ''),
            // Why a row cannot be driven, stated in the list so the user does
            // not have to open each task to find out.
            task.policy_mode === 'monitor'
              ? h('span', { className: 'flup-tag flup-tag-warn' }, t.monitorOnly)
              : task.desktop_owned
                ? h('span', { className: 'flup-tag' }, t.taskDesktopTag)
                : !task.controllable
                  ? h('span', { className: 'flup-tag' }, t.taskObserverTag)
                  : null,
            task.pending_count
              ? h('span', { className: 'flup-tag flup-tag-error' }, `⚠ ${task.pending_count}`)
              : null,
            task.turn_active
              ? h('span', { className: 'flup-tag flup-tag-active' }, t.turnActive)
              : null,
          ),
          h('td', { className: 'flup-num' }, h('button', {
            className: 'flup-btn flup-btn-sm flup-btn-accent',
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
      // Keyed by task, so opening a second task does not show the first one's
      // transcript while its own loads.
      const scope = `task:${project.id}:${task.id}`
      const [providers, setProviders] = useCached(scope, 'providers', null)
      // An unsent message to the agent. The most expensive thing on screen to
      // lose, and previously lost on any view switch.
      const [draft, setDraft] = useCached(scope, 'draft', '')
      const [busy, setBusy] = React.useState(false)
      const [error, setError] = useCached(scope, 'error', null)
      const [sent, setSent] = React.useState(false)
      // Seeded from the row that was clicked and replaced by the fresh read.
      // Cached so a revisit keeps the newer copy rather than reverting to the
      // list's older snapshot.
      const [fresh, setFresh] = useCached(scope, 'fresh', task)
      const [events, setEvents] = useCached(scope, 'events', null)
      const [eventError, setEventError] = useCached(scope, 'eventError', null)
      const [workspace, setWorkspace] = useCached(scope, 'workspace', null)
      const [wsBusy, setWsBusy] = React.useState(false)
      const [wsError, setWsError] = useCached(scope, 'wsError', null)

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

      const workspaceAction = async route => {
        setWsBusy(true)
        setWsError(null)
        try {
          await callJson(`${BASE}${route}`, {
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
      const adopt = () => workspaceAction('adopt-workspace')
      // Drops the sidebar registration only. The directory is already gone,
      // and session logs are kept either way.
      const forget = () => workspaceAction('forget-workspace')

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
          row.stale ? h('span', { className: 'flup-tag flup-tag-warn' }, t.taskStale) : null,
          row.turn_active ? h('span', { className: 'flup-tag flup-tag-active' }, t.turnActive) : null,
          row.policy_mode === 'monitor' ? h('span', { className: 'flup-tag flup-tag-warn' }, t.monitorOnly) : null,
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
          ? h('p', { className: 'flup-warn' }, `⚠ ${t.pendingApproval} (${row.pending_count})`)
          : null,
        // Each task works in its own git worktree. Registering it as a
        // workspace puts that code in the sidebar so the task can be opened
        // directly. Registration only: no worktree is created or moved, and
        // two tasks pointing at one path resolve to a single workspace.
        workspace && workspace.supported
          ? h('div', {
            className: workspace.stale
              ? 'flup-card flup-card-warn'
              : workspace.exists ? 'flup-card flup-card-ok' : 'flup-card',
          },
            h('h4', { className: 'flup-card-title' }, t.workspaceSection),
            // A registration whose directory the dashboard has since pruned is
            // worse than none: it stays in the sidebar and opens nothing. Say
            // so and offer to drop it, rather than showing a reassuring tick.
            workspace.stale
              ? h(React.Fragment, null,
                h('p', { className: 'flup-meta flup-mb2' },
                  h('span', { className: 'flup-tag flup-tag-warn' }, t.workspaceStaleTag),
                  ' ',
                  t.workspaceStale),
                h('button', {
                  className: 'flup-btn flup-btn-sm flup-btn-danger',
                  type: 'button',
                  disabled: wsBusy,
                  title: t.workspaceForgetHint,
                  onClick: forget,
                }, wsBusy ? `${t.workspaceForgetting}…` : t.workspaceForget))
              : workspace.exists
                ? h('p', { className: 'flup-meta' },
                  h('span', { className: 'flup-ok' }, '✓ '),
                  workspace.title ? `${t.workspaceReady}: ${workspace.title}` : t.workspaceReady)
                : workspace.missing
                  ? h('p', { className: 'flup-meta' }, t.workspaceNone)
                  : h(React.Fragment, null,
                    h('button', {
                      className: 'flup-btn flup-btn-sm flup-btn-accent',
                      type: 'button',
                      disabled: wsBusy,
                      title: t.workspaceAddHint,
                      onClick: adopt,
                    }, wsBusy ? `${t.workspaceAdding}…` : t.workspaceAdd),
                    h('p', { className: 'flup-meta flup-mt2' }, t.workspaceAddHint)),
            wsError ? h('p', { className: 'flup-err' }, wsError) : null,
          )
          : null,
        // A task's own failure message gets the error-tinted card rather than a
        // bare red line, so it carries the same weight as the sections around
        // it instead of being the one piece of bad news with no surface.
        row.error
          ? h('div', { className: 'flup-card flup-card-error' },
            h('h4', { className: 'flup-card-title' }, t.error),
            h('p', { className: 'flup-err-inline flup-flat' }, String(row.error)))
          : null,
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
            !events && !eventError ? h(Skeleton, { rows: 4, label: t.loading }) : null,
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
      /*
       * Cached per project, so switching between two projects keeps both.
       *
       * The cache key carries the project id, which also replaces the reset
       * effect this component used to run on `project.id`: a different
       * project reads different slots, so there is no stale state to clear.
       * That effect additionally fought the remount — it cleared the data
       * this cache exists to preserve.
       */
      const scope = `project:${project.id}`
      const [branch, setBranch] = useCached(scope, 'branch', null)
      const [branches, setBranches] = useCached(scope, 'branches', [])
      const [status, setStatus] = useCached(scope, 'status', null)
      const [tasks, setTasks] = useCached(scope, 'tasks', null)
      const [error, setError] = useCached(scope, 'error', null)
      const [busy, setBusy] = React.useState(false)
      const [tab, setTab] = useCached(scope, 'tab', 'backends')
      // Which task is expanded is view state, not data, but it is cached for
      // the same reason: reopening the project should look like returning to
      // it rather than starting over.
      const [openTask, setOpenTask] = useCached(scope, 'openTask', null)
      const [taskBranch, setTaskBranch] = useCached(scope, 'taskBranch', null)
      // The dashboard owns tasks created outside this plugin, and there are
      // normally many of them. Default to the ones created here.
      const [mineOnly, setMineOnly] = useCached(scope, 'mineOnly', true)

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
        // Devices are fetched by their own view, not here: this request is
        // per-branch, and the fleet does not change with the branch.
        const taskBody = await callJson(readPath('/api/adaptation-tasks', { project: project.id })).catch(() => null)
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
       * Force one branch collection round now.
       *
       * Background collection already runs inside the dashboard (it starts
       * with the process and cannot be switched on or off separately), so this
       * means "don't wait for the next round" rather than "start collecting".
       * It runs `git fetch` against the configured remote, so it reaches
       * outside this machine and stays an explicit button. The device
       * equivalent lives in the device view, with its own fleet.
       */
      const refresh = React.useCallback(async () => {
        setBusy(true)
        try {
          await callJson(`${BASE}refresh`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: '/api/branch-refresh', project: project.id, branch: branch || undefined }),
          })
          await load()
        } catch (failure) {
          setError(String(failure.message))
        } finally {
          setBusy(false)
        }
      }, [project.id, branch, load])

      /*
       * Pin (number) or unpin (null) a backend's PR on the shown branch. The
       * dashboard re-collects that branch before answering, so one reload
       * shows the result.
       */
      const [pinning, setPinning] = React.useState(null)
      const pinPr = React.useCallback(async (backend, number) => {
        const target = (status && status.branch) || branch
        if (!target) return
        setPinning(backend)
        try {
          await callJson(`${BASE}pin-pr`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ project: project.id, branch: target, backend, number }),
          })
          // The dashboard answers before re-collecting (a collection outlasts
          // the request timeout), so poll until the snapshot shows the pin.
          const settled = row => number === null
            ? !(row && row.pr && row.pr.selectedBy === 'pinned')
            : Boolean(row && row.pr && row.pr.number === number)
          const deadline = Date.now() + 180_000
          for (let poll = 0; ; poll += 1) {
            const body = await callJson(readPath('/api/branch-status', { project: project.id, branch: target }))
            const rows = body && Array.isArray(body.backends) ? body.backends : []
            const row = rows.find(item => item && String(item.id) === backend)
            const busy = Boolean(body && body.collection && body.collection.refreshing)
            // Stop once the pin shows, once collection has gone idle without it
            // (give the re-collection a few polls to start), or at the deadline.
            if (settled(row) || (!busy && poll >= 3) || Date.now() > deadline) {
              setStatus(body)
              setError(body && body.error ? String(body.error) : null)
              break
            }
            await new Promise(done => setTimeout(done, 3_000))
          }
        } catch (failure) {
          setError(String(failure.message))
        } finally {
          setPinning(null)
        }
      }, [project.id, branch, status, load])

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

      // Devices are deliberately absent: they are not scoped to a project or a
      // branch, so they live in their own rail entry rather than under a
      // branch picker that does not affect them.
      const tabs = [
        ['backends', t.tabBackends],
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
          // Background collection runs on its own; this forces a round early.
          h('button', {
            className: 'flup-btn flup-btn-accent',
            type: 'button',
            disabled: busy,
            title: t.refreshBranchHint,
            onClick: refresh,
          }, busy ? h(Pending, { label: t.refreshBranch }) : t.refreshBranch),
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
          !status && !error ? h(Skeleton, { rows: 5, label: t.loading }) : null,
          tab === 'backends' && status ? h(BackendTable, { status, t, onPin: pinPr, pinning }) : null,
          tab === 'tasks'
            ? h(React.Fragment, null,
              h(RepoCard, { t, project }),
            h(CreateTask, { t, project, onCreated: load }),
              h(TaskTable, {
                tasks,
                t,
                onOpen: setOpenTask,
                branch: taskBranch,
                onBranch: setTaskBranch,
                mineOnly,
                onMineOnly: setMineOnly,
              }))
            : null,
        ),
      )
    }

    /**
     * One project's source checkout, as a sidebar workspace.
     *
     * The path is detected from the dashboard's own branch-monitor config, so
     * it is never typed in, and it never reaches the browser: the Host reports
     * the directory name and whether it exists.
     *
     * Adopting registers an existing directory and nothing more. Cloning is
     * offered as a copyable command rather than run here, because it writes to
     * disk and reaches the network, and the user asked for external actions to
     * be explicitly confirmed. A command they run themselves is the clearest
     * form of that.
     */
    function RepoCard({ t, project }) {
      const ctx = React.useContext(PluginContext)
      const scope = `repo:${project.id}`
      const [repo, setRepo] = useCached(scope, 'repo', null)
      const [busy, setBusy] = React.useState(false)
      const [error, setError] = useCached(scope, 'error', null)
      /*
       * Clone-in-flight, kept separate from `busy` because a clone outlasts a
       * normal action (it is a network fetch) and the button should say so.
       * Not cached: a stale "cloning" restored on a later visit would claim
       * work that is not happening.
       */
      const [cloning, setCloning] = React.useState(false)

      const load = React.useCallback(async () => {
        try {
          const body = await callJson(`${BASE}repos`)
          if (!body.supported) { setRepo({ supported: false }); return }
          const row = (body.repos || []).find(entry => entry.project === project.id)
          setRepo({ supported: true, ...(row || { known: false }) })
        } catch (failure) {
          setError(String(failure.message))
        }
      }, [project.id])

      React.useEffect(() => { load() }, [load])

      const adopt = async () => {
        setBusy(true)
        setError(null)
        try {
          await callJson(`${BASE}adopt-repo`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ project: project.id }),
          })
          await load()
        } catch (failure) {
          setError(String(failure.message))
        } finally {
          setBusy(false)
        }
      }

      /**
       * Clone the project's repository, then re-read so the card flips to
       * "present". The request carries only the project id — the Host takes
       * both the source and the destination from the dashboard's own config.
       */
      const cloneRepo = async () => {
        setCloning(true)
        setError(null)
        try {
          const body = await callJson(`${BASE}clone-repo`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ project: project.id }),
          })
          if (body && body.ok === false) {
            setError(String(body.error || t.repoCloneFailed))
            return
          }
          await load()
        } catch (failure) {
          setError(String(failure.message))
        } finally {
          setCloning(false)
        }
      }

      /**
       * Start a conversation in the project workspace, seeded with context.
       *
       * The workspace id comes from the Host, because the Client workspace
       * service cannot enumerate workspaces and the browser never learns the
       * path it would otherwise match on.
       *
       * The conversation is named after the branch, not the workspace. The
       * workspace is the directory and keeps a stable name; a conversation is
       * about one branch at one moment, so that is where a version belongs.
       * `connectWorkspace` resolves the session id first so it can be renamed
       * before the draft opens, which `startSession` alone cannot do.
       */
      const startSession = async () => {
        const uiWorkspace = ctx && ctx.get('uiWorkspace')
        if (!uiWorkspace || !repo.workspace) {
          setError(t.repoNoSession)
          return
        }
        setBusy(true)
        setError(null)
        const branch = repo.branch || t.repoTrackedBranch
        try {
          const options = {
            prompt: t.repoSessionPrompt
              .replace('{project}', project.label)
              .replace('{branch}', branch),
          }
          // Naming needs the id up front; without `connectWorkspace` the only
          // way in is `startSession`, which returns nothing to rename.
          if (typeof uiWorkspace.connectWorkspace === 'function') {
            const sessionId = await uiWorkspace.connectWorkspace(repo.workspace)
            await renameSession(ctx, sessionId, branch)
          }
          await uiWorkspace.startSession(repo.workspace, options)
        } catch (failure) {
          setError(String(failure.message))
        } finally {
          setBusy(false)
        }
      }

      if (!repo || repo.supported === false) return null
      if (!repo.known) {
        return h('div', { className: 'flup-card' },
          h('h3', { className: 'flup-card-title' }, t.repoTitle),
          h('p', { className: 'flup-meta' }, t.repoUnknown),
        )
      }

      return h('div', {
        className: `flup-card ${repo.adopted ? 'flup-card-ok' : repo.present ? 'flup-card-accent' : 'flup-card-warn'}`,
      },
        h('h3', { className: 'flup-card-title' }, t.repoTitle),
        h('p', { className: 'flup-meta' },
          h('b', null, repo.name),
          repo.branch ? h('span', { className: 'flup-tag' }, repo.branch) : null,
        ),

        !repo.present
          ? h('div', null,
            h('p', { className: 'flup-warn' }, `⚠ ${t.repoMissing}`),
            h('p', { className: 'flup-meta' }, t.repoCloneHint),
            // One click runs the clone. Both endpoints come from the
            // dashboard's config, so the button carries no path or URL.
            repo.remote
              ? h('div', { className: 'flup-line flup-mt2' },
                h('button', {
                  className: 'flup-btn flup-btn-sm flup-btn-accent',
                  type: 'button',
                  disabled: busy || cloning,
                  onClick: cloneRepo,
                }, cloning ? h(Pending, { label: t.repoCloning }) : t.repoClone),
                h('span', { className: 'flup-meta' }, repo.name),
              )
              // No configured source means no clone; the command stays visible
              // so the user can still do it, but the button is withheld rather
              // than guessing a repository.
              : null,
            h('code', { className: 'flup-code flup-mt2' },
              `git clone https://github.com/${repo.remote || 'OWNER/REPO'}.git ${repo.name}`),
          )
          : repo.adopted
            ? h('div', null,
              h('p', { className: 'flup-ok' }, `✓ ${t.repoAdopted}`),
              h('div', { className: 'flup-mt2' },
                h('button', {
                  className: 'flup-btn flup-btn-sm flup-btn-accent',
                  type: 'button',
                  disabled: busy,
                  onClick: startSession,
                }, t.repoStartSession),
              ),
              h('p', { className: 'flup-meta flup-mt2' }, t.repoSessionHint),
            )
            : h('div', null,
              h('p', { className: 'flup-meta' }, t.repoNotAdopted),
              h('button', {
                className: 'flup-btn flup-btn-sm flup-btn-accent',
                type: 'button',
                disabled: busy,
                onClick: adopt,
              }, busy ? h(Pending, { label: t.loading }) : t.repoAdopt),
            ),
        error ? h('p', { className: 'flup-err' }, `${t.error}: ${error}`) : null,
      )
    }

    /**
     * Create one adaptation task.
     *
     * Collapsed by default, because creating a task is not a browsing action:
     * it adds a git worktree upstream and takes a machine lease that blocks
     * every other project from that device. It deliberately cannot start the
     * task — starting spends model tokens and runs commands on a remote GPU
     * host, so it stays in the dashboard.
     *
     * Every blocker is resolved by the Host from upstream and shown before the
     * button is reachable, so an impossible combination is explained here
     * rather than returned as a 400.
     */
    function CreateTask({ t, project, onCreated }) {
      /*
       * Cached per project, including the draft.
       *
       * A half-typed goal is worth more than the fetched options: losing it to
       * a glance at another view is the sharper version of the same flicker
       * complaint. `made` is deliberately not cached — it reports what this
       * visit created, and restoring it later would announce a task the user
       * did not just make.
       */
      const scope = `create:${project.id}`
      const [open, setOpen] = useCached(scope, 'open', false)
      const [options, setOptions] = useCached(scope, 'options', null)
      const [form, setForm] = useCached(scope, 'form', { profile: '', branch: '', goal: '', provider: 'codex' })
      const [busy, setBusy] = React.useState(false)
      const [error, setError] = useCached(scope, 'error', null)
      const [made, setMade] = React.useState(null)
      /*
       * Remote-workspace entry, for a machine that has none.
       *
       * Not cached: it is a one-shot action keyed to the machine currently
       * selected, and a stale draft restored against a different machine would
       * be worse than an empty field.
       */
      const [wsDraft, setWsDraft] = React.useState('')
      const [wsBusy, setWsBusy] = React.useState(false)
      const [wsError, setWsError] = React.useState(null)

      React.useEffect(() => {
        if (!open) return undefined
        let alive = true
        callJson(`${BASE}task-options?project=${encodeURIComponent(project.id)}`)
          .then(body => {
            if (!alive) return
            setOptions(body)
            // Default to the branch the dashboard already tracks.
            setForm(current => ({ ...current, branch: current.branch || body.defaultBranch || '' }))
          })
          .catch(failure => { if (alive) setError(String(failure.message)) })
        return () => { alive = false }
      }, [open, project.id])

      const edit = (field, value) => setForm(current => ({ ...current, [field]: value }))

      const machine = (options && options.machines || []).find(row => row.id === form.profile)
      // A policy only applies when its branch matches the one being targeted.
      const policy = machine && machine.backend && options
        ? (options.policies || {})[machine.backend]
        : null
      const policyBlocks = Boolean(policy && policy.mode === 'monitor' && policy.branch === form.branch)
      const pairTaken = Boolean(machine && machine.backend && (options.takenPairs || [])
        .some(pair => pair.backend === machine.backend && pair.branch === form.branch))
      // Manual creation needs a remote workspace, which the profile supplies.
      const needsWorkspace = Boolean(machine && !machine.hasWorkspace)

      const blocked = !form.profile || !form.branch.trim() || policyBlocks || pairTaken || needsWorkspace

      const submit = async () => {
        setBusy(true)
        setError(null)
        setMade(null)
        try {
          const result = await callJson(`${BASE}create-task`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              project: project.id,
              profile: form.profile,
              branch: form.branch.trim(),
              goal: form.goal.trim(),
              provider: form.provider,
            }),
          })
          if (result && result.ok) {
            setMade(String(result.task))
            setForm(current => ({ ...current, goal: '' }))
            if (onCreated) onCreated()
          } else {
            setError(String((result && result.error) || t.createFailed))
          }
        } catch (failure) {
          // A timed-out create may have succeeded upstream and there is no
          // idempotency key, so this never retries on its own.
          setError(String(failure.message))
        } finally {
          setBusy(false)
        }
      }

      /*
       * Set the selected machine's remote workspace, then re-read the options
       * so `hasWorkspace` flips and the blocker clears in place. The path is
       * the user's to supply — the plugin never guesses a remote directory.
       */
      const saveWorkspace = async () => {
        const path = wsDraft.trim()
        if (!path.startsWith('/') || !form.profile) return
        setWsBusy(true)
        setWsError(null)
        try {
          const result = await callJson(`${BASE}set-workspace`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ project: project.id, profile: form.profile, workspace: path }),
          })
          if (result && result.ok) {
            setWsDraft('')
            // Refresh options so the saved profile now reports hasWorkspace.
            const body = await callJson(`${BASE}task-options?project=${encodeURIComponent(project.id)}`)
            setOptions(body)
          } else {
            setWsError(String((result && result.error) || t.createWorkspaceFailed))
          }
        } catch (failure) {
          setWsError(String(failure.message))
        } finally {
          setWsBusy(false)
        }
      }

      if (!open) {
        return h('div', { className: 'flup-pb3' },
          h('button', {
            className: 'flup-btn flup-btn-primary',
            type: 'button',
            onClick: () => setOpen(true),
          }, `+ ${t.createOpen}`),
        )
      }

      return h('div', { className: 'flup-card flup-card-accent' },
        h('div', { className: 'flup-line' },
          h('h3', { className: 'flup-card-title flup-flat' }, t.createTitle),
          h('span', { className: 'flup-spacer' }),
          h('button', {
            className: 'flup-btn flup-btn-sm',
            type: 'button',
            onClick: () => setOpen(false),
          }, t.createClose),
        ),
        h('p', { className: 'flup-meta flup-mt2' }, t.createIntro),

        !options && !error ? h(Skeleton, { rows: 3, label: t.loading }) : null,

        // No profile carrying a remote workspace means creation cannot work on
        // this deployment at all. Say that once, up front.
        options && !options.anyManualPossible
          ? h('p', { className: 'flup-warn flup-mt2' }, `⚠ ${t.createNoWorkspaces}`)
          : null,

        options
          ? h('div', { className: 'flup-form flup-mt3' },
            h('div', { className: 'flup-field' },
              h('span', { className: 'flup-label' }, t.createMachine),
              h('select', {
                className: 'flup-select',
                value: form.profile,
                onChange: event => edit('profile', event.target.value),
              },
                h('option', { value: '' }, `— ${t.createPickMachine} —`),
                (options.machines || []).filter(row => row.usable).map(row => h('option', {
                  key: row.id,
                  value: row.id,
                }, `${row.label}${row.backend ? ` · ${row.backend}` : ` · ${t.createNoBackend}`}`)),
              ),
            ),
            h('div', { className: 'flup-field' },
              h('span', { className: 'flup-label' }, t.createBranch),
              // A dropdown of the branches the dashboard tracks, not a free
              // text box: a typo there silently targets a branch that does not
              // exist. Falls back to just the default when the list is empty.
              (options.branches && options.branches.length)
                ? h('select', {
                  className: 'flup-select',
                  value: form.branch,
                  onChange: event => edit('branch', event.target.value),
                },
                  form.branch && !options.branches.includes(form.branch)
                    ? h('option', { value: form.branch }, form.branch)
                    : null,
                  options.branches.map(name => h('option', { key: name, value: name }, name)),
                )
                : h('input', {
                  className: 'flup-input',
                  type: 'text',
                  value: form.branch,
                  onChange: event => edit('branch', event.target.value),
                }),
            ),
            h('div', { className: 'flup-field' },
              h('span', { className: 'flup-label' }, t.createProvider),
              h('select', {
                className: 'flup-select',
                value: form.provider,
                onChange: event => edit('provider', event.target.value),
              },
                // Every provider the dashboard reports as available. A
                // provider cannot be changed after creation, so an unavailable
                // one is filtered out rather than offered and stranded.
                ['codex', 'claude', 'flagscale']
                  .filter(name => ((options.providers || {})[name] || {}).available)
                  .map(name => h('option', { key: name, value: name }, name)),
              ),
            ),
            h('div', { className: 'flup-field' },
              h('span', { className: 'flup-label' }, t.createGoal),
              h('textarea', {
                className: 'flup-input flup-textarea',
                rows: 3,
                value: form.goal,
                placeholder: t.createGoalHint,
                onChange: event => edit('goal', event.target.value),
              }),
            ),

            // The chosen machine has no remote working directory. Rather than
            // a dead-end error, offer to set it right here: one field, saved to
            // the profile, and the blocker clears without leaving the panel.
            needsWorkspace
              ? h('div', { className: 'flup-sub flup-mt2' },
                h('p', { className: 'flup-warn flup-flat' }, `⚠ ${t.createNeedsWorkspace}`),
                h('div', { className: 'flup-field flup-mt2' },
                  h('span', { className: 'flup-label' }, t.createWorkspaceLabel),
                  h('input', {
                    className: 'flup-input',
                    type: 'text',
                    value: wsDraft,
                    placeholder: t.createWorkspaceHint,
                    onChange: event => setWsDraft(event.target.value),
                  }),
                ),
                h('div', { className: 'flup-line flup-mt2' },
                  h('button', {
                    className: 'flup-btn flup-btn-sm flup-btn-accent',
                    type: 'button',
                    disabled: wsBusy || !wsDraft.trim().startsWith('/'),
                    onClick: saveWorkspace,
                  }, wsBusy ? `${t.createWorkspaceSaving}…` : t.createWorkspaceSave),
                  wsError ? h('span', { className: 'flup-err' }, wsError) : null,
                ),
                h('p', { className: 'flup-meta flup-mt2' }, t.createWorkspaceWhy),
              )
              : null,
            policyBlocks
              ? h('p', { className: 'flup-warn' },
                `⚠ ${t.createMonitorOnly}`,
                policy.pr_url
                  ? h('a', {
                    className: 'flup-link flup-inline-note',
                    href: policy.pr_url,
                    target: '_blank',
                    rel: 'noreferrer noopener',
                  }, policy.pr ? `PR #${policy.pr}` : 'PR')
                  : null,
                policy.reason ? h('span', { className: 'flup-meta' }, ` ${policy.reason}`) : null)
              : null,
            pairTaken
              ? h('p', { className: 'flup-warn' }, `⚠ ${t.createPairTaken}`)
              : null,
            machine && !machine.backendVerified
              ? h('p', { className: 'flup-meta' }, t.createBackendGuess)
              : null,

            h('p', { className: 'flup-meta' }, t.createCost),
            h('div', { className: 'flup-line' },
              h('button', {
                className: 'flup-btn flup-btn-primary',
                type: 'button',
                disabled: busy || blocked,
                onClick: submit,
              }, busy ? `${t.createRunning}…` : t.createRun),
              made ? h('span', { className: 'flup-ok' }, `✓ ${t.createDone}`) : null,
            ),
            // Creation never starts the task, so say what to do next.
            made ? h('p', { className: 'flup-meta' }, t.createNext) : null,
            error ? h('p', { className: 'flup-err' }, `${t.error}: ${error}`) : null,
          )
          : null,
        error && !options ? h('p', { className: 'flup-err' }, `${t.error}: ${error}`) : null,
      )
    }

    /**
     * Deploy view: configure the checkout and start it, without a terminal.
     *
     * The repo's own non-interactive wizard does the work. Plugin checkout
     * paths cannot be discovered, so the user supplies them here.
     */
    function DeployView({ t, onDone }) {
      const [preflight, setPreflight] = useCached('deploy', 'preflight', null)
      // The picked paths are the user's answers to setup; a glance elsewhere
      // mid-wizard should not empty the form.
      const [picked, setPicked] = useCached('deploy', 'picked', {})
      const [busy, setBusy] = React.useState(false)
      const [error, setError] = useCached('deploy', 'error', null)
      // Transcript of the run this visit performed, and whether it finished.
      // Not cached: both describe an action, not a view, and replaying them
      // on a later visit would claim a deployment just happened.
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

      /*
       * Branch and remote lists per project, for the dropdowns. Keyed by
       * project id, each entry `{ branches, remotes }`; a missing entry means
       * not loaded yet, and empty lists mean the form falls back to text input.
       */
      const [gitLists, setGitLists] = React.useState({})
      // Which ids have been requested, so a re-render never re-fetches. A ref
      // rather than state: reading it must not make the effect run again.
      const requested = React.useRef(new Set())

      /*
       * Load the lists for every checked project.
       *
       * In an effect, not in render: the fetch sets state, and setting state
       * during render queues another render on every keystroke in the form.
       */
      const pickedIds = Object.keys(picked).sort().join(',')
      React.useEffect(() => {
        let cancelled = false
        for (const id of pickedIds ? pickedIds.split(',') : []) {
          if (requested.current.has(id)) continue
          requested.current.add(id)
          callJson(`${BASE}project-branches?project=${encodeURIComponent(id)}`)
            .then(body => {
              if (cancelled) return
              setGitLists(current => ({
                ...current,
                [id]: { branches: body.branches || [], remotes: body.remotes || [] },
              }))
            })
            .catch(() => {
              if (cancelled) return
              setGitLists(current => ({ ...current, [id]: { branches: [], remotes: [] } }))
            })
        }
        return () => { cancelled = true }
      }, [pickedIds])

      /*
       * Toggle a project's selection.
       *
       * A detected project defaults to useDetected:true (the host resolves its
       * path from config) and preselects the branch and remote the monitor
       * config already names, so checking the box is usually the only step.
       * An undetected one needs a path typed in.
       */
      const toggle = id => setPicked(current => {
        const next = { ...current }
        if (next[id]) {
          delete next[id]
          return next
        }
        const project = (preflight && preflight.projects || []).find(p => p.id === id)
        const branch = project && project.configuredBranch ? project.configuredBranch : ''
        const remote = project && project.configuredRemote ? project.configuredRemote : 'origin'
        next[id] = project && project.detected
          ? { useDetected: true, remote, branch }
          : { repoPath: '', remote, branch }
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
      const complete = chosen.length > 0 && chosen.every(([id, value]) => {
        const project = (preflight && preflight.projects || []).find(p => p.id === id)
        const pathOk = (project && project.detected) ? true : Boolean(value.repoPath && value.repoPath.trim())
        return pathOk && Boolean(value.branch && value.branch.trim())
      })

      /*
       * Once the dashboard is configured, the wizard has nothing left to ask
       * and the credentials are the only outstanding step, so they lead. The
       * configure card stays below, collapsed into a one-line summary: it is
       * how a deployment gets repaired or repointed later, but it is no longer
       * what the screen is about.
       */
      const deployed = Boolean(check('configured') && check('configured').ok)

      return h('div', { className: 'flup-body' },
        deployed ? h(CredentialView, { t, project: (preflight && preflight.projects || [])[0] }) : null,
        h('div', { className: 'flup-card' },
          h('h3', { className: 'flup-card-title' }, t.deployTitle),
          h('p', { className: 'flup-meta' }, deployed ? t.deployRedoIntro : t.deployIntro),
          h('p', { className: 'flup-meta flup-mt2' },
            `${t.reqRepo}: `,
            h('span', { className: repoCheck && repoCheck.ok ? 'flup-ok' : 'flup-err' },
              repoCheck && repoCheck.ok ? t.reqReady : t.reqMissing),
            ' · ',
            `${t.reqConfig}: `,
            h('span', { className: check('configured') && check('configured').ok ? 'flup-ok' : 'flup-meta' },
              check('configured') && check('configured').ok ? t.deployAlready : t.reqMissing),
          ),
          !preflight ? h(Skeleton, { rows: 3, label: t.loading }) : null,
          /*
           * What is wrong, and where to fix it.
           *
           * `repoPath` lives in the plugin's own configuration, which is a
           * file most people have never opened, so naming the setting alone
           * leaves them stuck. The route reports which of the two distinct
           * failures happened — no path at all, or a path that is not this
           * project — and each gets its own instruction.
           */
          preflight && repoCheck && !repoCheck.ok
            ? h('div', { className: 'flup-warn flup-warn-err' },
              h('p', { className: 'flup-warn-lead' }, t.deployNeedRepo),
              h('p', { className: 'flup-meta flup-mt2' }, t.deployWhereConfig),
              // The clone is the actual first step, so give the command rather
              // than leaving the user to find the repository themselves. Shown
              // as copyable text: the plugin never runs a clone itself.
              h('code', { className: 'flup-code' },
                'git clone https://github.com/physics31415926/fl-plugin-upgrade-workflow'),
            )
            : null,
          preflight && repoCheck && repoCheck.ok && scriptCheck && !scriptCheck.ok
            ? h('div', { className: 'flup-warn flup-warn-err' },
              h('p', { className: 'flup-warn-lead' }, t.deployNeedScript),
              // The directory name is the one identifying detail the browser
              // is allowed, and it is what tells the user they picked wrong.
              repoCheck.detail
                ? h('p', { className: 'flup-meta flup-mt2' },
                  `${t.deployFoundDir}: `, h('code', { className: 'flup-code-inline' }, String(repoCheck.detail)))
                : null,
            )
            : null,
        ),

        preflight && scriptCheck && scriptCheck.ok
          ? h('div', { className: 'flup-card' },
            h('p', { className: 'flup-meta' }, t.deployPickProjects),
            h('div', { className: 'flup-form flup-mt3' },
              (preflight.projects || []).map(project => {
                const p = picked[project.id]
                const lists = gitLists[project.id] || { branches: [], remotes: [] }
                const projectBranches = lists.branches
                const projectRemotes = lists.remotes
                return h('div', { key: project.id, className: 'flup-mt2' },
                  h('label', { className: 'flup-check' },
                    h('input', {
                      type: 'checkbox',
                      checked: Boolean(p),
                      onChange: () => toggle(project.id),
                    }),
                    h('span', null, String(project.label),
                      // Show a detected badge so the user knows the path was
                      // found automatically and no typing is needed.
                      // flup-tag-active / flup-tag-warn are the badge modifiers;
                      // flup-warn is a block-level callout and flup-ok sets its
                      // own font-size, so neither belongs on an inline badge.
                      project.detected
                        ? h('span', { className: 'flup-tag flup-tag-active' }, `✓ ${t.deployDetected}`)
                        : h('span', { className: 'flup-tag flup-tag-warn' }, t.deployNotDetected),
                    ),
                  ),
                  p
                    ? h('div', { className: 'flup-grid flup-mt2 flup-nest' },
                      /*
                       * Path: skip the input when the host already knows it.
                       * A detected project sends useDetected:true in the
                       * payload; the host resolves the path from its own
                       * config and the browser never sees an absolute path.
                       */
                      !project.detected
                        ? h('div', { className: 'flup-field' },
                          h('span', { className: 'flup-label' }, t.deployRepoPath),
                          h('input', {
                            className: 'flup-input',
                            type: 'text',
                            value: p.repoPath || '',
                            placeholder: project.repo,
                            onChange: event => edit(project.id, 'repoPath', event.target.value),
                          }),
                        )
                        : h('p', { className: 'flup-meta' },
                          `${t.deployUsingDetected}: `,
                          h('b', null, project.detectedName || project.label),
                        ),
                      /*
                       * Branch: a dropdown when the list is available, a text
                       * input otherwise (no git, or list failed to load).
                       */
                      h('div', { className: 'flup-field' },
                        h('span', { className: 'flup-label' }, t.deployBranch),
                        projectBranches.length > 0
                          ? h('select', {
                            className: 'flup-input',
                            value: p.branch,
                            onChange: event => edit(project.id, 'branch', event.target.value),
                          },
                          h('option', { value: '' }, t.deployBranchPlaceholder),
                          // A configured branch missing from the list would
                          // otherwise render as a blank selection.
                          p.branch && !projectBranches.includes(p.branch)
                            ? h('option', { value: p.branch }, p.branch)
                            : null,
                          projectBranches.map(b => h('option', { key: b, value: b }, b)),
                          )
                          : h('input', {
                            className: 'flup-input',
                            type: 'text',
                            value: p.branch,
                            placeholder: t.deployBranchPlaceholder,
                            onChange: event => edit(project.id, 'branch', event.target.value),
                          }),
                      ),
                      /*
                       * Remote: a dropdown of the remotes the checkout really
                       * has. The monitor config names one, but it is not always
                       * present in the checkout, so the list is read from git
                       * and the configured name is only the default.
                       */
                      h('div', { className: 'flup-field' },
                        h('span', { className: 'flup-label' }, t.deployRemote),
                        projectRemotes.length > 0
                          ? h('select', {
                            className: 'flup-input',
                            value: p.remote || '',
                            onChange: event => edit(project.id, 'remote', event.target.value),
                          },
                          // Keep a configured-but-absent remote selectable
                          // rather than silently switching it to another one.
                          projectRemotes.includes(p.remote)
                            ? null
                            : h('option', { value: p.remote || '' }, p.remote || ''),
                          projectRemotes.map(r => h('option', { key: r, value: r }, r)),
                          )
                          : h('input', {
                            className: 'flup-input',
                            type: 'text',
                            value: p.remote || '',
                            onChange: event => edit(project.id, 'remote', event.target.value),
                          }),
                      ),
                    )
                    : null,
                )
              }),
              h('div', { className: 'flup-mt3' },
                h('button', {
                  className: 'flup-btn flup-btn-primary',
                  type: 'button',
                  disabled: busy || !complete,
                  onClick: submit,
                }, busy ? h(Pending, { label: t.deployRunning }) : t.deployRun),
              ),
              done ? h('p', { className: 'flup-ok' }, t.deployDone) : null,
              error ? h('p', { className: 'flup-err' }, `${t.error}: ${error}`) : null,
              output ? h('pre', { className: 'flup-pre' }, output) : null,
            ),
          )
          : null,

        // Credentials are independent of configure: the repo's own wizard
        // accepts none, so they are collected separately or not at all. Shown
        // after the wizard only before a deployment exists, where configuring
        // is the first step; afterwards they lead the screen instead.
        deployed ? null : h(CredentialView, { t, project: (preflight && preflight.projects || [])[0] }),
      )
    }

    /**
     * Credential readiness, and a write-only field for the two secrets the
     * dashboard can actually store.
     *
     * Each value is handed straight to the dashboard's own sink, which seals it
     * with Windows DPAPI under the current user inside a git-ignored directory
     * and reports it back only as a boolean. Nothing is kept here: the field is
     * cleared on submit, never echoed, and never written to browser storage.
     *
     * A GitHub token has no field on purpose. The dashboard reads it from its
     * own process environment and has nowhere to put one, so a form would mean
     * inventing persistence for a secret. The card explains `gh auth login`
     * instead, which takes precedence over the variable anyway.
     *
     * An SSH key has no field either. That design references key FILES by path
     * and never reads their bytes, so accepting key material would break an
     * invariant the dashboard currently holds.
     */
    function CredentialView({ t, project }) {
      /*
       * Deliberately NOT cached, unlike every other view here.
       *
       * `draft` holds a token or a password in plaintext. Caching it would
       * keep that string in a module-level map for the lifetime of the page,
       * long after the form was abandoned, so an unsubmitted secret would
       * outlive the view that collected it. Retyping it is the correct cost.
       *
       * `state` only reports which credentials are configured, so caching it
       * would be safe, but it is left alone too: the whole view fits one
       * fetch, and a card that shows a stale "configured" after a save
       * elsewhere is worse than its skeleton.
       */
      const [state, setState] = React.useState(null)
      const [draft, setDraft] = React.useState({ claude: '', jumpserver: '', github: '' })
      const [remember, setRemember] = React.useState(false)
      const [busy, setBusy] = React.useState('')
      const [error, setError] = React.useState(null)
      const [saved, setSaved] = React.useState('')
      /*
       * Which already-configured secrets the user has opened for replacement.
       *
       * A configured credential hides its field, so "done" reads as done. But
       * keys get rotated and passwords get mistyped, and the earlier version
       * left no way back: once configured, the only route was the dashboard or
       * the filesystem. This opt-in reveal keeps the finished state quiet while
       * making replacement reachable.
       *
       * Not cached, for the same reason the draft is not: this governs a form
       * holding a plaintext secret, and reopening the panel should start clean.
       */
      const [replacing, setReplacing] = React.useState({})
      /*
       * Whether a saved GitHub token still needs a dashboard restart.
       *
       * The running process holds the environment it was spawned with, so a
       * token stored after it started is not visible to collection yet. The
       * Host reports this per save; keeping it in state means the notice
       * survives until the card is left.
       */
      const [restart, setRestart] = React.useState(false)

      const projectId = project && project.id

      const load = React.useCallback(async () => {
        if (!projectId) return
        try {
          setState(await callJson(`${BASE}credentials?project=${encodeURIComponent(projectId)}`))
        } catch (failure) {
          setError(String(failure.message))
        }
      }, [projectId])

      React.useEffect(() => { load() }, [load])

      const submit = async kind => {
        const secret = draft[kind]
        if (!secret) return
        setBusy(kind)
        setError(null)
        setSaved('')
        try {
          const result = await callJson(`${BASE}credential`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            // The secret travels in the body: the dashboard logs request lines
            // to a plaintext file, so a query parameter would land on disk.
            body: JSON.stringify({
              project: projectId,
              kind,
              secret,
              remember: kind === 'jumpserver' ? remember : false,
            }),
          })
          // Clear immediately, whatever happened next.
          setDraft(current => ({ ...current, [kind]: '' }))
          if (result && result.state) setState(result.state)
          if (result && result.ok) {
            setSaved(kind)
            if (result.restartRequired) setRestart(true)
            // Collapse the replacement form: the value it existed to change
            // has been changed, and leaving an empty secret box open invites
            // typing into a field that no longer has a purpose.
            setReplacing(current => ({ ...current, [kind]: false }))
          } else {
            // Keep the form open on failure — the user has to retype anyway,
            // and closing it would hide the only way to retry.
            setError(String((result && result.error) || t.credFailed))
          }
        } catch (failure) {
          setDraft(current => ({ ...current, [kind]: '' }))
          setError(String(failure.message))
        } finally {
          setBusy('')
        }
      }

      if (!projectId) return null

      /** One readiness line: a coloured dot, a verdict, and why it matters. */
      const row = (label, ok, detail, warn) => h('p', { className: 'flup-meta flup-line flup-row' },
        h('span', {
          className: 'flup-dot',
          style: { background: ok ? 'var(--dsw-alias-state-success-primary)' : warn ? 'var(--dsw-alias-state-warn-primary)' : 'var(--dsw-alias-state-error-primary)' },
          'aria-hidden': true,
        }),
        h('b', null, label),
        h('span', null, detail),
      )

      const claude = state && state.claude
      const jump = state && state.jumpserver
      const jumpMatters = state && state.machineSource === 'jumpserver'
      const github = state && state.github
      // Either way in authenticates GitHub reads. `gh` is what the dashboard
      // tries first; a token stored in our own store is the fallback.
      const ghCli = Boolean(github && github.cli && github.cli.authenticated === true)
      const ghStored = Boolean(github && github.supported && github.configured && !github.ambient)

      /** A write-only secret field with its own save button. */
      const field = (kind, label, hint, extra) => h('div', { className: 'flup-mt3' },
        h('div', { className: 'flup-field' },
          h('span', { className: 'flup-label' }, label),
          h('input', {
            className: 'flup-input',
            type: 'password',
            value: draft[kind],
            autoComplete: 'off',
            spellCheck: false,
            placeholder: t.credPlaceholder,
            onChange: event => setDraft(current => ({ ...current, [kind]: event.target.value })),
          }),
        ),
        extra,
        h('div', { className: 'flup-line flup-mt2' },
          h('button', {
            className: 'flup-btn flup-btn-sm flup-btn-accent',
            type: 'button',
            disabled: Boolean(busy) || !draft[kind].trim(),
            onClick: () => submit(kind),
          }, busy === kind ? `${t.credSaving}…` : t.credSave),
          // Abandoning a replacement must also drop what was typed: leaving it
          // in `draft` would keep a secret in memory for a form the user
          // closed.
          replacing[kind]
            ? h('button', {
              className: 'flup-btn flup-btn-sm',
              type: 'button',
              disabled: Boolean(busy),
              onClick: () => {
                setDraft(current => ({ ...current, [kind]: '' }))
                setReplacing(current => ({ ...current, [kind]: false }))
              },
            }, t.credCancel)
            : null,
          saved === kind ? h('span', { className: 'flup-ok' }, `✓ ${t.credSaved}`) : null,
        ),
        h('p', { className: 'flup-meta flup-mt2' }, hint),
      )

      /**
       * One collectable secret: its verdict, and a way to change it.
       *
       * Three states, because "stored" and "working" are different facts and
       * conflating them was the bug. A secret can be `configured` while the
       * backend still refuses it — a mistyped key is exactly that — and the
       * earlier version rendered a tick for it, hiding the one case where
       * retyping is most needed.
       */
      const slot = (kind, ready, label, hint, doneText, extra, replaceLabel) => {
        if (!ready) return field(kind, label, hint, extra)
        if (replacing[kind]) return field(kind, label, hint, extra)
        // Done, and quiet about it: a standing secret field would make a
        // finished setup look unfinished. Replacement is one click away.
        return h('div', { className: 'flup-line flup-mt3' },
          h('span', { className: 'flup-meta' }, `✓ ${doneText}`),
          h('button', {
            className: 'flup-btn flup-btn-sm',
            type: 'button',
            disabled: Boolean(busy),
            onClick: () => {
              setSaved('')
              setReplacing(current => ({ ...current, [kind]: true }))
            },
          }, replaceLabel || t.credReplace),
        )
      }

      return h('div', { className: 'flup-card flup-card-accent' },
        h('h3', { className: 'flup-card-title' }, t.credTitle),
        h('p', { className: 'flup-meta' }, t.credIntro),

        !state ? h(Skeleton, { rows: 3, label: t.loading }) : null,

        state
          ? h('div', { className: 'flup-mt3' },
            // Codex needs no input here: it authenticates through its own CLI.
            state.codex
              ? row('Codex', state.codex.authenticated, state.codex.authenticated ? t.credCodexOk : t.credCodexNo)
              : null,
            claude
              ? row('Claude', claude.configured && claude.available,
                claude.configured
                  ? (claude.available ? t.credClaudeOk : claude.error || t.credUnavailable)
                  : t.credClaudeNo,
                claude.configured && !claude.available)
              : null,
            // Configured but unavailable is a real state here, so it is warned
            // rather than failed: nothing the user types fixes a missing
            // interpreter package.
            state.flagscale
              ? row('FlagScale', state.flagscale.available,
                state.flagscale.available ? t.credReady : (state.flagscale.error || t.credUnavailable),
                true)
              : null,
            jumpMatters && jump
              ? row('JumpServer', jump.authenticated,
                jump.authenticated
                  ? `${t.credJumpOk} (${jump.assetCount})`
                  : jump.error || t.credJumpNo)
              : null,
            /*
             * GitHub, which has two ways in. The dashboard reads through a
             * signed-in `gh` CLI first and falls back to the token, so either
             * one alone authenticates collection. The row is green when either
             * is present, and names which.
             */
            ghCli
              ? row('GitHub', true,
                ghStored ? `${t.credGithubCli} · ${t.credGithubCliAndToken}` : t.credGithubCli)
              : github && github.supported
                ? row('GitHub', github.configured,
                  github.configured
                    ? (github.ambient ? `${t.credGithubAmbient} (${github.source || '—'})` : t.credGithubStored)
                    : t.credGithubNone,
                  Boolean(github.ambient) || !github.configured)
                : row('GitHub', false, t.credGithubUnknown, true),
          )
          : null,

        /*
         * Only the secrets the dashboard can store get a field.
         *
         * Claude counts as done only when the key is both stored AND accepted.
         * `configured && !available` is a real and common state — a mistyped
         * or revoked key — and treating it as done hid the field precisely
         * when retyping was the fix.
         */
        claude
          ? slot('claude', claude.configured && claude.available,
            t.credClaudeLabel, t.credClaudeHint, t.credClaudeDone)
          : null,

        jumpMatters && jump
          ? slot('jumpserver', jump.authenticated, t.credJumpLabel, t.credJumpHint, t.credJumpDone,
            jump.canRemember
              ? h('label', { className: 'flup-check flup-mt2' },
                h('input', {
                  type: 'checkbox',
                  checked: remember,
                  onChange: event => setRemember(event.target.checked),
                }),
                t.credRemember)
              : null)
          : null,

        /*
         * The GitHub token field is always offered when the credential
         * service is present, never gated on a writable flag. The service's
         * `set` always writes its own store and a stored value overrides any
         * ambient one, so there is no state in which saving is impossible —
         * gating the field was exactly what left users unable to configure it.
         *
         * "Done" means the token lives in our managed store. A value that is
         * only ambient (the shell exported GITHUB_TOKEN) still shows the field,
         * with a note that saving here overrides it, rather than a dead tick.
         */
        /*
         * A signed-in `gh` makes the token optional, so the field collapses
         * to a done line with an "add a token anyway" button rather than
         * asking for something the user does not need.
         */
        github && github.supported
          ? slot('github', ghStored || ghCli,
            t.credGithubLabel,
            github.ambient ? t.credGithubAmbientHint : t.credGithubFieldHint,
            ghStored ? t.credGithubDone : t.credGithubCliDone,
            undefined,
            ghStored ? undefined : t.credGithubAddToken)
          : null,

        // Collection reads the environment it was started with, so a token
        // saved now applies after a restart. Saying so avoids "I saved it and
        // nothing changed".
        restart ? h('p', { className: 'flup-warn flup-mt2' }, t.credGithubRestart) : null,

        h('p', { className: 'flup-meta flup-mt3' }, t.credGithubHint),
        h('code', { className: 'flup-code' }, 'gh auth login'),
        h('p', { className: 'flup-meta flup-mt3' }, t.credSshHint),
        h('p', { className: 'flup-meta flup-mt3' }, t.credSafety),
        error ? h('p', { className: 'flup-err' }, `${t.error}: ${error}`) : null,
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
      // Cached so reopening the sidebar restores the panel as it was, rather
      // than replaying the full-pane skeleton while `state` is null.
      const [state, setState] = useCached('panel', 'state', null)
      const [projects, setProjects] = useCached('panel', 'projects', [])
      const [selected, setSelected] = useCached('panel', 'selected', null)
      const [error, setError] = useCached('panel', 'error', null)
      const [busy, setBusy] = React.useState(false)
      const [restarting, setRestarting] = React.useState(false)
      const [setup, setSetup] = useCached('panel', 'setup', null)

      const load = React.useCallback(async () => {
        try {
          const next = await callJson(`${BASE}state`)
          setState(next)
          setError(null)
          // Whether setup is still outstanding decides whether its entry
          // exists. Null until the answer arrives, so the rail does not flash
          // an entry that is about to disappear.
          const outstanding = await callJson(`${BASE}setup-state`).catch(() => null)
          setSetup(outstanding)
          const list = await callJson(`${BASE}projects`).catch(() => null)
          const rows = (list && Array.isArray(list.projects)) ? list.projects : []
          setProjects(rows)
          setSelected(current => {
            // The entries that are not projects keep their selection across a
            // reload: they do not depend on the project list at all.
            if (current === 'devices') return current
            // Setup is the exception: once it is finished its entry is gone,
            // so staying on it would leave the user on an unreachable view.
            if (current === 'deploy') {
              if (!outstanding || !outstanding.complete) return current
              return rows.length ? rows[0].id : 'devices'
            }
            if (current && rows.some(row => row.id === current)) return current
            if (rows.length) return rows[0].id
            return outstanding && outstanding.complete ? 'devices' : 'deploy'
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
      const code = state && state.code ? state.code : null
      const project = projects.find(row => row.id === selected) || null
      // A first-run clone of the dashboard is in flight; say so rather than
      // showing a bare "not configured" while it fetches.
      const cloning = Boolean(state && state.clone && state.clone.phase === 'cloning')
      const cloneError = state && state.clone && state.clone.phase === 'error' ? state.clone.error : null

      return h('div', { className: 'flup-root' },
        // Component-local stylesheet: unmounting the panel removes it.
        h('style', null, CSS),
        h('div', { className: 'flup-head' },
          h('h2', { className: 'flup-title' }, t.heading),
          h('span', { className: 'flup-spacer' }),
          h('span', { className: 'flup-state', title: adopted ? t.adoptedHint : (cloneError || undefined) },
            h(Dot, { status: running ? 'online' : (cloning ? 'idle' : (configured ? 'idle' : 'unknown')) }),
            busy
              ? (running ? t.stopping : t.starting)
              : (cloning ? t.cloning
                : (adopted ? t.adopted : (running ? t.running : (configured ? t.stopped : t.notConfigured)))),
          ),
          // Whether collection is delivering, which the process indicator
          // above cannot say: the server answers while a fan-out fails.
          h(CollectionBadge, { collection: state && state.collection, running, t }),
          // An externally started dashboard offers no lifecycle button: the
          // plugin does not own that process.
          adopted ? null : h('button', {
            className: 'flup-btn flup-btn-primary',
            type: 'button',
            disabled: busy || !configured,
            onClick: () => lifecycle(running ? 'stop' : 'start'),
          }, running ? t.stop : t.start),
          /*
           * Reload discards the cache first.
           *
           * Pressing it means "I do not trust what is on screen", so serving
           * the cached copy back would defeat the button. Every view refetches
           * on its next render; the data the cache exists to smooth over is
           * exactly what the user is asking to replace.
           */
          h('button', {
            className: 'flup-btn',
            type: 'button',
            disabled: busy,
            onClick: () => { dropCache(); load() },
          }, t.reload),
          /*
           * Restart, offered only when the running process is behind the code
           * on disk. Works for an adopted dashboard too: it re-executes itself
           * with its own arguments, so the plugin never kills a process it
           * does not own.
           */
          code && code.stale ? h('button', {
            className: 'flup-btn flup-btn-primary',
            type: 'button',
            disabled: busy,
            title: t.codeStaleHint(code.changedCount, code.startedHead, code.currentHead),
            onClick: async () => {
              setRestarting(true)
              try { await lifecycle('restart'); dropCache() } finally { setRestarting(false) }
            },
          }, restarting ? t.restarting : t.restart) : null,
          // Plugin self-update. Renders nothing unless a newer release exists,
          // so it does not compete with the dashboard controls day to day.
          h(UpdateButton, { t }),
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
        // Stale code is easy to miss in a tooltip, so it also gets a line.
        code && code.stale ? h('div', { className: 'flup-sub', role: 'status' },
          h('span', { className: 'flup-tag flup-tag-warn' }, t.codeStale),
          ' ',
          t.codeStaleHint(code.changedCount, code.startedHead, code.currentHead),
        ) : null,

        h('div', { className: 'flup-split' },
          h('nav', { className: 'flup-rail', 'aria-label': t.heading },
            projects.map(row => h('button', {
              key: String(row.id),
              className: 'flup-rail-btn',
              type: 'button',
              'aria-current': selected === row.id,
              onClick: () => setSelected(row.id),
            },
              // No branch here: everything the project view shows follows the
              // branch picked inside it, so a fixed branch label would mislead.
              h('span', null, String(row.label || row.id)),
            )),
            !projects.length && state ? h('p', { className: 'flup-empty flup-rail-empty' }, t.noProjects) : null,
            /*
             * The GPU fleet, below a divider.
             *
             * It sits outside the project list on purpose. The entries above
             * each scope the view to one project and one branch; the fleet is
             * shared by all of them and changes with neither. Keeping it here
             * rather than as a third tab inside a project means it is never
             * shown underneath a branch picker that does not apply to it.
             */
            projects.length ? h('span', { className: 'flup-rail-divider', role: 'separator' }) : null,
            h('button', {
              className: 'flup-rail-btn',
              type: 'button',
              'aria-current': selected === 'devices',
              onClick: () => setSelected('devices'),
            },
              h('span', null, t.tabDevices),
              h('span', { className: 'flup-rail-sub' }, t.devicesRailSub),
            ),
            /*
             * Setup, only while setup is unfinished.
             *
             * It used to sit here permanently, which made a solved problem
             * occupy a rail slot forever. Once the dashboard runs and the
             * collectable credentials are in place there is nothing to do in
             * it, so it goes away — and because it goes away, "finished" has
             * to include the credentials: a deployment that runs but cannot
             * start a task would otherwise hide its own remaining step.
             *
             * Still reachable afterwards from the running view, for the case
             * where something breaks later.
             */
            setup && !setup.complete
              ? h('button', {
                className: `flup-rail-btn ${configured ? 'flup-rail-cta' : 'flup-rail-cta-urgent'}`,
                type: 'button',
                'aria-current': selected === 'deploy',
                onClick: () => setSelected('deploy'),
                style: { marginTop: 'auto' },
              },
                h('span', null, t.deployTitle),
                h('span', { className: 'flup-rail-sub' },
                  configured ? t.deployRailCreds : t.deployRailSetup),
              )
              : null,
            // After setup the entry is gone, so this is the way back to it.
            setup && setup.complete && selected !== 'deploy'
              ? h('button', {
                className: 'flup-rail-link',
                type: 'button',
                onClick: () => setSelected('deploy'),
                style: { marginTop: 'auto' },
              }, t.deployRailDone)
              : null,
          ),

          /*
           * Each branch carries a key, so switching entries remounts the body
           * and its slide-in runs again. Without distinct keys React reuses
           * the same element across a switch, the animation never restarts,
           * and the pane changes content with no transition at all.
           */
          h('div', { className: 'flup-pane' },
            !state
              ? h('div', { className: 'flup-body', key: 'loading' }, h(Skeleton, { rows: 5, label: t.loading }))
              : selected === 'deploy'
                ? h(DeployView, { key: 'deploy', t, onDone: load })
                : selected === 'devices'
                  // Any project reaches the same fleet, so the first one is
                  // passed purely to satisfy the route's namespacing.
                  ? h(DeviceView, { key: 'devices', t, project: projects[0] || null, running })
                  : project
                    ? h(ProjectView, { key: project.id, project, running, t })
                    : h('div', { className: 'flup-body', key: 'empty' }, h('p', { className: 'flup-empty' }, t.noProjects)),
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

    /*
     * A seam for the render tests, which load this file into a `vm` context
     * and assert on the tree each component produces. The components are pure
     * functions of their props and hold no credential, so exposing them costs
     * nothing; it is what lets the blocking rules be tested without a browser.
     */
    globalThis.__probe = { TaskDetail, ProjectView, CreateTask, CredentialView, TaskTable, RepoCard, dictFor, Dot, Skeleton, Pending, DeviceView, DeviceTable, shortBackend, CollectionBadge, collectionVerdict, VIEW_CACHE, dropCache, useCached, UpdateButton }

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
