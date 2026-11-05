# FL 插件适配工作台

在同一页面跟踪 **vLLM-Plugin-FL** 和 **SGLang-Plugin-FL** 的适配进展，查看各 backend 的 PR、CI、验收结果和 agent 任务。

![双仓库适配进展快照](docs/images/dashboard-demo.jpg)

*当前版本总览快照（2026-10-15）。在线状态以最新采集结果为准。*

- 双仓库独立选择分支，结合 PR 标题与描述关联适配 PR；合入后继续跟踪，并核验合并提交属于所选分支。
- 本机从已采集设备中选择机器和 Codex、Claude 或 FlagScale Agent 即可创建任务；backend 按采集结果自动匹配，远端目录自动分配，补充说明可选。
- Codex 任务可在客户端继续同一会话，看板保留进展；长日志中的活跃任务持续显示。
- 验收结果优先读取 PR 描述，点击在页面内查看测试表格；保留本地存档与原生子会话进展。
- 本机登录 JumpServer 后刷新授权机器列表；也可沿用 SSH 配置，支持 Include。设备信息和最近读数保存在本地。
- 集成 [jms-top](https://github.com/lvyufeng/jms-top) 的多厂商资源采集：查看空闲卡、显存、负载及进程，按设备进入新建任务。
- 本机完整控制，局域网普通地址免登录只读；可生成定时或永久有效、随时可撤销的完整权限分享链接。

## 快速部署

准备 **Python 3.11+、Git**，下载本项目，并 clone 需要监控的插件仓库：[vLLM-Plugin-FL](https://github.com/flagos-ai/vllm-plugin-FL)、[SGLang-Plugin-FL](https://github.com/flagos-ai/sglang-plugin-FL)。插件 checkout 需包含所选 remote 的目标分支；首次 clone 后通常已有，缺少时先执行 `git fetch <remote>`。

看板使用 Python 标准库，**无需 pip 安装依赖或 GPU 环境**。建议安装并登录 `gh` 读取 GitHub 状态；执行适配任务时再准备 agent CLI、SSH 和远端 GPU 环境。

在本项目目录运行（Windows / Linux / macOS；系统只有 `python3` 时替换命令中的 `python`）：

```sh
python -B dashboard.py configure
python -B dashboard.py start
```

向导选择一个或两个项目，填写插件 checkout 路径、remote、目标分支和端口；自动生成配置后，启动器会检查配置及端口。打开 [本机看板](http://127.0.0.1:8766/)，保留启动终端；按 `Ctrl+C` 停止。端口冲突可用 `python -B dashboard.py start --port 9000`。

配置保存在 Git 忽略的 `runs/configured/`，各项目的会话与验收目录独立。向导不会覆盖已有配置；需要另一套配置时指定 `--output-dir`。已有 `projects.json` 的部署可继续使用原启动命令。迁移到另一台机器后重新运行向导，验收存档另行导入。

局域网部署沿用这份项目配置，单独启动免登录只读 HTTPS 服务。证书、防火墙、非交互配置及 Windows 登录后常驻的可复制步骤见 [部署指南](docs/DEPLOYMENT.md)。

## 页面入口

| 页面 | 用途 |
| --- | --- |
| `/` | 双仓库适配总览 |
| `/monitor?project=vllm` | 分支、PR、CI 和逐项验收详情 |
| `/sessions?project=vllm` | 验证机器、agent 配置与适配任务 |
| `/devices?project=vllm` | 设备资源、每卡状态与可用性（局域网免登录查看、手动刷新） |
| `/access-shares` | 本机创建、查看和撤销完整权限分享链接（需启用） |

总览页点击 **“导出 HTML”**，将两个项目当前所选分支的 PR、CI 和验收明细保存为单个文件，可直接发送、离线打开；本机与局域网访客均可导出。报告保留真实采集时间，缓存或采集未完成会明确标注。

版本标签随所选分支变化，读取该分支提交的 `pyproject.toml` 中对应引擎的精确依赖版本；尚未更新依赖声明的升级目标不会自动推断。

查看 SGLang 时将 `project=vllm` 改为 `project=sglang`。任务工作台提供「活跃 / 全部 / 回收站」视图，可搜索名称、分支或设备，按状态和 Agent 筛选。已结束的任务在「全部」中查看；删除任务会移入回收站，可恢复，工作目录和验收记录仍保留。运行中的任务须先中断。

代码合入、CI 和验收结果分别展示。验收结果汇总 PR 作者在描述中报告的测试结论：通过、部分通过、未通过或待验收，点击在当前页面展开验收表格，可继续查看原 PR。PR 暂无可识别结果时，使用关联同一 PR 的本地记录或逐项结果；原始场景和测试版本保留在分支详情中。

新建任务的“验证设备”读取设备资源页的采集记录。新机器先在资源页采集；过期记录会标明并在创建时重新核验，当前项目不支持的硬件标记为“仅资源监控”。如需手动指定 backend，可展开高级设置并启用“使用自定义连接与验收配置”。

设备资源页支持两种来源：在本机页面登录 JumpServer 后直接同步授权机器，或沿用已有 SSH 档案。无需安装 TUI。控制服务每 5 分钟在后台采集设备读数，关闭设备页也会更新。打开或切回页面即可读取最近结果。机器列表可按需手动同步。数据过期、重启后尚未核验或驱动异常时，不计为空闲。JumpServer 登录用于获取资产，实时资源采集还需可用的 SSH 网关认证；详见[设备配置](docs/REFERENCE.md#设备资源与-jms-top)。普通 LAN 地址可直接查看适配进展、设备及进程明细，并手动刷新资源。多个访客共用同一份缓存，手动刷新共用 30 秒冷却。任务操作仍需本机或有效管理分享权限，JumpServer 登录与配置仅限本机。

## 在 DeepSeek Harness 侧边栏里使用

除了浏览器页面，本项目还提供一个 [DeepSeek Harness](https://github.com/deepseek-ai) 插件，
把适配工作台搬进 Harness 左侧边栏：原生面板，跟随主题和语言，不用 iframe。
源码在 [integrations/dsh-plugin/](integrations/dsh-plugin/)，那里的
[README](integrations/dsh-plugin/README.md) 写了完整的安全边界和已知限制。

### 安装

从 [Releases](../../releases) 下载 `local-fl-upgrade-workflow-<版本>.tgz`，在 Harness 里安装：

```sh
dsh plugin install ./local-fl-upgrade-workflow-0.1.0.tgz
```

也可以自己打包，产物与发布的一致（纯标准库，不需要 Node）：

```sh
python -B scripts/pack_dsh_plugin.py --output-dir dist
```

打包是可复现的：条目排序固定、时间戳固定、换行统一为 LF，所以同一份源码在任何平台
打出的包字节一致，发布的 sha256 才有意义。

### 配置

安装后必须填 `repoPath`，面板在这之前什么也做不了：

| 配置项 | 说明 |
| --- | --- |
| `repoPath` | **必填**，本项目检出的绝对路径 |
| `port` | 看板监听的回环端口，默认 `8766` |
| `pythonPath` | 留空则用 PATH 上的 `python`，需要 3.11+ |
| `projectsConfig` | 留空则依次找 `runs/configured/projects.json`、`projects.json` |
| `autoStart` | 插件激活时是否自动拉起看板进程，默认 `false` |

还没有配置过看板也没关系：面板里「在本机部署」是高亮的入口，勾选项目、填检出路径和
目标分支，它会调用本项目自带的 `configure.py` 和启动器完成配置并启动，不重新实现一套
配置逻辑。看板已经在跑（包括你自己在终端里起的）时，插件会识别并接管显示，不会重复启动。

### 面板能做什么

- 项目和分支切换，后端视图（合入、CI、验收、门禁）、设备视图、任务视图
- 任务详情：计划、最新消息、进展摘要和对话记录，可发送后续消息
- 新建任务：**只准备不启动**。上游带 `start` 的那次请求会同时创建并启动，也就是立刻
  开始烧模型额度、在远端机器上执行命令，所以启动留在看板里做。准备本身也会建 worktree
  并占用机器，这件事写在按钮上方
- 项目仓库：按看板配置自动识别检出目录，一键加入 Harness 工作区，并在该工作区里新建
  一个带好上下文的对话。只登记已存在的目录，不执行 `git clone` —— 仓库不在本地时只给
  一条可复制的命令
- 凭据：显示 Claude / JumpServer / GitHub / SSH 各自是否就绪。只有前两个能在面板里填，
  因为只有它们在看板侧有加密落盘的位置；GitHub token 看板只从环境变量读，SSH 私钥只按
  路径引用从不读取内容，给它们做表单等于让插件为密钥自己发明存储

任务列表默认只显示从插件创建的任务。上游不记录任务由谁创建（没有这类字段，未知字段还
会被静默丢弃），所以插件把创建时返回的 id 记在 `runs/` 下（已被 git 忽略，只存 id），
想看全部可以一键切换。

### 安全边界（摘要）

- 浏览器不直连看板，所有读取走插件在 Harness 上的代理，**只放行一份只读白名单**。
  `/api/codex-sessions`、`/api/jumpserver`、`/api/agent-settings` 这些带机器身份和凭据
  位置的接口全部拒绝
- 出站前裁剪字段，并对 agent 写的自由文本额外脱敏（绝对路径、主机、账号、IP）
- 机器列表只保留 id、脱敏标签、后端、角色和「有没有远端工作目录」这个布尔值
- 凭据只走 POST 请求体。原因很具体：看板的控制服务器会把每个请求行写进明文日志，
  密钥出现在 query string 就等于落盘。插件自己不保存、不回显、不写浏览器存储
- 浏览器拿不到文件系统路径，目录只以名字出现
- 创建任务和填凭据只能由人在面板里操作，不进 agent 工具

完整清单见[插件 README](integrations/dsh-plugin/README.md#安全边界)。

## 更多配置

- [部署指南](docs/DEPLOYMENT.md)：配置向导、无人值守配置、HTTPS、免登录只读和后台常驻。
- [高级配置与验收参考](docs/REFERENCE.md)：agent 配置、原生任务接入、结果归档、命令编排与异常恢复。

Claude 与 FlagScale 的 API key 独立配置，使用 Windows 当前用户的 DPAPI 加密保存；普通局域网入口提供脱敏状态与验收表格。需要协作管理时，按[部署指南](docs/DEPLOYMENT.md#完整权限分享链接)启用分享，再从本机「访问分享」生成链接；持有者可创建、启动和管理任务，默认 24 小时到期，也可选择“永久有效（可撤销）”。

## 代码结构与扩展

业务代码位于 fl_dashboard/，按 monitoring、projects、agents、tasks、services、deployment 分工；页面在 web/templates/，示例配置在 examples/。根目录仅保留 dashboard.py 统一入口，命令脚本集中在 scripts/。新增同类仓库只需增加项目配置；新监控类型通过可信采集器注册接入，默认只读。详见 [结构与扩展指南](docs/ARCHITECTURE.md)。
