# 团队只读看板部署

`share_server.py` 提供团队只读入口，独立于本机任务控制台。它不创建 Codex 执行器，不提供机器配置、会话控制、原始验收文件或本地日志接口。浏览器仅获取已脱敏的分支监控结果。

## 运行环境

- Python 3.11+、Git、tmux。
- 一份插件仓库，用于读取目标分支的用例和完整 Git tree；仓库需要包含验收提交。
- `branch-monitor.json`：从示例复制并填写服务器上的仓库路径、remote、目标分支和证据目录。
- 可选：服务器自己的 GitHub 只读凭据，用于提高 API 配额。凭据通过部署环境提供。

源码通过 Git 或发布归档部署。机器档案、SSH 配置、私钥、个人 Codex 登录目录均不随源码迁移。验收结果应由维护者单独导入配置的证据目录；原始文件仍由维护者管理。

## 双仓库部署

使用 `projects.example.json` 准备服务器自己的 `projects.json`，分别配置两个插件 checkout、目标分支和证据目录。启动时改用 `--projects-config projects.json`，首页显示两个仓库；详情为 `/monitor?project=vllm` 和 `/monitor?project=sglang`。两边缓存、采集时间和失败状态独立；原有认证、路径前缀及脱敏规则同样适用。共享服务不连接本机配置中的 `sessions_api_url`，也不提供会话控制。

## 启动

在部署目录中先以前台运行验证配置：

```sh
python3 -B share_server.py --monitor-config branch-monitor.json --host 127.0.0.1 --port 8765
```

确认成功后，在 tmux 中启动常驻服务：

```sh
tmux new-session -d -s flflow-dashboard 'exec python3 -B share_server.py --monitor-config branch-monitor.json --host 127.0.0.1 --port 8765'
tmux capture-pane -t flflow-dashboard -p
```

服务默认定期采集 Git/CI 状态；`--refresh-interval` 设置采集间隔。页面的“读取最新状态”通过同源 JSON 请求触发一次采集并显示新快照；30 秒内重复请求合并，后台和手动采集串行执行。监控状态与进程必须由一个实例管理，持久保存插件仓库和证据目录。

## 访问入口

### 已有带登录认证的开发平台入口

将平台端口代理连接到本机服务，配置 HTTPS 和团队访问权限。若平台网关通过容器网络访问服务，需要绑定容器可达地址；只有确认网关负责访问认证时才使用 `--trusted-ingress`。这一参数声明外部网关已经配置好，不会自动创建认证。

平台使用路径前缀时，启动服务指定 `--base-path /your-prefix/`，并让浏览器通过带末尾 `/` 的入口访问。页面 API 使用相对 URL。网关应只转发看板服务端口。

### 独立 HTTPS 反向代理

在同一主机用 Nginx 等入口提供 HTTPS。可接入团队登录系统；也可给看板设置独立 HTTP Basic 登录，密码通过 `FLFLOW_SHARE_PASSWORD` 环境变量注入，用户名通过 `--username` 设置。Basic 登录需要由 HTTPS 入口保护。不要将密码写入命令参数、源码或 PR。

服务保持监听 loopback，反向代理将请求转发到 `http://127.0.0.1:8765`。共享服务具有独立只读路由，不依赖放宽本机任务控制台的 Host/Origin 检查。

### 局域网直接 HTTPS

没有反向代理时，共享服务可直接提供 HTTPS：

```sh
python -B share_server.py --projects-config projects.json --host 0.0.0.0 --port 8767 --tls-cert runs/tls/dashboard-cert.pem --tls-key runs/tls/dashboard-key.pem
```

启动前通过部署环境设置 `FLFLOW_SHARE_PASSWORD`，访问者使用 `--username` 指定的用户名（默认 `viewer`）及该密码登录。`--tls-cert` 与 `--tls-key` 必须一起提供，证书链及未加密 PEM 私钥会在采集缓存和后台线程启动前加载；参数缺失、文件不可读或 PEM 无效会明确报错。服务要求 TLS 1.2 或以上；既有 loopback HTTP 和反向代理部署方式仍可使用。

证书的 Subject Alternative Name 应包含访问者使用的内网域名或 IP。优先使用内部 CA 签发的证书，并在访问者设备上信任该 CA。临时自签名证书需要先核对证书指纹，再将对应证书加入访问者设备的信任库；未经信任时浏览器会显示证书警告。证书及私钥保存在忽略的本地运行目录，限制私钥的文件访问权限；测试代码中的 `explicit_dummy` 证书仅供自动化测试。

常驻主机需要固定内网地址、保持开机并关闭休眠。只对需要访问的内网网段开放共享服务端口，本机任务控制台继续监听 loopback。Windows 可在管理员 PowerShell 中建立限定局域网和网络类型的规则：

```powershell
New-NetFirewallRule -DisplayName "FL dashboard LAN HTTPS" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8767 -Profile Private,Domain -RemoteAddress LocalSubnet
```

可将 `LocalSubnet` 换成团队使用的明确内网 CIDR。访问者使用 `https://<LAN-host>:8767/`；服务监听 `0.0.0.0` 只表示绑定所有 IPv4 接口，实际可达性还受 Windows 防火墙、路由和网络隔离规则影响。配置服务或计划任务开机启动时，沿用同一运行目录和环境变量，并将输出保存在本地日志中。

### 局域网免登录阅读

团队只需要免登录查看进度时，使用 `--anonymous-lan`；绑定地址填写主机的具体 RFC1918 内网 IPv4 地址：

```sh
python -B share_server.py --projects-config projects.json --host <LAN-IPv4> --port 8767 --anonymous-lan --tls-cert runs/tls/dashboard-cert.pem --tls-key runs/tls/dashboard-key.pem
```

此模式不读取登录密码环境变量，不返回登录提示，仅提供原有只读监控路由。它要求直接 HTTPS、具体内网地址，不接受 `0.0.0.0`、公网地址或主机名。防火墙仍限定允许访问的内网网段。本机完整控制台继续绑定 loopback；共享入口不连接任务执行器，新建、跟进、审批、停止任务与机器管理请求均返回 `405`。

### Windows 登录后常驻

`start_lan_dashboard.ps1` 读取忽略的 `runs/lan/deployment.json`。设置 `anonymous_lan: true` 时无需密码文件；其余模式从当前 Windows 用户的 DPAPI 加密文件读取密码并短暂注入服务进程环境。服务输出与标准错误分别写入本地日志。免登录配置示例如下；路径均填写部署机器上的实际值：

```json
{
  "python": "<python executable>",
  "host": "<LAN IPv4>",
  "port": 8767,
  "anonymous_lan": true,
  "projects_config": "<projects.json>",
  "tls_cert": "<PEM certificate>",
  "tls_key": "<PEM private key>",
  "refresh_interval": 300,
  "log_file": "<local service log>"
}
```

需要登录认证时，省略 `anonymous_lan` 或设置为 `false`，增加 `username` 和 `password_file`。使用 `ConvertFrom-SecureString` 保存由安全提示或部署流程提供的 SecureString。运行目录仅授予部署用户及 SYSTEM 访问权限；DPAPI 密码文件只能由创建它的 Windows 用户解密，不可直接迁移到另一台机器。

验证 `powershell.exe -NoProfile -File .\start_lan_dashboard.ps1` 后，可注册以当前用户、最低权限、登录时触发的 Windows 计划任务，启动命令增加 `-WindowStyle Hidden`。计划任务的执行时限设为零，避免长时间运行后被默认时限停止。重新配置监听地址或证书后，重启该共享服务；本机任务控制台独立运行。

### Windows 本机任务控制台常驻

`start_local_dashboard.ps1 -ConfigPath <config>` 读取受信任、忽略的 `runs/local/<config>.json`，用于运行 `flflow.py serve`。`python` 填本机 Python `.exe`，`working_directory` 填仓库目录，`arguments` 是完整 argv 数组，包含 `-u`、`-B`、`flflow.py` 路径及 `serve` 参数；可另填 `program` 指定 `flflow.py`，此时 `arguments` 从 `serve` 开始。默认配置为 `runs/local/dashboard.json`；旧 owner 可使用独立配置，分别保持各自端口及会话目录。标准输出写入 `log_file`，标准错误写入同名 `.stderr`，启动或退出诊断追加到 `.launcher.log`。每次启动会重建标准输出/错误日志，需要长期保留时先归档旧日志。配置仅由本机维护者编辑，不接受网页提供的命令字符串。

```json
{
  "python": "<absolute Python executable>",
  "arguments": ["-u", "-B", "<absolute flflow.py>", "serve", "--projects-config", "<absolute projects.json>", "--host", "127.0.0.1", "--port", "8766"],
  "working_directory": "<repository directory>",
  "log_file": "<repository directory>/runs/local/dashboard.log"
}
```

计划任务使用当前登录用户、`Interactive` 登录类型、最低权限 `Limited`，登录时触发；保留同一用户的 Codex 登录及 DPAPI 配置。Action 使用下面的独立 PowerShell 进程；设置 `ExecutionTimeLimit = 0`、`RestartCount = 3`、`RestartInterval = 1 分钟`、`MultipleInstances = IgnoreNew`，使 Python 异常退出后可自动重启且不会重复运行 owner。启动器以隐藏窗口等待 Python，直接分别重定向 stdout/stderr；Windows PowerShell 5.1 不使用 `& python 2>&1`，正常 stderr 不会触发 `ErrorActionPreference = Stop` 退出。更新计划任务 Action 只影响下次启动；维护已运行的旧 owner 时，不通过重启加载启动器变更，以免中断现有任务。

```powershell
powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "<repository directory>\start_local_dashboard.ps1" -ConfigPath "<repository directory>\runs\local\dashboard.json"
```

### 临时 SSH 访问

有开发机 SSH 权限的维护者可转发端口：

```sh
ssh -N -L 127.0.0.1:8765:127.0.0.1:8765 <development-host>
```

然后访问 `http://127.0.0.1:8765/`。各访问者需自行建立隧道；这不是一个可直接转发给团队的公共网址。

## 验证与维护

- 在服务器检查 `/healthz` 和 `/api/capabilities`，确认 `read_only: true`。
- 从实际团队入口检查首页、CI/Gate 数据以及带前缀的 API 请求。
- 确认任务、机器、日志和原始证据接口不可访问。仅 `/api/branch-refresh` 接受符合当前认证策略和同源检查的 JSON POST；任务控制 POST 及其他写请求仍拒绝访问。
- 查看 `tmux capture-pane -t flflow-dashboard -p` 排查启动或采集错误。
- 页面保留最后采集时间；采集失败不会把未知状态伪装成通过。

公网或团队入口尚未创建时，仅在服务器看到监听端口并不代表其他用户已经能够访问。交付地址前应从访问者网络验证。

### 本机 agent 配置与只读任务状态

Codex / Claude 的新建、启动、跟进和配置只通过 loopback 完整控制台使用。Claude API key 由当前 Windows 用户 DPAPI 加密保存在忽略的运行目录；服务或计划任务必须使用同一 Windows 用户。局域网入口不初始化 agent 设置或执行器，`/api/agent-settings`、`/api/agent-providers` 及私有原件接口均不开放。

原生子会话的脱敏状态可在共享首页和详情页读取，内容仅包含配置的 backend、分支、状态、阶段与时间。历史实验记录在共享页面指向 GitHub PR，本机入口可读取经哈希验证的 UTF-8 存档。
