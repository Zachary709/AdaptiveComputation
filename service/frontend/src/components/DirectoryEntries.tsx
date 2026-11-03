import { formatBytes } from "../format";
import type { DirectoryState } from "../hooks/useDirectories";

export function DirectoryEntries({
  state,
  registered,
  empty,
}: {
  state?: DirectoryState;
  registered: boolean;
  empty: string;
}) {
  if (!registered) {
    return <span className="muted">未注册</span>;
  }
  if (!state || state.loading) {
    return <span className="muted">加载中…</span>;
  }
  if (state.error) {
    return <span className="error-text">{state.error}</span>;
  }
  if (state.entries.length === 0) {
    return <span className="muted">{empty}</span>;
  }
  return (
    <div className="entry-list">
      {state.entries.map((entry) => (
        <div className="entry-row" key={entry.name}>
          <span className="entry-name mono">{entry.name}</span>
          <span className="entry-size">{formatBytes(entry.size_bytes)}</span>
        </div>
      ))}
    </div>
  );
}
