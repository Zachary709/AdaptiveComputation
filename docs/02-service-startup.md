# 第二部分：服务启动的约束

这一部分规定服务可以被启动、发布的唯一方式，以及启动过程允许触碰的范围。代码位置见
[`01-service-code.md`](01-service-code.md)，操作步骤见
[`03-skill-usage.md`](03-skill-usage.md)。

## 启动前置条件

- `uv sync` 已完成，`service/` 与 `skill/` 使用的依赖可用。
- `tailscale up` 已完成，本机在 tailnet 内。
- 当前用户有权修改 serve 配置。没有时先执行一次
  `sudo tailscale set --operator=$USER`，不要用 `sudo` 启动服务来绕过。
- `--ssh-config` 指向的配置文件存在且当前用户可读。

任一条件不满足时停止，报告缺失项。不要用关闭主机校验、改用密码登录或改用公共地址
来绕过。

## 监听

- 服务只监听回环地址。默认 `--host 127.0.0.1`，`loopback_host` 只接受
  `127.0.0.1`、`localhost`、`::1`。
- 不得监听 `0.0.0.0`、`::` 或任何具体网卡地址，不得在前面加 nginx、caddy、frp 之类
  的公共反向代理。
- 同一端口上不得挂第二个监听。端口被占用时改 `--port`，或者停止占用者，不要并行启动
  两个实例。

## 发布

- 发布方式只有一种：`tailscale serve` 以 HTTPS 发布到 tailnet。服务内部执行
  `tailscale serve --bg --https=<port> http://127.0.0.1:<port>`。
- 发布端口默认 443。该端口已被其他应用占用时用 `--tailscale-port` 换端口，本机使用
  8443，发布地址形如 `https://<machine>.<tailnet>.ts.net:8443/`。
- 该端口上已存在指向其他目标的规则时，服务启动失败并保留原规则。不要先删掉别人的
  规则，换一个端口。
- Funnel 永远不启用。不得把看板发布到公共地址，不得使用纯 HTTP 对外提供接口。
- 服务不校验 bearer token，访问控制完全依赖 tailnet 与 `tailscale serve` 的 HTTPS
  监听。因此必须先满足“只监听回环 + 只经 tailscale serve 发布”这两条，否则等于公开。
- 服务正常退出时会移除自己的 serve 规则。判定可用性必须同时看回环监听和 tailnet
  HTTPS 地址；出现“本机接口正常、tailnet 地址不通”时按发布缺失处理，不能当作健康。
- 启动失败的判断依据是启动输出：服务会打印 tailnet URL，端口冲突会打印已有目标。
  把这些输出原样报告，不要自行改写 serve 配置。

## systemd 用户单元

服务以 systemd 用户单元常驻，这是唯一允许的常驻方式：

```ini
[Unit]
Description=Adaptive resource board service
After=default.target

[Service]
Type=simple
ExecStart=/absolute/path/to/service/scripts/adaptive-service --tailscale-port 8443
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now adaptive-service.service
```

不得用 `nohup`、`tmux`、`&` 后台进程或直接运行脚本代替单元来长期托管服务。单元已经
存在时用 `systemctl --user start|restart <unit>` 操作，不要另起一个进程。

## 由任务启动的服务

通过本 skill 派发的任务如果启动长期服务（演示、推理接口、看板、网页），同样受这一
部分约束：

- 任务命令只能让服务监听 `127.0.0.1`。
- 对外访问由控制器上的 `tailscale serve` 发布，不得在任务命令里开启公共监听、纯
  HTTP 对外端口或 Funnel。
- 这类清单在审核阶段就要按这一条检查；不符合的清单不进入执行。

## 远端执行与 SSH

- 永远不要读取、打印、复制、上传或修改私钥。不要用 `cat ~/.ssh/id_*`、`ssh-add -L`
  代替发现主机，也不要把密钥内容写进提示词、计划、日志或提交。
- 服务只用 `ssh -G` 获取 SSH 元数据，并把配置的身份文件交给 OpenSSH，自己不打开
  密钥。
- 使用 `BatchMode=yes` 行为。认证或网络失败时停下并报告，不要退回密码登录，不要
  关闭主机校验。
- `adaptive-runner run` 与 `adaptive-runner repair` 属于远端代码执行。除非用户已经
  明确授权执行，生成命令后先请求确认。
- 不要把打包的调度运行时或 SSH 材料复制到远端节点。

## 执行边界

可执行的计划只能来自资源看板服务：通过 `submit` 申请并占用计划，runner 拒绝没有
`server_plan_id`、未被当前用户占用或设备超出已占用计划的计划文件。不要手工编辑
`plan.json`、`allocations.json`、`plan-cache.json`，也不要用脚本、模板或旧计划副本
构造计划；本地 `plan` 命令只用于离线评审，其输出不执行。

计划审核通过后，执行只限于提交已审核的任务并接收结果：

- 优先用打包 runner 的 `submit`；需要单独执行时用 `submit` 打印的计划路径调用 `run`。
  需要保存结果时用 `--output`。不要另外写 shell heredoc、Python 解析脚本、`jq`、
  `awk` 之类的临时汇报脚本。
- 结果成功以后不要再跑第二条 SSH 命令、远端 `ps`、`nvidia-smi`、`find`、`df` 或
  环境扫描来解释结果。分发器已经做了 GPU 与命令预检、短环境检查和结果组装。
- 不要每次成功后重跑环境安装任务。`environment_maintenance` 任务是备用候选，只在
  检查失败后通过 `repair` 显式执行一次。
- 不要为了生成报告而刷新服务清单。只在规划新放置或从资源故障中恢复时刷新。
- 同一清单重复执行时复用缓存的计划；清单或控制器源码根改变、缓存失效、或用户要求
  `--refresh-plan` 时才重新规划。
- 分发期间不要复制未声明的源码、改写清单、安装软件包或创建无关文件。
- runner 不会自行修复环境，也不会在环境失败后重试负载。由 agent 决定下一步，并用
  `repair` 执行、检查和上报。
- 直接报告 runner 的结果 JSON。结果不完整或失败时停下并报告节点与任务错误。

本地执行产物只允许：拉取的快照、已审核的计划、结果 JSON、修复记录、outbox，以及
`.adaptive-computation/` 下被忽略的产物缓存。

## 安装位置与代理隧道

- 不要安装到 `/`、`/root`、`/usr`、`/opt`、`~/.cache`、`~/.local`。uv 缓存、Python
  运行时、环境和临时文件都放在所选数据盘的工作根目录下。
- 修复命令通过 `bash -c` 运行，不加载登录 shell。使用绝对路径或在命令里设置 `PATH`。
- 代理转发只允许回环地址，必须保留 SSH 子进程 PID，并在成功、失败、超时或取消时
  终止并等待该 PID。不要使用未跟踪的 `ssh -f` 进程。每次下载使用独立的转发端口，
  确认隧道进程与远端回环监听都已消失后再开始下一次下载。
- 每次安装或同步命令都把 `UV_CACHE_DIR`、`UV_PYTHON_INSTALL_DIR`、`UV_TOOL_DIR`、
  `TMPDIR`、`UV_PROJECT_ENVIRONMENT` 设在 `{work_dir}/envir` 下。

完整的 agent 侧契约见 [`../skill/SKILL.md`](../skill/SKILL.md)。
