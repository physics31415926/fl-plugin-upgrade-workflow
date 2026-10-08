# FL 插件适配工作台

在同一页面跟踪 **vLLM-Plugin-FL** 和 **SGLang-Plugin-FL** 的适配进展，查看各 backend 的 PR、CI、Gate 和 agent 任务。

![双仓库适配进展快照](docs/images/dashboard-demo.png)

*只读总览快照。在线状态以最新采集结果为准。*

- 双仓库独立选择分支，自动关联规范命名的适配 PR。
- 本机通过 Codex 或 Claude 创建任务，查看进度、跟进和处理授权。
- 展示原生子会话与历史实验记录；有本地验收存档时优先打开，否则链接到 PR。
- 本机完整控制，局域网免登录只读。

## 快速启动

需要 **Python 3.11+、Git** 和两个插件的本地 checkout。建议安装并登录 `gh` 读取 GitHub 状态；使用 agent 时还需对应 CLI 和 SSH 环境。

首次使用时复制配置（PowerShell）；已有配置可直接沿用：

```powershell
Copy-Item projects.example.json projects.json
Copy-Item branch-monitor.example.json branch-monitor.json
Copy-Item branch-monitor.sglang.example.json branch-monitor.sglang.json
```

编辑两份监控配置的 `repo`、`remote`、`branch`，确认各项目的会话与验收目录独立。暂不使用命令编排时，删除 `projects.json` 中的 `workflow_config`。

```sh
python -B flflow.py serve --projects-config projects.json --host 127.0.0.1 --port 8766
```

打开 [本机看板](http://127.0.0.1:8766/)。配置文件、运行记录和日志仅保存在本机，已由 Git 忽略。

## 页面入口

| 页面 | 用途 |
| --- | --- |
| `/` | 双仓库适配总览 |
| `/monitor?project=vllm` | 分支、PR、CI 和逐项验收详情 |
| `/sessions?project=vllm` | 验证机器、agent 配置与适配任务 |

查看 SGLang 时将 `project=vllm` 改为 `project=sglang`。任务列表默认只显示活跃任务；历史任务可在任务页查看。

代码合入、CI 和 Gate 分别展示。已有通过或部分验收记录的平台在概览 Gate 标注通过，具体测试范围和版本见记录详情。

## 更多配置

- [部署与局域网访问](DEPLOYMENT.md)：HTTPS、免登录只读和 Windows 后台常驻。
- [高级配置与验收参考](docs/REFERENCE.md)：agent 配置、原生任务接入、结果归档、命令编排与异常恢复。

Claude API key 使用 Windows 当前用户的 DPAPI 加密保存；局域网入口只提供脱敏监控信息与 PR 链接。
