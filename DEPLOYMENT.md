# 部署与局域网访问

本机控制台提供双仓库监控与 agent 任务管理；局域网入口提供免登录的只读进度页面。首次部署先运行配置向导和本机页面，再按需增加 LAN 入口。

以下多行命令使用 PowerShell 续行符。Linux / macOS 使用 `python3`，将参数写成一行或用 `\` 续行；Windows 常驻步骤仅适用于 Windows。

## 首次配置

准备 Python 3.11+、Git、工作台源码，以及要监控的 vLLM-Plugin-FL / SGLang-Plugin-FL 本地 Git checkout。看板使用 Python 标准库，不需要安装 vLLM、SGLang 或 GPU 运行包；Linux 常驻时可选用 tmux，Windows 使用下文的计划任务。

插件 checkout 必须有目标分支的远端引用及要验收的提交。配置向导只读取本地 Git 信息，不执行 clone、fetch、GPU 命令或 agent 任务。缺少分支时，先自行检查 remote 并 fetch，例如：

```powershell
git -C 'C:\work\vllm-plugin-FL' remote
git -C 'C:\work\vllm-plugin-FL' fetch upstream
git -C 'C:\work\sglang-plugin-FL' fetch upstream
```

将示例路径和 `upstream` 换成自己 checkout 的实际值。可选安装并以部署用户登录 `gh`，提高 GitHub PR / CI 查询配额；没有 `gh` 时仍可查询公开 API。创建 agent 任务时，另准备对应的 Codex / Claude CLI 或 FlagScale Agent Python 环境、认证和 SSH 环境。只读 LAN 不需要 agent CLI 或 API key。

在工作台目录打开终端，运行向导：

```powershell
python -B configure.py
```

依次选择项目、插件路径、remote、目标分支、私有输出目录和本机端口。默认选择两个项目，输出到 `runs/configured/`，端口为 `8766`。生成内容包括 `projects.json`、所选项目的 `branch-monitor*.json` 和 `launch.json`；两边会话与验收目录独立，生成文件由 Git 忽略。

只监控一个项目也可以，例如：

```powershell
python -B configure.py --projects sglang
```

批量或重复部署时使用非交互参数；以下路径和分支仅为示例：

```powershell
python -B configure.py --non-interactive --projects vllm sglang `
  --vllm-repo 'C:\work\vllm-plugin-FL' --vllm-remote upstream --vllm-branch '0.4.0-dev' `
  --sglang-repo 'C:\work\sglang-plugin-FL' --sglang-remote upstream --sglang-branch 'dev/0.5.18' `
  --output-dir runs/configured --port 8766
```

生成的配置只保留模型、模式和 backend 等通用默认值，不带入示例里的历史 PR、Gate 通过记录或会话服务连接。模型和模式可用 `--vllm-models` / `--sglang-models`、`--vllm-modes` / `--sglang-modes` 覆盖，或在生成的监控 JSON 中修改。

配置向导默认拒绝覆盖已有文件。已有配置可直接继续使用；需要另一套配置时选择不同的 `--output-dir`。只有确认要替换生成文件并自行备份后才使用 `--overwrite`，它不会保留手工修改。重新配置不会重启正在运行的服务；旧 owner、会话目录和已有私有配置应由维护者单独管理。

源码可通过 Git 或发布归档迁移。机器档案、SSH 私钥、个人 Codex 登录目录及 DPAPI 密文不随源码迁移；在新机器上以部署用户重新配置。已有验收文件由维护者单独导入对应证据目录，未导入的记录会显示缺失。

## 启动本机页面

先检查生成配置，再启动前台服务：

```powershell
python -B start_dashboard.py --check-only
python -B start_dashboard.py
```

默认读取 `runs/configured/launch.json`，打开 [本机看板](http://127.0.0.1:8766/)。启动器使用配置中的 Python 和 projects 文件；完整控制台只监听 `127.0.0.1`。终端保持运行，`Ctrl+C` 结束这次前台服务。

使用其他输出目录或临时端口时：

```powershell
python -B start_dashboard.py --config runs/configured-alt/launch.json --port 8876 --check-only
python -B start_dashboard.py --config runs/configured-alt/launch.json --port 8876
```

`--port` 只覆盖本次启动参数，不修改配置文件。需要长期使用新端口时更新对应 `launch.json`。

双仓库首页分别显示两个项目；详情为 `/monitor?project=vllm` 和 `/monitor?project=sglang`。各项目的缓存、采集时间和错误状态独立。任务控制只通过本机入口使用。首次完成 Agent 登录或 API 配置及 SSH 非交互连接后，在任务页选择设备和 Agent 即可创建；补充说明可选，远端目录自动分配。自定义连接和验收参数放在高级设置中。

## 局域网免登录阅读

准备主机的固定 RFC1918 IPv4 地址，以及包含该地址的 HTTPS PEM 证书和未加密 PEM 私钥。以下用 `192.168.10.20` 示意，替换为这台主机真实的内网地址；该地址应固定或设置 DHCP 保留。

先检查，再在另一个终端启动独立的只读服务：

```powershell
python -B start_dashboard.py --lan --lan-ip 192.168.10.20 --lan-port 8767 `
  --tls-cert runs/tls/dashboard-cert.pem --tls-key runs/tls/dashboard-key.pem `
  --refresh-interval 300 --check-only

python -B start_dashboard.py --lan --lan-ip 192.168.10.20 --lan-port 8767 `
  --tls-cert runs/tls/dashboard-cert.pem --tls-key runs/tls/dashboard-key.pem `
  --refresh-interval 300
```

另一个配置目录可增加 `--config <launch.json>`。LAN 模式使用同一 projects 配置，直接提供 HTTPS 与免登录只读路由，不创建任务执行器。它仅接受具体的 `10.x.x.x`、`172.16.x.x–172.31.x.x` 或 `192.168.x.x` 地址，拒绝 `0.0.0.0`、主机名、loopback 和公网地址。

访问者打开 `https://192.168.10.20:8767/`。本机完整控制台继续使用 `http://127.0.0.1:8766/`。LAN 入口不读取登录密码环境变量，新建、跟进、审批、停止任务与机器管理请求均返回 `405`，私有配置和原始日志接口不开放。启动器不会自动生成或信任证书、修改防火墙、注册计划任务。

### 证书准备

优先使用内部 CA 签发的证书，Subject Alternative Name（SAN）应包含访问者使用的内网 IP 或域名，并让访问者设备信任该 CA。服务读取 PEM 证书链和未加密 PEM 私钥，不直接读取 PFX 或 Windows 证书库。

若已安装 OpenSSL，可手动生成临时自签名证书。以下命令在工作台目录执行，仅用于新建证书；文件已存在时停止，避免覆盖现有私钥：

```powershell
$lanIp = '192.168.10.20'
$tlsDirectory = Join-Path (Get-Location).ProviderPath 'runs\tls'
$certPath = Join-Path $tlsDirectory 'dashboard-cert.pem'
$keyPath = Join-Path $tlsDirectory 'dashboard-key.pem'
if ((Test-Path -LiteralPath $certPath) -or (Test-Path -LiteralPath $keyPath)) {
    throw 'Certificate or key already exists; reuse it or choose a new directory.'
}
New-Item -ItemType Directory -Path $tlsDirectory -Force | Out-Null
$deploymentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
icacls $tlsDirectory /inheritance:r /grant:r "${deploymentUser}:(OI)(CI)F" 'SYSTEM:(OI)(CI)F'
if ($LASTEXITCODE -ne 0) { throw 'Unable to restrict the TLS directory permissions.' }
openssl req -x509 -newkey rsa:3072 -nodes -sha256 -days 365 `
  -subj '/CN=FL dashboard' -addext "subjectAltName=IP:$lanIp" `
  -addext 'extendedKeyUsage=serverAuth' -addext 'basicConstraints=critical,CA:FALSE' `
  -keyout $keyPath -out $certPath
if ($LASTEXITCODE -ne 0) { throw 'Certificate generation failed; inspect the output before retrying.' }
openssl x509 -in $certPath -noout -fingerprint -sha256
```

OpenSSL 仅用于这个可选步骤，不是看板依赖；参数见 [OpenSSL req 文档](https://docs.openssl.org/3.0/man1/openssl-req/)。核对证书 SHA-256 指纹后，将公开证书交给访问者，并按团队要求加入其设备的信任库。未经信任时浏览器会显示证书警告。不要分发私钥或使用测试代码中的 `explicit_dummy` 证书。

证书及私钥放在忽略的本地运行目录，私钥目录仅授权部署用户和 SYSTEM。证书到期或内网 IP 变化后重新签发，并由维护者重启该 LAN 服务。

### Windows 防火墙

只有开放入站端口需要管理员 PowerShell。将 IP、端口和允许网段改为实际值后，单独执行：

```powershell
$ruleName = 'FLFlow-LAN-HTTPS-8767'
if (Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue) {
    throw 'Firewall rule already exists; inspect it before changing it.'
}
New-NetFirewallRule -Name $ruleName -DisplayName 'FL dashboard LAN HTTPS' `
  -Direction Inbound -Action Allow -Protocol TCP -LocalAddress 192.168.10.20 `
  -LocalPort 8767 -Profile Private,Domain -RemoteAddress LocalSubnet
```

可将 `LocalSubnet` 换成团队使用的明确 CIDR，例如 `192.168.10.0/24`。不要为本机控制台的 `8766` 开放入站规则。规则仅适用于 Private / Domain 网络；路由、无线客户端隔离和企业策略也会影响访问。

常驻主机需保持开机并关闭休眠。从另一台设备验证 HTTPS 首页及 `/healthz`，再交付入口地址；主机监听成功不代表其他设备已能访问。

## Windows 登录后常驻

### 使用生成的本机配置

先完成前台验证并结束这次新部署的前台服务。手动后台启动与下面的计划任务选择一种；手动启动可运行：

```powershell
powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass `
  -File .\start_local_dashboard.ps1 -ConfigPath .\runs\configured\launch.json
```

启动器以隐藏窗口等待 Python，stdout / stderr 分别写入配置的日志；正常 stderr 不会使 Windows PowerShell 5.1 异常退出。默认生成日志为 `runs/configured/runs/local/dashboard.log`、同名 `.stderr` 和 `.launcher.log`。每次启动会重建 stdout / stderr 文件，需要保留时先归档。

需要登录后自动启动时，在工作台目录以部署用户执行以下代码。任务名称要与其他部署区分；示例检测到同名任务时拒绝覆盖，也不会停止现有 owner：

```powershell
$dashboardRoot = (Get-Location).ProviderPath
$launchPath = (Resolve-Path -LiteralPath '.\runs\configured\launch.json').ProviderPath
$launcherPath = (Resolve-Path -LiteralPath '.\start_local_dashboard.ps1').ProviderPath
$deploymentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$taskName = 'FLFlow-Configured-Local-8766'
python -B start_dashboard.py --config $launchPath --check-only
if ($LASTEXITCODE -ne 0) { throw 'Configuration or port check failed; do not register another owner.' }
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    throw 'Task already exists; choose a distinct name or review it before updating.'
}
$powerShellPath = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
$actionArguments = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}" -ConfigPath "{1}"' -f $launcherPath, $launchPath
$action = New-ScheduledTaskAction -Execute $powerShellPath -Argument $actionArguments -WorkingDirectory $dashboardRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $deploymentUser
$principal = New-ScheduledTaskPrincipal -UserId $deploymentUser -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) `
  -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
  -Principal $principal -Settings $settings | Out-Null
Start-ScheduledTask -TaskName $taskName
Get-ScheduledTaskInfo -TaskName $taskName
```

任务使用当前登录用户、最低权限，运行时限为零，异常退出最多重试三次、间隔一分钟，并忽略重复启动。该方式需要用户保持登录；不表示登录前就可访问。参数参见 Microsoft 的 [任务运行身份](https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/new-scheduledtaskprincipal) 和 [任务设置](https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/new-scheduledtasksettingsset) 文档。

日志可按生成配置读取：

```powershell
$launchSettings = Get-Content -LiteralPath .\runs\configured\launch.json -Raw -Encoding UTF8 | ConvertFrom-Json
Get-Content -LiteralPath $launchSettings.log_file -Tail 30
Get-Content -LiteralPath ($launchSettings.log_file + '.stderr') -Tail 30
if (Test-Path -LiteralPath ($launchSettings.log_file + '.launcher.log')) {
    Get-Content -LiteralPath ($launchSettings.log_file + '.launcher.log') -Tail 30
}
```

计划任务环境的 PATH 可能与终端不同。配置向导可通过 `--codex-executable` 记录 CLI 完整路径；安装路径变化时同步更新忽略的监控配置。使用同一部署用户保留 Codex 登录和 DPAPI 配置。

更新计划任务 Action 只影响下次启动；维护运行中的旧 owner 时，不通过重启加载启动器变更，以免中断任务。每个会话目录只由一个 owner 管理。

### LAN 服务常驻

LAN 可继续使用 `start_lan_dashboard.ps1`，手动准备忽略的 `runs/lan/deployment.json`。目录及日志父目录需提前创建；路径填写这台机器上的绝对路径，`projects_config` 指向生成的 projects 文件：

```json
{
  "python": "<absolute Python executable>",
  "host": "192.168.10.20",
  "port": 8767,
  "anonymous_lan": true,
  "projects_config": "<absolute runs/configured/projects.json>",
  "tls_cert": "<absolute PEM certificate>",
  "tls_key": "<absolute PEM private key>",
  "refresh_interval": 300,
  "log_file": "<absolute local LAN log>"
}
```

`anonymous_lan: true` 无需密码文件。先验证：

```powershell
powershell.exe -NoProfile -File .\start_lan_dashboard.ps1 -ConfigPath .\runs\lan\deployment.json
```

注册 LAN 任务时复用上面的计划任务代码，将 `$launchPath` 改为这个 deployment 文件、`$launcherPath` 改为 `start_lan_dashboard.ps1`，并将 `$taskName` 改为独立名称，例如 `FLFlow-Configured-LAN-8767`。其中 `--check-only` 检查改用上文 LAN 快捷启动命令及对应的 `launch.json`、地址和证书参数；deployment JSON 不传给 `start_dashboard.py`。先结束这次新部署的前台 LAN 服务，避免端口冲突；本机控制台继续独立运行。输出和错误分别写入 `log_file` 及同名 `.stderr`，启动异常记录在 `runs/lan/launcher-errors.log`。

需要登录认证时，省略 `anonymous_lan` 或设为 `false`，增加 `username` 与 `password_file`。使用 `ConvertFrom-SecureString` 保存由安全提示提供的 SecureString；DPAPI 密码文件只能由创建它的 Windows 用户解密，不能直接迁移到另一台机器。启动器只在进程环境中短暂提供密码。

## 高级部署

### 沿用已有配置

已有 `projects.json` 可直接启动本机控制台：

```powershell
python -B flflow.py serve --projects-config projects.json --host 127.0.0.1 --port 8766
```

要接入快捷启动器，可手动准备兼容的 launch JSON，再通过 `--config` 指定。Windows 的 `start_local_dashboard.ps1` 同样读取此格式：

```json
{
  "python": "<absolute Python executable>",
  "arguments": ["-u", "-B", "<absolute flflow.py>", "serve", "--projects-config", "<absolute projects.json>", "--host", "127.0.0.1", "--port", "8766"],
  "working_directory": "<repository directory>",
  "log_file": "<repository directory>/runs/local/dashboard.log"
}
```

`python` 指向真实 Python 可执行文件；Windows 启动器要求 `.exe`，不接受 shell shim。`arguments` 是 argv 数组，不是 shell 命令。旧 Windows 启动器还支持单独的 `program` 字段指定 `flflow.py`，此时 `arguments` 从 `serve` 开始。私有启动配置放在 `runs/` 下，并仅由本机维护者编辑。

双仓库可参照 `examples/projects.example.json` 配置各自的监控文件。共享服务不连接配置中的 `sessions_api_url`，不提供会话控制；原生任务和已落盘 agent 任务只投影脱敏状态。

### 只读服务与 Linux 常驻

`share_server.py` 提供独立只读入口，不创建 Codex 执行器，不开放机器配置、会话控制、原始验收文件或本地日志。可先以前台运行单项目配置：

```sh
python3 -B share_server.py --monitor-config branch-monitor.json --host 127.0.0.1 --port 8765
```

Linux 上确认配置后可在 tmux 中常驻：

```sh
tmux new-session -d -s flflow-dashboard 'exec python3 -B share_server.py --monitor-config branch-monitor.json --host 127.0.0.1 --port 8765'
tmux capture-pane -t flflow-dashboard -p
```

双仓库改用 `--projects-config projects.json`。服务默认定期采集，`--refresh-interval` 指定秒数；页面“读取最新状态”通过同源 JSON 请求触发采集，30 秒内重复请求合并，后台和手动采集串行执行。

已知 backend 的新适配 PR 使用 `codex/backend-<backend>-…` 分支名即可被自动发现；`corex` 另接受 `codex/backend-iluvatar-…`。发现只读取准确目标 base 分支的打开 PR，多个候选报告歧义。自定义 `backend_pr_discovery` 覆盖该 backend 的默认命名规则；手动有效关联和已验证合入结果优先。GitHub 查询失败会显示发现错误并保留已有配置。

### 已有开发平台或 HTTPS 反向代理

将平台端口代理连接到共享服务，配置 HTTPS 和团队访问权限。只有确认网关负责访问认证时才使用 `--trusted-ingress`；该参数不会创建认证。平台通过容器网络访问时，绑定容器可达的地址。

平台使用路径前缀时指定 `--base-path /your-prefix/`，通过带末尾 `/` 的入口访问。页面 API 使用相对 URL，网关应只转发看板服务端口。

独立反向代理可接入团队登录系统，也可设置 HTTP Basic 登录，密码通过 `FLFLOW_SHARE_PASSWORD` 环境变量注入，用户名通过 `--username` 设置。Basic 登录必须由 HTTPS 入口保护，密码不写入命令、源码、URL 或 PR。服务监听 loopback，由 Nginx 等代理转发到 `http://127.0.0.1:8765`。

### 需要密码的 LAN 直接 HTTPS

保留密码保护时，直接运行共享服务：

```sh
python -B share_server.py --projects-config projects.json --host 0.0.0.0 --port 8767 --tls-cert runs/tls/dashboard-cert.pem --tls-key runs/tls/dashboard-key.pem
```

部署环境先设置 `FLFLOW_SHARE_PASSWORD`，访问者使用默认用户名 `viewer` 或 `--username` 指定的用户名登录。`0.0.0.0` 表示所有 IPv4 接口，仍需限定防火墙允许的网段；免登录模式必须绑定具体 RFC1918 地址。

`--tls-cert` 和 `--tls-key` 必须成对提供，证书加载发生在采集缓存和后台线程启动之前；缺失、不可读或 PEM 无效会明确报错。直连 HTTPS 要求 TLS 1.2 或以上，原有 loopback HTTP 与反向代理部署兼容。

### 临时 SSH 访问

有开发机 SSH 权限的维护者可自行转发只读服务端口：

```sh
ssh -N -L 127.0.0.1:8765:127.0.0.1:8765 <development-host>
```

然后访问 `http://127.0.0.1:8765/`。各访问者需要自己的 SSH 隧道。

## 验证与维护

- 检查共享入口的 `/healthz` 和 `/api/capabilities`，确认 `read_only: true`。
- 从访问者网络检查首页、CI / Gate 数据以及带前缀的 API。
- 任务、机器、日志和原始证据接口不可访问；仅 `/api/branch-refresh` 接受符合认证和同源检查的 JSON POST。
- Windows 查看配置的 stdout、`.stderr` 和启动器日志；Linux 查看 tmux 输出。
- 采集失败显示最后采集时间和错误，不把未知状态当作通过。

Codex / Claude / FlagScale Agent 的创建、启动、跟进和配置只通过本机完整控制台使用。Claude 与 FlagScale 的 API key 分别使用当前 Windows 用户的 DPAPI 加密保存在忽略目录；服务或计划任务使用同一 Windows 用户。LAN 不初始化 agent 设置或执行器，`/api/agent-settings`、`/api/agent-providers` 及私有原件接口均不开放。

原生子会话和已落盘任务的共享状态仅包含安全的 backend、分支、状态、阶段和时间。历史实验记录在 LAN 页面指向 GitHub PR，本机入口可读取经哈希验证的 UTF-8 存档。更多配置见 [高级配置与验收参考](docs/REFERENCE.md)。
