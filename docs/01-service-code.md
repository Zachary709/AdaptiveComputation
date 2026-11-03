# 第一部分：服务的代码

这一部分只讲代码在哪里、由什么启动、每个包负责什么。启动与发布规则在
[`02-service-startup.md`](02-service-startup.md)，操作步骤在
[`03-skill-usage.md`](03-skill-usage.md)。

## 两个部署单元

仓库包含两套各自完整、可单独复制的运行时：

- **资源看板服务**（`service/`）：通过 SSH 只读采集节点清单，保存快照、环境记录、
  GPU 分配和审计事件，提供 HTTP API 和网页看板。它从不规划、执行或修复工作负载。
- **调度 skill**（`skill/`）：拆分任务、准备产物、规划放置、通过 SSH 派发执行，并
  执行一条已审核的修复命令。它从不采集清单，也不写服务状态。

两套运行时各带一份 `adaptive_core`，两份内容必须一致，
`skill/tests/test_adaptive_skill_bundle.py` 会逐文件比对。

## 目录布局

```text
service/adaptive_core/            服务侧共享模型与 SSH 命令构造
service/adaptive_service/         采集、存储、API、tailscale 发布
service/adaptive_service/app.py   应用工厂、lifespan、静态看板托管
service/adaptive_service/local.py 读取不入库的 local.json 默认计划
service/adaptive_service/cli.py   命令行入口与参数
service/adaptive_service/collector/  probe.py 负责远端只读探针，service.py 负责刷新与合并
service/adaptive_service/store/   SQLite 结构与查询
service/adaptive_service/api/     节点、清单、环境、分配、审计、事件、nvitop 路由
service/adaptive_service/tailscale.py  状态解析、serve 状态检查、HTTPS 发布与清理
service/frontend/                 Vite + React + TypeScript 看板
service/scripts/adaptive-service  服务启动器
service/tests/                    服务测试
service/references/               清单字段说明

skill/adaptive_core/              skill 侧的同一份共享模型与 SSH 命令构造
skill/opensmi/                    规划、分发、产物、执行、修复、HTTP 客户端、CLI
skill/scripts/adaptive-runner     skill 启动器
skill/tests/                      skill 测试
skill/references/                 工作流、清单、环境、安全、运行时说明
skill/assets/                     任务清单示例与冒烟样例
skill/SKILL.md                    agent 的行为契约

development/                       仓库检查脚本与独立可执行文件的构建
dist/adaptive-runner               由 skill 包构建的 zipapp
```

## 启动器与包解析

- `skill/scripts/adaptive-runner` 把 `skill/` 放在 `sys.path` 最前，调用
  `opensmi.cli.main`。
- `service/scripts/adaptive-service` 把 `service/` 放在 `sys.path` 最前，调用
  `adaptive_service.cli.main`，缺少 `adaptive_core/__init__.py` 或
  `adaptive_service/app.py` 时直接退出。
- 两个启动器都只从仓库目录解析包，不使用环境中已安装的同名包，也不在远端节点
  安装任何东西。`opensmi` 这个内部包名保留以兼容既有工具与测试。

## 模块职责

服务侧：

- `collector/probe.py`：SSH 发现、远端只读探针脚本、解析、并发、过期合并、快照
  序列化。
- `collector/service.py`：节点选择、刷新任务、快照发布、环境合并、只读复核。
- `store/db.py`：节点、快照、GPU 采样、环境记录、分配、审计的 SQLite 结构与查询。
- `store/allocations.py`：分配数据结构与 JSON 辅助函数。
- `config.py`：集群配置解析。
- `tailscale.py`：状态解析、serve 状态检查、HTTPS 发布配置与清理。
- `api/`：节点、清单、环境、分配、审计、快照事件的路由。
- `app.py`：应用工厂、生命周期、静态看板托管。

skill 侧：

- `planner.py`：任务归一化、产物规划、放置、资源预留。
- `dispatcher.py`：执行顺序、环境检查、预检、结果组装。
- `artifacts.py`：目标侧模型复用、ModelScope 获取、控制器传输回退、数据缓存、
  rsync 同步、校验标记。
- `executor.py`：GPU 空闲校验、命令引用、GPU 绑定、direct 或 tmux 执行。
- `repair.py`：执行一条已审核的修复命令和一条检查，返回结构化结果。
- `client.py`：快照、节点、路径、目录、nvitop、环境、分配、审计的 HTTP 客户端。
- `snapshot.py`：快照校验、新鲜度检查、分配转换、策略提取。
- `cli.py`：`inventory pull|show`、`inspect`、`path`、`gpu`、`environment`、
  `plan`、`allocate`、`allocations`、`release`、`run`、`repair`。

更细的模块职责见 [`../skill/references/controller.md`](../skill/references/controller.md)。

## 看板前端

`service/frontend/` 使用 Vite、React 和 TypeScript。`npm run build` 把产物写到
`service/adaptive_service/static/`，该目录不入库，由服务在 `/` 提供。前端不需要构建
时可以直接用 HTTP API。

接口清单见 [`../service/README.md`](../service/README.md)。

## 状态与数据

- 服务状态目录由 `--state-dir` 指定，`service.db` 保存节点、快照、GPU 采样、环境
  记录、分配和审计事件。
- 工作负载项目把自己的状态写在 `.adaptive-computation/`：`snapshot.json`、
  `plan.json`、分配记录、结果 JSON、`modelscope/` 与 `verified/` 产物缓存、
  `repairs/` 修复记录、`outbox.jsonl` 未送达的环境上报。

## 构建与检查

```bash
uv sync
npm ci --prefix service/frontend
./development/check.sh
```

`check.sh` 编译 `skill` 与 `service`，运行两组测试，并在前端依赖已安装时构建前端。
`./development/build-cli-pyz.sh` 从 skill 包构建独立的 `dist/adaptive-runner`
zipapp。

修改某个模块后，运行 `skill/tests/` 或 `service/tests/` 中对应的测试组。
