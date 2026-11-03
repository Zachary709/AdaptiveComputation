import { useEffect, useState } from "react";

import { api } from "../api";
import type { DirectoryEntry, DirectoryList, NodeDocument } from "../types";

export interface DirectoryState {
  loading: boolean;
  entries: DirectoryEntry[];
  error: string;
}

function pathFor(node: NodeDocument, kind: "data" | "model"): string {
  return String(kind === "data" ? node.data_path ?? "" : node.model_path ?? "");
}

export function useDirectoryLists(
  nodes: NodeDocument[],
  kind: "data" | "model",
): Record<string, DirectoryState> {
  const [states, setStates] = useState<Record<string, DirectoryState>>({});
  const signature = nodes
    .map((node) => `${node.alias}:${pathFor(node, kind)}`)
    .join("|");

  useEffect(() => {
    let cancelled = false;
    const targets = nodes.filter((node) => pathFor(node, kind) !== "");
    if (targets.length === 0) {
      setStates({});
      return;
    }
    setStates((current) => {
      const next: Record<string, DirectoryState> = {};
      for (const node of targets) {
        next[node.alias] = current[node.alias] ?? {
          loading: true,
          entries: [],
          error: "",
        };
      }
      return next;
    });
    for (const node of targets) {
      api<DirectoryList>(
        `/api/v1/nodes/${encodeURIComponent(node.alias)}/directories/${kind}`,
      )
        .then((payload) => {
          if (cancelled) {
            return;
          }
          setStates((current) => ({
            ...current,
            [node.alias]: { loading: false, entries: payload.entries, error: "" },
          }));
        })
        .catch((caught) => {
          if (cancelled) {
            return;
          }
          setStates((current) => ({
            ...current,
            [node.alias]: {
              loading: false,
              entries: [],
              error: String(caught),
            },
          }));
        });
    }
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signature, kind]);

  return states;
}
