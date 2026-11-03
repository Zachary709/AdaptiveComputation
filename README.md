# AdaptiveComputation

本仓库包含两套可以单独复制的运行时：采集节点清单并发布网页看板的资源看板服务，以及
规划、派发并执行任务的调度 skill。两者边界清晰：服务不规划、不执行、不修复负载，
skill 不采集清单、不写服务状态。

仓库内容按三部分组织。

| 部分 | 内容 | 文档 |
| --- | --- | --- |
| 服务的代码 | 目录布局、启动器、包解析、模块职责、前端、状态与数据、构建与检查 | [`docs/01-service-code.md`](docs/01-service-code.md) |
| 服务启动的约束 | 前置条件、监听、tailscale serve 发布、systemd 用户单元、由任务启动的服务、SSH 与执行边界、安装位置与代理隧道 | [`docs/02-service-startup.md`](docs/02-service-startup.md) |
| 使用方法 | 启动服务、注册 skill、快照、清单、提交、计划、路径登记、修复、结果、常见问题 | [`docs/03-skill-usage.md`](docs/03-skill-usage.md) |

各部署单元自身的说明仍然是权威细节：

- [`service/README.md`](service/README.md)：服务启动参数、看板构建、HTTP API 清单；
- [`skill/SKILL.md`](skill/SKILL.md)：agent 行为契约与执行流程；
- [`skill/references/`](skill/references/)：工作流、任务清单、Python 环境、安全说明；
- [`service/references/`](service/references/)：清单字段说明。

项目使用 MIT 许可证，见 [`LICENSE`](LICENSE)。
