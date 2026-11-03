# 第三部分：使用方法

这一部分给出使用本仓库的操作顺序。代码位置见
[`01-service-code.md`](01-service-code.md)，启动与发布规则见
[`02-service-startup.md`](02-service-startup.md)。agent 侧的完整契约在
[`../skill/SKILL.md`](../skill/SKILL.md)，字段细节在
[`../skill/references/`](../skill/references/)。

## 1. 启动资源看板服务

```bash
uv sync
service/scripts/adaptive-service \
  --state-dir ~/.adaptive-computation \
  --ssh-config ~/.ssh/config \
  --interval 60 --tailscale-port 8443
```

启动输出会打印 tailnet URL，例如 `https://<controller>.<tailnet>.ts.net:8443/`。
把这个地址作为 `ADAPTIVE_SERVER`。本机监听为 `http://127.0.0.1:8765`。

常驻运行按 [`02-service-startup.md`](02-service-startup.md) 里的 systemd 用户单元
安装，不要用后台进程长期托管。

看板前端需要时构建：

```bash
npm ci --prefix service/frontend
npm run build --prefix service/frontend
```

健康检查：

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8765/healthz
curl -s -o /dev/null -w '%{http_code}\n' https://<controller>.<tailnet>.ts.net:8443/healthz
```

两个都要是 200。只有本机通过、tailnet 地址不通时，按发布缺失处理。

## 2. 注册调度 skill

skill 目录自包含，整体复制即完整。运行时设置 `SKILL_DIR`：

```bash
export SKILL_DIR=/absolute/path/to/AdaptiveComputation/skill
```

注册到 agent 时把这个目录加入 skill 搜索路径，或用 `--skill "$SKILL_DIR"` 传入。工作
目录选在工作负载项目里，不要选在仓库里。

## 3. 拉取快照

`submit` 会自行拉取快照；下面的命令只用于诊断或离线评审：

```bash
mkdir -p .adaptive-computation
"$SKILL_DIR/scripts/adaptive-runner" inventory pull \
  --server "$ADAPTIVE_SERVER" \
  --snapshot .adaptive-computation/snapshot.json
```

快照是放置的唯一依据。不要另外运行清单采集器，也不要凭记忆指定节点。离线评审产出的
计划不能执行。

## 4. 写任务清单

把 `$SKILL_DIR/assets/examples/adaptive_tasks.json` 复制成
`.adaptive-computation/tasks.json` 再改。最小清单：

```json
{
  "tasks": [
    {
      "id": "train",
      "command": "uv run python train.py --rank {rank}",
      "gpus": 4,
      "distribution": "one-to-one",
      "gpu_type": "A100",
      "min_gpu_memory_gib": 40,
      "min_memory": "32GiB",
      "uv_env": "uv",
      "depends_on": ["prepare"]
    }
  ]
}
```

优先写明确的资源要求，不要硬编码节点名。需要的源码、模型、数据集都要有 `artifacts`
声明。字段说明见 [`../skill/references/task-manifest.md`](../skill/references/task-manifest.md)。

## 5. 提交执行

可执行的计划只能来自资源看板服务。`submit` 向服务申请一个计划，占用成功后从该计划的
设备集重建执行计划再分发，并只缓存选中的 `plan_id`。`run` 拒绝没有 `server_plan_id`
的计划、被其他用户占用的计划，以及设备超出已占用计划的计划。每份计划都按固定设备集
处理，`scope: all` 也一样：服务在占用时把设备列表展开，之后这份列表必须全部可用。
占用后重新检查计划中的每台设备，任一设备不可达、不在快照中或已被占用，整个计划即
不可行，`submit` 释放占用并尝试别的计划，不会把不可用设备剔除后在剩余设备上执行。
不要手工编辑
`plan.json`、`allocations.json` 或 `plan-cache.json`，也不要用脚本、模板或上一次的
计划副本构造计划。

清单与控制器源码根没有变化时先用 `submit`：

```bash
"$SKILL_DIR/scripts/adaptive-runner" submit .adaptive-computation/tasks.json \
  --server "$ADAPTIVE_SERVER" \
  --task train --mode tmux \
  --output .adaptive-computation/train-result.json
```

所有计划都被占用时它以状态码 3 退出，打印每个失败原因；此时修复环境或换计划后再试，
或加 `--plan-id ID`。不要改用本地放置。

首次审核时查看服务计划并生成本地预览：

```bash
"$SKILL_DIR/scripts/adaptive-runner" plans list
"$SKILL_DIR/scripts/adaptive-runner" plans show --id ID
"$SKILL_DIR/scripts/adaptive-runner" submit .adaptive-computation/tasks.json \
  --server "$ADAPTIVE_SERVER" --dry-run --json
```

本地 `plan` 命令只用于服务不可达时的离线评审，其输出不能被 `run` 执行。

`--dry-run` 在不建立 SSH 连接的情况下验证命令展开。分离式 `tmux` 运行要在前一个模块
确认结束后，用 `--task` 逐个提交依赖模块。`--allow-snapshot-change`、
`--ignore-allocations`、`--allow-unallocated`、`--gpu-state snapshot` 与
`--no-reserve` 需要用户明确同意。

计划管理：

```bash
"$SKILL_DIR/scripts/adaptive-runner" plans list
"$SKILL_DIR/scripts/adaptive-runner" plans show --id ID
"$SKILL_DIR/scripts/adaptive-runner" plans claim --id ID
"$SKILL_DIR/scripts/adaptive-runner" plans release --id ID
```

`plans claim` 只登记占用，不生成执行计划；执行计划仍由 `submit` 生成。

## 6. 登记节点路径

使用某个节点之前登记固定工作目录，以及 `<work_path>/data` 与 `<work_path>/models`：

```bash
"$SKILL_DIR/scripts/adaptive-runner" work-path --node GPU-01
"$SKILL_DIR/scripts/adaptive-runner" data-path --node GPU-01
"$SKILL_DIR/scripts/adaptive-runner" model-path --node GPU-01
```

路径取自 `skill/local.json` 的 `nodes.GPU-01`。该文件不入库，模板见
`skill/assets/local.example.json`；也可以直接在命令里用 `--path` 给出
`<work_path>`、`<work_path>/data` 和 `<work_path>/models`。

skill 会先做只读检查：路径必须存在、是目录且可写；数据根目录只能包含含数据文件的
子目录；模型根目录只能包含分片完整的权重目录。检查失败时登记停止并列出有问题的条目。

## 7. 处理环境失败

`run` 在任务的 `environment_check` 失败时以 `reason: "environment_mismatch"` 停止，
返回检查输出和 `suggested_repair`，不会自行修复或重试。审核一条修复命令和一条检查
命令后运行一次：

```bash
"$SKILL_DIR/scripts/adaptive-runner" repair \
  --node GPU-01 \
  --command '/data/<user>/adaptive/tools/uv sync --frozen' \
  --check '/data/<user>/adaptive/tools/uv run python -c "import torch"' \
  --manager uv --path /data/<user>/adaptive/tools/uv \
  --version 'uv 0.5.4' --uv-version 'uv 0.5.4' --python-version 3.12.14 \
  --project-path /data/<user>/adaptive/projects/demo \
  --lockfile /data/<user>/adaptive/projects/demo/uv.lock \
  --lock-sha256 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef \
  --cache-hit --report
```

记录写在 `.adaptive-computation/repairs/`，上报失败时进入 `outbox.jsonl`，下次修复时
重试。服务用只读探针复核，不一致时标记 `conflict`，规划器随后把该环境视为不可用。

## 8. 报告结果

直接报告 runner 的结果 JSON，保留任务 id、分配、退出码和失败节点消息。不要在成功后
补跑诊断，也不要在任务开始后把任务挪到别的 GPU。资源失败后的重试需要通过 `submit`
重新申请计划。

## 常见问题

| 现象 | 处理 |
| --- | --- |
| 服务不可达 | 报告地址与错误并停止；最近快照只能用于离线评审，不执行任何任务 |
| tailnet 地址不通但本机正常 | 按发布缺失处理，报告 `tailscale serve status` 与期望端口，不要自行改发布规则 |
| 没有匹配的 GPU | 说明哪项要求没有满足，等待、刷新清单，或请用户放宽要求；不要抢占忙碌 GPU |
| SSH 失败 | 报告别名与生效目标，请用户修复 `~/.ssh/config` 或连通性；永远不要索取私钥 |
| 环境不匹配 | 报告检查输出与候选，只修复受影响节点，上报后重新规划 |
| 计划占用冲突 | 看板或 `plans` 查看占用者，换计划或等待释放 |
