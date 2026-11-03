import type { GpuDevice, NodeDocument, NvitopDevice, NvitopPayload, NvitopProcess, SeriesSample } from "../types";
import type { NvitopState } from "../hooks/useNvitop";
import { Sparkline } from "./Sparkline";
import { channelLabel, shortGpuName, shortNodeName } from "../format";

interface GpuBoardProps {
  nodes: NodeDocument[];
  live: Record<string, NvitopState>;
}

interface GpuView {
  index: number;
  name: string;
  total: number;
  used: number;
  utilization: number;
  temperature: number | null;
  power: number | null;
  processes: Array<{ pid: number; name: string; user: string; memory: number }>;
  error: boolean;
}

function processView(process: NvitopProcess) {
  return {
    pid: process.pid,
    name: process.name,
    user: process.user,
    memory: process.memory_mib,
  };
}

function liveView(device: NvitopDevice): GpuView {
  return {
    index: device.index,
    name: device.name,
    total: device.memory_total_mib,
    used: device.memory_used_mib,
    utilization: device.utilization_percent,
    temperature: device.temperature,
    power: device.power_w,
    processes: device.processes.map(processView),
    error: (device.name ?? "").toUpperCase().includes("ERROR"),
  };
}

function snapshotView(device: GpuDevice): GpuView {
  const total = device.memory_total_mib ?? 0;
  const free = device.memory_free_mib ?? 0;
  return {
    index: device.index,
    name: device.name ?? "",
    total,
    used: Math.max(0, total - free),
    utilization: device.utilization_gpu_percent ?? 0,
    temperature: device.temperature_c ?? null,
    power: device.power_draw_w ?? null,
    processes: (device.processes ?? []).map((process) => ({
      pid: process.pid ?? 0,
      name: process.process_name ?? "?",
      user: "",
      memory: process.used_memory_mib ?? 0,
    })),
    error: false,
  };
}

function samplesFor(payload: NvitopPayload | null, node: NodeDocument): SeriesSample[] {
  if (payload) {
    if (payload.samples && payload.samples.length > 0) {
      return payload.samples;
    }
    return payload.devices.map((device) => ({
      generated_at: payload.observed_at,
      gpu_index: device.index,
      name: device.name,
      memory_total_mib: device.memory_total_mib,
      memory_free_mib: Math.max(0, device.memory_total_mib - device.memory_used_mib),
      utilization_percent: device.utilization_percent,
    }));
  }
  const now = new Date().toISOString();
  return (node.hardware?.gpu?.devices ?? []).map((device) => ({
    generated_at: now,
    gpu_index: device.index,
    name: device.name,
    memory_total_mib: device.memory_total_mib,
    memory_free_mib: device.memory_free_mib,
    utilization_percent: device.utilization_gpu_percent,
  }));
}

function GpuPanel({ view, samples }: { view: GpuView; samples: SeriesSample[] }) {
  const users = [
    ...new Set(
      view.processes
        .filter((process) => process.memory >= 1024 && process.user)
        .map((process) => process.user),
    ),
  ];
  return (
    <article className={`gpu-card ${view.error ? "error" : ""}`}>
      <div className="gpu-card-head">
        <strong>GPU{view.index}</strong>
        <span className="gpu-card-model">{shortGpuName(view.name)}</span>
        {users.length > 0 ? (
          <span className="gpu-users">用户 {users.join("、")}</span>
        ) : null}
        <span className="gpu-card-meta">
          {view.temperature !== null ? `${view.temperature}°C` : "-"}
          {" · "}
          {view.power !== null ? `${view.power}W` : "-"}
        </span>
      </div>
      <div className="gpu-charts">
        <Sparkline
          samples={samples}
          gpuIndex={view.index}
          field="memory_used_mib"
          max={view.total || undefined}
          label="显存"
          suffix=" GiB"
          valueScale={1 / 1024}
          digits={2}
        />
        <Sparkline
          samples={samples}
          gpuIndex={view.index}
          field="utilization_percent"
          max={100}
          label="利用率"
          suffix="%"
        />
      </div>
    </article>
  );
}

function DeviceGpuBox({
  node,
  payload,
  error,
}: {
  node: NodeDocument;
  payload: NvitopPayload | null;
  error: string;
}) {
  const liveDevices = payload?.devices ?? [];
  const snapshotDevices = node.hardware?.gpu?.devices ?? [];
  const views = liveDevices.length > 0 ? liveDevices.map(liveView) : snapshotDevices.map(snapshotView);
  const samples = samplesFor(payload, node);

  return (
    <article className="device-box">
      <header className="device-box-head">
        <div>
          <strong>{shortNodeName(node.alias)}</strong>
          <span className={`badge channel-${node.channel ?? "direct"}`}>
            {channelLabel(node.channel)}
          </span>
        </div>
        {error ? (
          <span className="badge bad">nvitop 不可用</span>
        ) : payload ? null : (
          <span className="badge">连接中</span>
        )}
      </header>
      <div className="gpu-grid">
        {views.map((view) => (
          <GpuPanel key={view.index} view={view} samples={samples} />
        ))}
        {views.length === 0 ? <span className="muted">没有 GPU</span> : null}
      </div>
    </article>
  );
}

export function GpuBoard({ nodes, live }: GpuBoardProps) {
  const ordered = [...nodes].sort((left, right) =>
    shortNodeName(left.alias).localeCompare(shortNodeName(right.alias)),
  );
  return (
    <section className="panel board">
      <div className="board-title">
        <h2>显卡</h2>
        <span className="muted">每 5 秒刷新</span>
      </div>
      <div className="gpu-board">
        {ordered.map((node) => (
          <DeviceGpuBox
            key={node.alias}
            node={node}
            payload={live[node.alias]?.payload ?? null}
            error={live[node.alias]?.error ?? ""}
          />
        ))}
      </div>
    </section>
  );
}
