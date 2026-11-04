# FL 适配工作台 (DSH 插件)

把 [fl-plugin-upgrade-workflow](https://github.com/physics31415926/fl-plugin-upgrade-workflow)
看板搬进 DeepSeek Harness 侧边栏：原生 React 面板，跟随主题和语言，不用 iframe。

左侧一个「FL 适配」图标，面板内部再分项目（vLLM-Plugin-FL / SGLang-Plugin-FL）。

## 安装

```
dsh plugin install ./deepseek-fl-upgrade-workflow-0.1.0.tgz
```

或在 Harness 里用插件管理安装这个 tgz。安装后必须填 `repoPath`：

| 配置项 | 说明 |
| --- | --- |
| `repoPath` | **必填**，fl-plugin-upgrade-workflow 检出的绝对路径 |
| `port` | 看板监听的回环端口，默认 `8766` |
| `pythonPath` | 留空则用 PATH 上的 `python`，需要 3.11+ |
| `projectsConfig` | 留空则依次找 `runs/configured/projects.json`、`projects.json` |
| `autoStart` | 插件激活时是否自动拉起看板进程，默认 `false` |

```
git clone https://github.com/physics31415926/fl-plugin-upgrade-workflow
```

## 面板能做什么

- 项目切换、分支切换（分支列表来自 `/api/branches`，每个项目独立）
- 后端视图：合入状态、CI、验收、门禁通过数
- 设备视图：机器、后端、空闲卡
- 任务视图：适配任务标题、分支、状态、来源。上游接口会忽略 `branch` 参数，
  所以按目标分支分组是在面板里做的，chip 上带每个分支的任务数
- 任务详情：点任务进入，看 agent 的计划、最新消息、进展摘要和对话记录，
  并可发送后续消息。详情页按 5 秒（执行中）/ 20 秒轮询，标签页隐藏时暂停
- 「重新采集」手动触发一次采集
- 「在本机部署」：勾选项目、填本地插件检出路径和目标分支，一键配置并启动看板

### 关于 Agent 后端

看板支持 `codex`、`claude`、`flagscale` 三个后端，但**后端在创建任务时确定，
之后无法更改**（上游没有任何接口可以改写已有会话的 provider）。所以详情页把它
显示为状态而不是选择器，并附上该后端当前是否可用。要换后端只能新建任务。

面板只允许「给已存在的会话发消息」。创建会话、启动、删除、恢复、转交桌面端
这些动作要么会在远程机器上花钱，要么会移动历史，全部留在看板里，不做代理。
下面这些情况面板会直接说明原因而不是让你发送后失败：

- 任务是原生观察记录（`native-*`），没有对应会话
- 后端在该分支是 `monitor` 模式（上游会拒绝 start 和 message）
- 任务已转交 Codex 桌面端
- 任务 `controllable` 为 false

另外 agent 等待审批时会在详情页顶部提示 —— 否则任务看起来就是卡住了。审批本身
请在看板里处理。

部署按钮不重新实现配置逻辑，它调用仓库自带的
`scripts/configure.py --non-interactive`，再启动 `scripts/flflow.py serve`。

## 外部依赖

插件本身零依赖（只用 Node 内置模块和 Harness 注入的 React）。但**它是本机 Python
看板的前端**，所以仍然需要：

必需
- 一份 fl-plugin-upgrade-workflow 检出
- Python 3.11+（看板纯标准库，没有 requirements.txt）
- Git（配置向导和分支监控都要调 git）

可选，缺了会降级而不是报错
- `gh` 或 `GITHUB_TOKEN` / `GH_TOKEN`：读 PR 和 CI 状态。没有就走匿名 GitHub API
  （60 次/小时/IP），PR、CI 列可能显示 `unknown`，原因会进 `errors`，请求本身仍然成功
- SSH / JumpServer 凭据：设备视图要靠它才有机器。没有就是 200 空列表
- Claude / FlagScale API key：执行适配任务才需要。任务视图没有数据时为空

没有任何凭据时，项目、分支、后端状态、门禁这些核心视图都正常工作。

## 安全边界

- 看板只绑 `127.0.0.1`，**本身没有任何认证**，靠回环绑定加 `Host`/`Origin` 同源校验
  保护。插件不会把它暴露到 `0.0.0.0`
- 浏览器不能直连看板（`Host` 校验会 403），所有读取都走插件在 Harness web server 上
  的 `/fl-upgrade/*` 代理，由它改写 `Host`
- 代理只放行一份只读白名单。`/api/codex-sessions`、`/api/jumpserver`、
  `/api/access-shares`、`/api/agent-settings`、`/api/logs`、`/api/status` 全部拒绝：
  它们带 SSH 别名、机器身份、凭据位置和绝对路径
- 放行的响应在出站前会裁剪成决策需要的字段，顺带丢掉 `branch_ref`、
  `gate.source_thread`、`gate.audit_sources`、`acceptance.description_blocks`，
  以及未经脱敏的 `errors[]` 原文
- **自由文本会额外脱敏**。`last_agent_message`、`progress_summary` 这些是 agent
  自己写的，会嵌本机绝对路径，字段级白名单管不住，所以对文本本身做替换：
  `C:\...` / `C:/...`、UNC、`/root/`、`/home/`、`/Users/`、`user@host`、IPv4
  全部替换成 `[path]` / `[host]`
- 对话记录只放行 `params.delta` 和 `params.item.text` 两个字段。同级的
  `command`、`cwd`、`aggregatedOutput`、`diff`、`agentPath`、`scriptPath`、
  `commandActions[]`、execpolicy 修正里有绝对路径、捕获的 stdout 和原始 diff，
  一概不出站；没有正文的事件只显示一个本地化标签。账号额度和 token 用量这类
  纯遥测事件直接丢掉
- 注意：上游的 `?view=cards` **不是脱敏**，它只少了 `events` 和 `prompt`，
  `machine_profiles`、`task_devices`、`identity_file` 照旧都在。所以插件没有用它
- 创建任务和填凭据的路由只留给人在看板里操作，不进 agent 工具。唯一能驱动 agent 的
  是 `task-message`，它会去上游核对任务是否真的可控（`controllable`、
  `task_policy.mode`、`execution_owner`、会话 id 格式），而不是相信浏览器传来的值
- `/fl-upgrade/*` 对任何能访问 Harness 端口的本机进程可见，白名单是主要防线。
  `deploy` 能起进程并写 `runs/configured/`，暴露到非回环地址前请先评估

## agent 工具

`fl_upgrade_dashboard`，动作 `status` / `start` / `stop` / `read`。`read` 走同一份
白名单和同一套裁剪，默认返回摘要；`full: true` 才给原始响应。

## 已知限制

- 看板每个项目最多缓存 12 个分支且不淘汰，第 13 个分支返回 400。面板会提示重启看板
- 首次看某个分支会先拿到占位数据（`has_snapshot: false`），后台采集完才有内容
- 「重新采集」会在被监控仓库里跑 `git fetch` 更新远端跟踪引用，并且是同步的，慢
- 看板进程不是完全离线：分支监控每分钟最多一次 `git fetch`，设备探测线程 300 秒一轮
- 任务接口冷缓存时要 40–50 秒（预热后约 2 秒），这类慢接口超时放宽到 90 秒
- 对话记录只能往旧翻，上游没有「某时间之后的事件」接口。所以实时刷新是重读最新一页，
  不是增量追加；每次调用的服务端开销与页大小无关（固定读尾部 256 KiB）
- 审批需要在看板里处理，面板只提示有待审批
- 实测只覆盖了 `codex` 后端的会话。`claude` 已配置但没有会话，`flagscale` 当前不可用；
  其中 FlagScale 声明 `supports_mid_turn_steer: false`，对运行中的任务发消息会是新一轮
