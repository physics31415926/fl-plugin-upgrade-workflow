# 团队只读看板部署

`share_server.py` 提供团队只读入口，独立于本机任务控制台。它不创建 Codex 执行器，不提供机器配置、会话控制、原始验收文件或本地日志接口。浏览器仅获取已脱敏的分支监控结果。

## 运行环境

- Python 3.11+、Git、tmux。
- 一份插件仓库，用于读取目标分支的用例和完整 Git tree；仓库需要包含验收提交。
- `branch-monitor.json`：从示例复制并填写服务器上的仓库路径、remote、目标分支和证据目录。
- 可选：服务器自己的 GitHub 只读凭据，用于提高 API 配额。凭据通过部署环境提供。

源码通过 Git 或发布归档部署。机器档案、SSH 配置、私钥、个人 Codex 登录目录均不随源码迁移。验收结果应由维护者单独导入配置的证据目录；原始文件仍由维护者管理。

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

服务默认定期采集 Git/CI 状态；`--refresh-interval` 设置采集间隔。监控状态与进程必须由一个实例管理，持久保存插件仓库和证据目录。

## 访问入口

### 已有带登录认证的开发平台入口

将平台端口代理连接到本机服务，配置 HTTPS 和团队访问权限。若平台网关通过容器网络访问服务，需要绑定容器可达地址；只有确认网关负责访问认证时才使用 `--trusted-ingress`。这一参数声明外部网关已经配置好，不会自动创建认证。

平台使用路径前缀时，启动服务指定 `--base-path /your-prefix/`，并让浏览器通过带末尾 `/` 的入口访问。页面 API 使用相对 URL。网关应只转发看板服务端口。

### 独立 HTTPS 反向代理

在同一主机用 Nginx 等入口提供 HTTPS。可接入团队登录系统；也可给看板设置独立 HTTP Basic 登录，密码通过 `FLFLOW_SHARE_PASSWORD` 环境变量注入，用户名通过 `--username` 设置。Basic 登录需要由 HTTPS 入口保护。不要将密码写入命令参数、源码或 PR。

服务保持监听 loopback，反向代理将请求转发到 `http://127.0.0.1:8765`。共享服务具有独立只读路由，不依赖放宽本机任务控制台的 Host/Origin 检查。

### 临时 SSH 访问

有开发机 SSH 权限的维护者可转发端口：

```sh
ssh -N -L 127.0.0.1:8765:127.0.0.1:8765 <development-host>
```

然后访问 `http://127.0.0.1:8765/`。各访问者需自行建立隧道；这不是一个可直接转发给团队的公共网址。

## 验证与维护

- 在服务器检查 `/healthz` 和 `/api/capabilities`，确认 `read_only: true`。
- 从实际团队入口检查首页、CI/Gate 数据以及带前缀的 API 请求。
- 确认任务、机器、日志和原始证据接口不可访问，所有 POST 请求均被拒绝。
- 查看 `tmux capture-pane -t flflow-dashboard -p` 排查启动或采集错误。
- 页面保留最后采集时间；采集失败不会把未知状态伪装成通过。

公网或团队入口尚未创建时，仅在服务器看到监听端口并不代表其他用户已经能够访问。交付地址前应从访问者网络验证。
