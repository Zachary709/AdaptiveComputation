import type { NodeDocument } from "../types";
import { shortNodeName } from "../format";
import { useDirectoryLists } from "../hooks/useDirectories";
import { DirectoryEntries } from "./DirectoryEntries";

interface ModelBoardProps {
  nodes: NodeDocument[];
}

export function ModelBoard({ nodes }: ModelBoardProps) {
  const lists = useDirectoryLists(nodes, "model");
  return (
    <section className="panel board">
      <div className="board-title">
        <h2>模型</h2>
        <span className="muted">模型路径与模型</span>
      </div>
      <div className="device-card-list">
        {nodes.map((node) => (
          <article key={node.alias} className="device-card">
            <header>
              <strong>{shortNodeName(node.alias)}</strong>
            </header>
            <dl className="facts">
              <dt>模型路径</dt>
              <dd className="mono">{node.model_path ?? "未注册"}</dd>
              <dt>模型</dt>
              <dd>
                <DirectoryEntries
                  state={lists[node.alias]}
                  registered={Boolean(node.model_path)}
                  empty="没有模型"
                />
              </dd>
            </dl>
          </article>
        ))}
      </div>
    </section>
  );
}
