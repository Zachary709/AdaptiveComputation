import type { NodeDocument } from "../types";
import { channelLabel, shortGpuName, shortNodeName } from "../format";

interface DevicePickerProps {
  nodes: NodeDocument[];
  selected: Set<string>;
  onToggle: (alias: string) => void;
}

export function DevicePicker({ nodes, selected, onToggle }: DevicePickerProps) {
  return (
    <section className="device-picker">
      {nodes.map((node) => {
        const gpu = node.hardware?.gpu;
        const offline = node.reachable === false || Boolean(node.stale);
        return (
          <button
            type="button"
            key={node.alias}
            className={`device-chip ${selected.has(node.alias) ? "selected" : ""} ${
              offline ? "offline" : ""
            }`}
            onClick={() => onToggle(node.alias)}
          >
            <span className="device-chip-name">{shortNodeName(node.alias)}</span>
            <span className="device-chip-meta">
              {offline
                ? "已断开"
                : `${gpu?.count ?? 0} × ${shortGpuName(gpu?.models?.[0])}`}
            </span>
            <span
              className={`badge ${
                offline ? "bad" : `channel-${node.channel ?? "direct"}`
              }`}
            >
              {offline ? "断连" : channelLabel(node.channel)}
            </span>
          </button>
        );
      })}
    </section>
  );
}
