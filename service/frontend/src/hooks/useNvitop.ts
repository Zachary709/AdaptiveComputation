import { useEffect, useState } from "react";

import { api } from "../api";
import type { NodeDocument, NvitopPayload } from "../types";

export interface NvitopState {
  payload: NvitopPayload | null;
  error: string;
}

export function useNvitopMap(nodes: NodeDocument[]): Record<string, NvitopState> {
  const [data, setData] = useState<Record<string, NvitopState>>({});
  const key = nodes.map((node) => node.alias).join("|");

  useEffect(() => {
    let active = true;
    let busy = false;
    const aliases = key ? key.split("|") : [];

    async function load(retry: boolean) {
      if (busy || aliases.length === 0) {
        return;
      }
      busy = true;
      const entries = await Promise.all(
        aliases.map(async (alias) => {
          try {
            const suffix = retry ? "?retry=1" : "";
            const payload = await api<NvitopPayload>(
              `/api/v1/nodes/${encodeURIComponent(alias)}/nvitop${suffix}`,
            );
            return [alias, { payload, error: "" }] as const;
          } catch (caught) {
            return [alias, { payload: null, error: String(caught) }] as const;
          }
        }),
      );
      if (active) {
        setData((current) => {
          const next = { ...current };
          for (const [alias, state] of entries) {
            if (state.error && next[alias]?.payload) {
              next[alias] = { payload: next[alias].payload, error: state.error };
            } else {
              next[alias] = state;
            }
          }
          return next;
        });
      }
      busy = false;
    }

    void load(true);
    const timer = window.setInterval(() => {
      void load(false);
    }, 5000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [key]);

  return data;
}
