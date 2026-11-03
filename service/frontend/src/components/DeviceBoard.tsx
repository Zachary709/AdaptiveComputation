import type { NodeDocument } from "../types";
import type { NvitopState } from "../hooks/useNvitop";
import { diskForPath, formatBytes, shortNodeName } from "../format";

interface DeviceBoardProps {
  nodes: NodeDocument[];
  live: Record<string, NvitopState>;
}

function memoryText(
  node: NodeDocument,
  payload: NvitopState["payload"],
): string {
  if (payload) {
    const total = payload.memory_total_mib * 1024 ** 2;
    const used = payload.memory_used_mib * 1024 ** 2;
    if (total <= 0) {
      return "-";
    }
    return `${formatBytes(used)} / ${formatBytes(total)} (${((used / total) * 100).toFixed(1)}%)`;
  }
  const total = node.hardware?.memory?.total_bytes ?? 0;
  const available = node.hardware?.memory?.available_bytes ?? 0;
  if (total <= 0) {
    return "-";
  }
  const used = Math.max(total - available, 0);
  return `${formatBytes(used)} / ${formatBytes(total)} (${((used / total) * 100).toFixed(1)}%)`;
}

function cpuText(payload: NvitopState["payload"]): string {
  if (!payload) {
    return "-";
  }
  return `${payload.cpu_percent.toFixed(1)}%`;
}

export function DeviceBoard({ nodes, live }: DeviceBoardProps) {
  return (
    <section className="panel board">
      <div className="board-title">
        <h2>设备信息</h2>
        <span className="muted">系统、驱动、CPU 与内存</span>
      </div>
      <div className="device-card-list">
        {nodes.map((node) => {
          const disk = diskForPath(node.hardware?.disks, node.work_path);
          const used = disk
            ? Math.max((disk.total_bytes ?? 0) - (disk.available_bytes ?? 0), 0)
            : 0;
          const payload = live[node.alias]?.payload ?? null;
          return (
            <article key={node.alias} className="device-card">
              <header>
                <strong>{shortNodeName(node.alias)}</strong>
              </header>
              <dl className="facts">
                <dt>系统版本</dt>
                <dd>{node.software?.os ?? "-"}</dd>
                <dt>显卡驱动版本</dt>
                <dd>{node.software?.driver_version ?? "-"}</dd>
                <dt>工作路径</dt>
                <dd className="mono">{node.work_path ?? "未注册"}</dd>
                <dt>工作盘</dt>
                <dd>
                  {disk
                    ? `${formatBytes(used)} 已用 / ${formatBytes(disk.total_bytes)}`
                    : "未注册"}
                </dd>
                <dt>CPU 占用</dt>
                <dd>{cpuText(payload)}</dd>
                <dt>内存占用</dt>
                <dd>{memoryText(node, payload)}</dd>
              </dl>
            </article>
          );
        })}
      </div>
    </section>
  );
}
