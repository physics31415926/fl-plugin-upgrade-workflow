# FL 适配工作台 (DSH 插件)

把 [fl-plugin-upgrade-workflow](https://github.com/physics31415926/fl-plugin-upgrade-workflow)
看板搬进 DeepSeek Harness 侧边栏：原生 React 面板，跟随主题和语言，不用 iframe。

左侧一个「FL 适配」图标，面板内部再分项目（vLLM-Plugin-FL / SGLang-Plugin-FL）。

## 安装

在界面里装：左侧边栏点**插件** → **添加插件**，在「包名或地址」里填 `.tgz` 的**绝对
路径**，再点**安装**。那是个文本框而不是文件选择器，路径需要自己粘进去。相对路径会被
拒绝；只填文件名会被当成 npm 包名，报「未找到」而不是路径错误。

也可以用命令行：

```
dsh plugin install /绝对路径/local-fl-upgrade-workflow-0.1.0.tgz
```

安装后必须填 `repoPath`：

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
- 新建任务：面板内直接准备一个适配任务，**只准备不启动**（见下）
- 项目仓库：按看板配置自动识别检出目录，一键加入左侧工作区列表，
  并在该工作区里新建一个带好上下文的对话
- 凭据：显示 Claude / JumpServer / GitHub / SSH 各自是否就绪，能填的那两个就地填

### 新建任务为什么不帮你启动

上游的 `POST /api/codex-sessions` 带 `start: true` 时会**同时创建并启动**，也就是
一次请求就开始烧模型额度、在远端机器上执行命令。所以插件在任何输入下都不会转发
`start`，启动这一步留在看板里，那边能看到完整上下文。

即使只是「准备」也不是零成本：上游会在看板仓库里 `git worktree add` 一个目录，并
且**跨项目**占用这台机器（状态 `prepared` 时就已经占住了），删除任务才会释放。面板
会把这件事写在按钮上方。

下面这些限制由插件在**服务端**查好再决定按钮能不能按，而不是让你点了之后吃一个
400：

- 机器配置里没有远端工作目录（`remote_workspace`），准备任务必须要它
- 该后端在目标分支是 `monitor` 模式（已有适配 PR），上游会拒绝
- 该（后端, 分支）组合已经有任务了，同一时间只允许一个
- provider 只给 `codex` 和 `claude`。provider 在创建时固定且无法更改，
  而 `flagscale` 当前不可用，列出来只会让任务卡死

另外上游**不记录任务是谁创建的**（没有 `created_by` 之类的字段，未知字段在手动模式
下会被静默丢弃，所以也没法夹带标记）。插件只能把创建时返回的 id 记在本地
（`runs/` 下，已被 git 忽略，只存 id），任务列表默认按这份记录只显示自己建的，
可以切到「看板里的全部」。

### 关于凭据

仓库自带的 `configure.py` **不接受任何凭据参数**，所以凭据是单独收集的。能填的只有
两个，各自都有上游自己的加密落盘位置：

| 凭据 | 面板里能填吗 | 为什么 |
| --- | --- | --- |
| Claude API key | 能 | 上游用 Windows DPAPI 按当前用户加密写进 `runs/`（已 git 忽略），读回只有布尔值 |
| JumpServer 密码 | 能 | 换成会话令牌；不勾「记住」就只在内存里 |
| GitHub token | **不能** | 上游没有存放它的地方，只从自己的环境变量读。做成表单等于让插件自己为一个密钥发明存储，所以这里只提示用 `gh auth login`（它的优先级还更高） |
| SSH 私钥 | **不能** | 上游只按路径引用密钥文件，从不读取内容。接收密钥内容会破坏这个不变量 |

凭据一律走 **POST 请求体**。原因很具体：看板的控制服务器没有覆盖
`log_message`，每个请求行都会进明文日志文件，所以密钥绝对不能出现在
query string、路径段或任何 GET 里。插件自己不保存、不回显、不写浏览器存储。

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
- 创建任务和填凭据只能由人在面板里操作，**不进 agent 工具**（工具只有
  `status`/`start`/`stop`/`read`）。唯一能驱动 agent 的是 `task-message`，它会去上游
  核对任务是否真的可控（`controllable`、`task_policy.mode`、`execution_owner`、
  会话 id 格式），而不是相信浏览器传来的值
- 凭据只走 POST 请求体，绝不进 URL：看板的控制服务器会把每个请求行写进明文日志。
  插件不保存、不回显、不写浏览器存储，成功后只从上游的只读接口回读布尔值
- 机器列表经过投影后才出站，只留 id、标签、后端、角色和「有没有远端工作目录」这个
  布尔值。`ssh_connection`、`identity_file`、`ssh_alias`、`remote_workspace` 的值、
  `rollout_path`、`model_paths` 都不出站；标签是人写的、可能带主机名，所以也脱敏
- 工作区只做「登记已存在的目录」。插件从不创建、移动、清理或删除 worktree，
  也从不执行 `git clone` —— 仓库不在本地时只给出一条可复制的命令，由你自己执行
- 浏览器**从头到尾拿不到文件系统路径**。目录只以名字出现，工作区以不透明 uuid 寻址
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
- **当前这台机器上的 57 个机器配置全部没有填远端工作目录**，所以「准备任务」会被
  面板拦住并说明原因。这一项要在看板自己的机器设置里补，插件不替你猜
- 任务删除是软删除（上游只打 `deleted_at`，产物保留），没有硬删除接口。软删除会
  释放（后端, 分支）占用和机器占用
- 自动识别仓库靠的是看板配置里的 `monitor_config` → `repo` 字段。没配就显示
  「无法识别」，不会去猜一个路径然后对它动手
- 「在这里开始对话」只是在已登记的工作区里新建一个会话，并带上项目名和分支作为
  开场上下文。它不会读取仓库内容，也不会碰远端机器
