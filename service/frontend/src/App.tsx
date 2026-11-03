import { useCallback, useEffect, useState } from "react";

import { api, streamSnapshots } from "./api";
import type { Snapshot } from "./types";
import { DeviceBoard } from "./components/DeviceBoard";
import { DevicePicker } from "./components/DevicePicker";
import { DataBoard } from "./components/DataBoard";
import { EnvironmentBoard } from "./components/EnvironmentBoard";
import { GpuBoard } from "./components/GpuBoard";
import { ModelBoard } from "./components/ModelBoard";
import { PlanBoard } from "./components/PlanBoard";
import { useNvitopMap } from "./hooks/useNvitop";
import { shortNodeName } from "./format";

const SELECTED_KEY = "adaptive.selectedDevices";

function loadSelection(): Set<string> | null {
  const raw = window.localStorage.getItem(SELECTED_KEY);
  if (raw === null) {
    return null;
  }
  try {
    const parsed = JSON.parse(raw) as unknown;
    if (Array.isArray(parsed)) {
      return new Set(parsed.filter((item): item is string => typeof item === "string"));
    }
  } catch {
    return null;
  }
  return null;
}

export default function App() {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [selected, setSelected] = useState<Set<string> | null>(() => loadSelection());
  const [error, setError] = useState("");
  const [eventVersion, setEventVersion] = useState(0);

  const loadSnapshot = useCallback(async () => {
    try {
      const next = await api<Snapshot>("/api/v1/snapshot");
      setSnapshot(next);
      setError("");
    } catch (caught) {
      setError(String(caught));
    }
  }, []);

  useEffect(() => {
    void loadSnapshot();
  }, [loadSnapshot, eventVersion]);

  useEffect(() => {
    const controller = new AbortController();
    streamSnapshots(
      () => setEventVersion((value) => value + 1),
      controller.signal,
    ).catch((caught) => {
      if (!controller.signal.aborted) {
        setError(String(caught));
      }
    });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (!snapshot) {
      return;
    }
    const aliases = new Set(snapshot.nodes.map((node) => node.alias));
    setSelected((current) => {
      if (current === null) {
        return aliases;
      }
      return new Set([...current].filter((alias) => aliases.has(alias)));
    });
  }, [snapshot]);

  useEffect(() => {
    if (selected === null) {
      return;
    }
    window.localStorage.setItem(SELECTED_KEY, JSON.stringify([...selected]));
  }, [selected]);

  function toggle(alias: string) {
    setSelected((current) => {
      const next = new Set(current ?? []);
      if (next.has(alias)) {
        next.delete(alias);
      } else {
        next.add(alias);
      }
      return next;
    });
  }

  const nodes = snapshot?.nodes ?? [];
  const active = selected ?? new Set<string>();
  const orderedNodes = [...nodes].sort((left, right) =>
    shortNodeName(left.alias).localeCompare(shortNodeName(right.alias)),
  );
  const onlineNodes = orderedNodes.filter(
    (node) => node.reachable !== false && !node.stale,
  );
  const visibleNodes = onlineNodes.filter((node) => active.has(node.alias));
  const live = useNvitopMap(visibleNodes);

  return (
    <div className="app">
      {error ? <div className="error-banner">{error}</div> : null}

      <DevicePicker nodes={orderedNodes} selected={active} onToggle={toggle} />
      <GpuBoard nodes={visibleNodes} live={live} />
      <DataBoard nodes={visibleNodes} />
      <ModelBoard nodes={visibleNodes} />
      <EnvironmentBoard nodes={visibleNodes} />
      <DeviceBoard nodes={visibleNodes} live={live} />
      <PlanBoard nodes={onlineNodes} />
    </div>
  );
}
