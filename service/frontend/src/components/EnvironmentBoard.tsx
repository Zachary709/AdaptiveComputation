import type { NodeDocument } from "../types";
import { formatTmuxVersion, formatUvVersion, shortNodeName } from "../format";
import { UvCacheSection } from "./UvCacheSection";

interface EnvironmentBoardProps {
  nodes: NodeDocument[];
}

export function EnvironmentBoard({ nodes }: EnvironmentBoardProps) {
  return (
    <section className="panel board">
      <div className="board-title">
        <h2>环境</h2>
        <span className="muted">uv 缓存与 lock</span>
      </div>
      <div className="device-card-list">
        {nodes.map((node) => {
          const uv = node.software?.uv;
          const tmux = node.software?.tmux;
          const models = [
            ...new Set(
              (node.hardware?.gpu?.models ?? []).filter(
                (name) => Boolean(name) && !name.toUpperCase().includes("ERROR"),
              ),
            ),
          ];
          return (
            <article key={node.alias} className="device-card">
              <header>
                <strong>{shortNodeName(node.alias)}</strong>
              </header>
              <dl className="facts">
                <dt>uv 版本</dt>
                <dd className="mono">{uv?.installed ? formatUvVersion(uv.version) || "已安装" : "未安装"}</dd>
                <dt>uv 路径</dt>
                <dd className="mono">{uv?.path || "未记录"}</dd>
                <dt>tmux 版本</dt>
                <dd className="mono">{tmux?.installed ? formatTmuxVersion(tmux.version) || "已安装" : "未安装"}</dd>
                <dt>tmux 路径</dt>
                <dd className="mono">{tmux?.path || "未记录"}</dd>
                <UvCacheSection alias={node.alias} />
                <dt>显卡型号</dt>
                <dd>{models.length > 0 ? models.join(" · ") : "-"}</dd>
              </dl>
            </article>
          );
        })}
      </div>
    </section>
  );
}
