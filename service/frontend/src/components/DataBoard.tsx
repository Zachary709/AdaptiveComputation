import type { NodeDocument } from "../types";
import { shortNodeName } from "../format";
import { useDirectoryLists } from "../hooks/useDirectories";
import { DirectoryEntries } from "./DirectoryEntries";

interface DataBoardProps {
  nodes: NodeDocument[];
}

export function DataBoard({ nodes }: DataBoardProps) {
  const lists = useDirectoryLists(nodes, "data");
  return (
    <section className="panel board">
      <div className="board-title">
        <h2>数据</h2>
        <span className="muted">数据路径与数据集</span>
      </div>
      <div className="device-card-list">
        {nodes.map((node) => (
          <article key={node.alias} className="device-card">
            <header>
              <strong>{shortNodeName(node.alias)}</strong>
            </header>
            <dl className="facts">
              <dt>数据路径</dt>
              <dd className="mono">{node.data_path ?? "未注册"}</dd>
              <dt>数据集</dt>
              <dd>
                <DirectoryEntries
                  state={lists[node.alias]}
                  registered={Boolean(node.data_path)}
                  empty="没有数据集"
                />
              </dd>
            </dl>
          </article>
        ))}
      </div>
    </section>
  );
}
