# FL 插件适配工作台

**DeepSeek Harness** 插件：在左侧边栏里跟踪 **vLLM-Plugin-FL** 和 **SGLang-Plugin-FL**
的适配进展 —— 各 backend 的 PR、CI、验收、GPU 设备和 agent 任务，都在一个原生面板里，
跟随主题和语言，不用 iframe。

<!-- 截图待补：Harness 左栏「FL 适配」图标 + 右侧原生面板。 -->

面板读的是本机一个 Python 看板进程；插件负责拉起它、代理它的只读接口、把结果画进面板。
那个看板也能脱离 Harness 单独用浏览器打开，见[独立使用](docs/STANDALONE.md)。

## 安装

从 [Releases](../../releases) 下载 `physics31415926-fl-upgrade-workflow-<版本>.tgz`，在左侧边栏
**插件 → 添加插件**里填它的**绝对路径**，点安装。需要本机有 **Python 3.11+ 和 Git**
（看板只用 Python 标准库，无需 pip 依赖，也不需要 GPU）。

命令行等价写法：

```sh
dsh plugin install /绝对路径/physics31415926-fl-upgrade-workflow-0.6.4.tgz
```

**装好即用，不用先手动 clone 看板仓库。** `repoPath` 留空时，插件首次激活会把看板仓库
浅克隆到 `~/.dsh/fl-plugin-upgrade-workflow` 并使用它（标题栏显示「正在拉取看板…」）。
想用自己管的检出，就把路径填进 `repoPath`。

装过之后不用再手动下载：标题栏的「检查更新」按钮点一下就从 GitHub Releases 拉取、校验、
安装。

## 配置

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `repoPath` | 自动 clone | 留空则 clone 到 `~/.dsh/fl-plugin-upgrade-workflow`；填了就用你的检出，绝不往里 clone |
| `autoClone` | `true` | 留空的 `repoPath` 是否自动拉取；`false` 则完全手动 |
| `autoStart` | `true` | 激活时是否自动拉起看板进程（面板数据都来自它） |
| `port` | `8766` | 看板的回环端口 |
| `pythonPath` | PATH 上的 `python` | 需要 3.11+ |
| `githubTokenEnv` | `GITHUB_TOKEN` | 读取 GitHub token 的环境变量**名**（值存在 Harness 凭据库，不在这里） |

还没配过看板也行：面板里「在本机部署」勾选项目、填分支，它会调用仓库自带的 `configure.py`
完成配置并启动。看板已在运行（含你自己在终端起的）时，插件会识别并接管显示。

## 面板能做什么

- **进展**：项目/分支切换，后端视图（合入、CI、验收、门禁），任务视图
- **设备**：GPU 机群状态，可按厂商筛选（机群跨项目共用，独立于分支）
- **任务**：详情页看计划、消息、进展和对话，并可发送后续消息；新建任务可选分支下拉、
  provider（codex / claude / flagscale）、就地补远端工作目录。新建**只准备不启动** ——
  启动会烧额度、在远端执行命令，留在看板里做
- **仓库**：自动识别项目检出、一键加入 Harness 工作区并开一个带上下文的对话。检出不在本地时，
  面板里一键 clone —— 目标路径和仓库源都取看板已有配置，不用手打
- **凭据**：Claude / JumpServer 可在面板里填，已配置的可「更换」。GitHub 两种方式任选其一：
  已登录 `gh` CLI（面板自动识别，不用再填 token），或填一个 token
- **采集状态小标 / 检查更新**：标题栏里，分别反映采集是否在出数据、以及有无新版本

要监控的插件仓库（[vLLM-Plugin-FL](https://github.com/flagos-ai/vllm-plugin-FL)、
[SGLang-Plugin-FL](https://github.com/flagos-ai/sglang-plugin-FL)）不需要预先 clone：
面板检测到缺失时会给出「立即克隆」按钮。

### FlagScale Agent 预设（可选）

想让 agent 带 FlagScale-Agent 的工作流和知识库，跑一条命令即可 —— 缺检出就 clone，
已有就 fast-forward 拉取，然后链接技能并重建知识索引：

```sh
python scripts/setup_flagscale_agent.py              # clone 或 pull，然后同步
python scripts/setup_flagscale_agent.py --install-schedule   # 顺带注册每日自动更新
```

默认 checkout 在 `~/.dsh/FlagScale-Agent`，技能根 `~/.dsh/flagscale-preset/skills`
（preset 的 `customSkillDirs` 要指向它）。检出有未提交改动时拒绝拉取；只做
fast-forward；技能根里的真实目录绝不删除。

## 安全边界（摘要）

- 浏览器不直连看板，只走插件代理的**只读白名单**；带机器身份或凭据位置的接口一律拒绝
- 出站前裁剪字段、脱敏 agent 自由文本（路径、主机、账号、IP）；浏览器拿不到任何文件系统路径
- 凭据只走 POST 请求体，不落盘、不回显、不写浏览器存储；GitHub token 存进 Harness 凭据库，
  以子进程环境变量交给看板，不经 HTTP
- 创建任务、填凭据只能由人在面板里操作，不进 agent 工具
- 看板自身**无身份认证**，仅靠回环绑定与 Host/Origin 检查

完整的安全边界、已知限制和自动更新细节见
[插件 README](integrations/dsh-plugin/README.md)。

## 更多文档

- [独立使用](docs/STANDALONE.md)：不装 Harness，直接用浏览器页面，含局域网只读访问
- [部署指南](docs/DEPLOYMENT.md)：配置向导、无人值守、HTTPS、后台常驻
- [高级配置与验收参考](docs/REFERENCE.md)：agent 配置、原生任务接入、结果归档
- [结构与扩展指南](docs/ARCHITECTURE.md)：代码结构、新增项目与监控类型
