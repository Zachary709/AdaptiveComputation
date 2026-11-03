import type { Disk } from "./types";

export function formatBytes(value: number | undefined): string {
  if (value === undefined || !Number.isFinite(value) || value <= 0) {
    return "-";
  }
  const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
  let number = value;
  let unit = 0;
  while (number >= 1024 && unit < units.length - 1) {
    number /= 1024;
    unit += 1;
  }
  return `${number.toFixed(1)} ${units[unit]}`;
}

export function formatTime(value: string | null | undefined): string {
  if (!value) {
    return "-";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleString();
}

export function shortNodeName(alias: string | null | undefined): string {
  if (!alias) {
    return "-";
  }
  return alias;
}

export function formatUvVersion(value: string | null | undefined): string {
  if (!value) {
    return "";
  }
  const matched = value.match(/\b(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)\b/);
  if (matched) {
    return `uv ${matched[1]}`;
  }
  return value.replace(/\s*\([^)]*\)\s*$/, "").trim();
}

export function formatTmuxVersion(value: string | null | undefined): string {
  if (!value) {
    return "";
  }
  const matched = value.match(/\b(\d+(?:\.\d+)+[a-z]?)\b/);
  if (matched) {
    return `tmux ${matched[1]}`;
  }
  return value.replace(/\s*\([^)]*\)\s*$/, "").trim();
}

export function shortGpuName(name: string | null | undefined): string {
  if (!name) {
    return "-";
  }
  const text = name.trim();
  const rtx = text.match(/RTX\s*(\d{4})/i);
  if (rtx) {
    return rtx[1];
  }
  const letter = text.match(/\b([AHV])\s*(\d{3,4})\b/i);
  if (letter) {
    return `${letter[1].toLowerCase()}${letter[2]}`;
  }
  const digits = text.match(/\b(\d{4})\b/);
  if (digits) {
    return digits[1];
  }
  return text.replace(/^NVIDIA\s+/i, "");
}

export function formatMibAsGib(value: number | undefined): string {
  if (value === undefined || !Number.isFinite(value)) {
    return "-";
  }
  return (value / 1024).toFixed(2);
}

export function diskForPath(
  disks: Disk[] | undefined,
  path: string | null | undefined,
): Disk | undefined {
  const target = (path ?? "").trim();
  if (!target) {
    return undefined;
  }
  const normalized = target.replace(/\/+$/, "") || "/";
  let best: Disk | undefined;
  let bestLength = -1;
  for (const disk of disks ?? []) {
    const mount = (disk.path || "").replace(/\/+$/, "") || "/";
    const matched =
      mount === "/" || normalized === mount || normalized.startsWith(`${mount}/`);
    if (matched && mount.length > bestLength) {
      best = disk;
      bestLength = mount.length;
    }
  }
  return best;
}

const channelLabels: Record<string, string> = {
  tailscale: "Tailscale",
  frp: "FRP",
  direct: "直连",
  local: "本机",
};

export function channelLabel(channel?: string): string {
  if (!channel) {
    return "-";
  }
  return channelLabels[channel] ?? channel;
}
