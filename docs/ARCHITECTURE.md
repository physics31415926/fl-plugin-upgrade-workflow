# 目录结构与项目扩展

## 按职责组织

| 路径 | 职责 |
| --- | --- |
| fl_dashboard/monitoring/ | 分支、PR、CI、验收记录与采集器注册 |
| fl_dashboard/projects/ | 项目配置、项目状态隔离、原有任务服务转接 |
| fl_dashboard/agents/ | Codex、Claude、FlagScale 执行与配置 |
| fl_dashboard/resources/ | JumpServer 授权资产、SSH 资源采集、本地缓存与可用性状态 |
| fl_dashboard/vendor/jms_top/ | 固定版本的 jms-top 标准库解析与探测核心，附来源及许可证 |
| fl_dashboard/tasks/ | 自动任务准备、SSH 同步、原生及只读任务观察 |
| fl_dashboard/services/ | 本机控制、LAN 只读投影与授权代理、分享凭证及工作流编排 |
| fl_dashboard/deployment/ | 配置向导、启动器实现 |
| fl_dashboard/paths.py | 应用根目录、页面和示例资源路径 |
| web/templates/ | 总览、分支详情、任务和工作流页面 |
| examples/ | 可复制的示例配置，不存运行数据 |
| tests/ | Python 契约测试、浏览器脚本回归 |
| runs/ | Git 忽略的本机状态、凭据与日志 |

根目录只保留 dashboard.py 统一入口。scripts/ 存放独立命令与 Windows 启动脚本；使用旧根目录命令的部署需把脚本路径改到 scripts/，并更新计划任务的启动路径。

项目注册负责隔离每个项目的配置、采集和错误。监控不依赖任务执行器；局域网模式保持延迟导入，不实例化 agent 或凭据存储。只读服务通过字段白名单输出状态，验收表格经过脱敏；增加新采集字段时必须同步检查这条边界。可选的 access_shares 保存有期限的令牌摘要，share_gateway 对获授权的 HTTPS 请求检查 Host、Origin、期限和撤销状态，再按路由白名单转发到既有 loopback 控制服务；分享创建与撤销始终仅本机可用。

## 新增同类项目

在 projects.json 的 projects 列表增加一个项目，配置唯一 id、展示 label、独立 monitor_config 路径。监控配置沿用 engine: vllm 或 sglang，填写 repo、remote、branch、github_repo 和 backend_labels。各项目的运行目录不可重叠。多个项目可共用同一种 engine，不需要复制服务端代码或页面。

示例配置已移到 examples/，复制到自己的配置目录后再修改。相对路径仍相对于配置文件所在目录；原有本机配置不会自动移动。配置向导继续负责内置 vLLM/SGLang 的便捷配置。

## 接入不同类型的监控

1. 在 fl_dashboard/monitoring/ 下实现独立采集器，遵循 registry.py 的 Monitor 接口。
2. 在可信启动代码中调用 register_monitor("new_engine", Factory)，然后调用服务 main()。
3. 新项目监控配置设置 engine: new_engine，沿用 projects.json 接入。

配置文件只能选择已经注册的名称，不接受任意 Python 导入路径。内置类型不可被覆盖。自定义采集器默认只提供监控，不会调用 vLLM/SGLang 专用的远端适配提示词、任务执行器或工作流。将来增加新任务类型时需显式实现对应执行能力。

可信启动代码示例（MyMonitor 为你自己的实现）：

    from fl_dashboard.monitoring.registry import register_monitor
    from fl_dashboard.monitoring.my_monitor import MyMonitor
    from fl_dashboard.services.flflow import main

    register_monitor("my_engine", MyMonitor)
    raise SystemExit(main())

局域网服务的自定义启动入口也需先注册相同采集器，再调用 fl_dashboard.services.share_server.main。没有注册的类型只报告该项目错误，不阻止其他项目显示。

采集器至少提供 branch、github_repo、config 属性和 status、refresh_branch 方法。状态使用共同字段：engine、branch、branches、head、observed_at、upstream_version、backends、errors；backends 中每项有 id、label、pr、integration、ci、acceptance。可没有 backend，但不能伪造测试通过。高级证据接口 read_evidence、acceptance_record 在不支持时应抛出 ValueError 并说明原因。公开服务仍只展示白名单字段。

## 验证修改

    python -B -m unittest discover -s tests -p "test_*.py" -v
    node --test tests/*.cjs

Windows PowerShell 可先用 Get-ChildItem tests -Filter '*.cjs' 收集路径，再传给 node --test。测试不使用真实 GPU 或 agent；项目注册测试包含第三种采集器、运行目录隔离和只读投影。改动启动路径时还需从其他工作目录检查兼容入口，并确认页面资源可加载。

示例配置统一在 examples/；旧工作流中的 strict_test.py 路径改为 scripts/strict_test.py，cases.*.example.json 改为 examples/cases.*.example.json。本机运行配置仍由 Git 忽略。

## 设备资源监控

设备资源与 PR / CI 采集分开运行。`DeviceFleet` 从已有任务 owner 的机器档案构造固定 SSH 探测，使用严格主机校验、有限并发、超时和输出上限；不建立第二套 Agent 执行器。`GET /api/devices` 读取缓存，授权的 `POST /api/devices/refresh` 发起异步采集，服务关闭时回收采集器。

`telemetry.py` 包装固定版本的 jms-top 探测和多厂商解析，第三方原件及来源记录位于 `vendor/jms_top/`。业务层只暴露规范化指标，舍弃原始输出和完整进程命令。更新第三方核心时，应同时检查来源版本、许可证、远端脚本行为、完成标记，以及未知 / 显存残留 / 多芯片板卡的判断。

设备详情不进入匿名 LAN 投影；完整权限分享通过现有网关白名单访问同一本机缓存。更改 SSH 配置或 profile 时重新识别采集目标，旧连接的数据不能继续表示新目标可用。

`JumpServerInventory` 管理本机登录和完整授权资产缓存，`JumpServerProfiles` 将当前资产转为隔离的 SSH 档案；启用后不再同步用户 SSH 列表。资源采集按来源 epoch 丢弃过期结果，`DeviceTelemetryCache` 仅持久化受限的展示指标，恢复时可用容量强制为零。JumpServer 管理 API 不加入分享网关白名单。
