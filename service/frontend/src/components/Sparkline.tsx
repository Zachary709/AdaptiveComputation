import type { SeriesSample } from "../types";

type SparkField = "utilization_percent" | "memory_used_mib";

interface SparklineProps {
  samples: SeriesSample[];
  gpuIndex: number;
  field: SparkField;
  max?: number;
  label: string;
  suffix?: string;
  valueScale?: number;
  digits?: number;
  windowMs?: number;
}

function fieldValue(sample: SeriesSample, field: SparkField): number {
  if (field === "memory_used_mib") {
    const total = Number(sample.memory_total_mib ?? 0);
    const free = Number(sample.memory_free_mib ?? 0);
    return Math.max(0, total - free);
  }
  return Number(sample.utilization_percent ?? 0);
}

function toneFor(ratio: number): "good" | "warn" | "bad" {
  if (ratio >= 0.85) {
    return "bad";
  }
  if (ratio >= 0.6) {
    return "warn";
  }
  return "good";
}

export function Sparkline({
  samples,
  gpuIndex,
  field,
  max,
  label,
  suffix,
  valueScale = 1,
  digits = 1,
  windowMs = 5 * 60 * 1000,
}: SparklineProps) {
  const timed = samples
    .filter((sample) => sample.gpu_index === gpuIndex)
    .map((sample) => ({ sample, time: Date.parse(sample.generated_at) }))
    .filter((item) => Number.isFinite(item.time));
  const latestTime =
    timed.length > 0 ? Math.max(...timed.map((item) => item.time)) : Date.now();
  const visible = timed
    .filter((item) => latestTime - item.time <= windowMs)
    .sort((left, right) => left.time - right.time);
  const points = visible.map((item) => fieldValue(item.sample, field));
  if (points.length === 0) {
    return (
      <div className="sparkline">
        <span className="muted">{label}: 暂无样本</span>
      </div>
    );
  }

  const ceiling = Math.max(max ?? Math.max(...points, 1), 1);
  const width = 360;
  const height = 92;
  const left = 64;
  const right = 352;
  const top = 10;
  const bottom = 78;
  const plotWidth = right - left;
  const barWidth = Math.max(2, Math.min(6, plotWidth / 60));
  const yFor = (value: number) =>
    bottom - (Math.min(Math.max(value, 0), ceiling) / ceiling) * (bottom - top);
  const xFor = (time: number) =>
    right - barWidth / 2 - ((latestTime - time) / windowMs) * (plotWidth - barWidth);
  const latest = points[points.length - 1];
  const tone = toneFor(latest / ceiling);
  const format = (value: number) => (value * valueScale).toFixed(digits);
  const ticks = [ceiling, ceiling / 2, 0];
  const tickYs = [top, (top + bottom) / 2, bottom];

  return (
    <div className="sparkline">
      <div className="sparkline-header">
        <span>{label}</span>
        <strong className={tone}>
          {format(latest)}
          {suffix ?? ""}
        </strong>
      </div>
      <svg viewBox={`0 0 ${width} ${height}`}>
        {ticks.map((tick, index) => (
          <g key={index}>
            <line
              x1={left}
              y1={tickYs[index]}
              x2={right}
              y2={tickYs[index]}
              className="sparkline-grid"
            />
            <text x={left - 6} y={tickYs[index] + 3} textAnchor="end" className="sparkline-tick">
              {format(tick)}
              {suffix ?? ""}
            </text>
          </g>
        ))}
        {visible.map((item, index) => {
          const value = Math.min(Math.max(fieldValue(item.sample, field), 0), ceiling);
          const y = yFor(value);
          const height = Math.max(bottom - y, 2);
          const x = xFor(item.time);
          return (
            <rect
              key={`${item.time}-${index}`}
              x={(x - barWidth / 2).toFixed(1)}
              y={(bottom - height).toFixed(1)}
              width={barWidth.toFixed(1)}
              height={height.toFixed(1)}
              rx={1}
              className={`sparkline-bar ${toneFor(value / ceiling)}`}
            />
          );
        })}
      </svg>
    </div>
  );
}
