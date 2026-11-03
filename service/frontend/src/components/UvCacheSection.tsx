import { useCallback, useEffect, useState } from "react";

import { api } from "../api";
import { formatBytes } from "../format";
import type { UvCacheInfo, UvLock } from "../types";

function packageKey(name: string, version: string): string {
  return `${name.toLowerCase()}==${version}`;
}

function missingPackages(info: UvCacheInfo, lock: UvLock) {
  const cached = new Set(
    [...info.packages, ...(info.installed ?? [])].map((item) =>
      packageKey(item.name, item.version),
    ),
  );
  return lock.packages.filter(
    (item) => !cached.has(packageKey(item.name, item.version)),
  );
}

export function UvCacheSection({ alias }: { alias: string }) {
  const [expanded, setExpanded] = useState(false);
  const [info, setInfo] = useState<UvCacheInfo | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [openLock, setOpenLock] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const payload = await api<UvCacheInfo>(
        `/api/v1/nodes/${encodeURIComponent(alias)}/uv-cache`,
      );
      setInfo(payload);
    } catch (caught) {
      setError(String(caught));
    } finally {
      setLoading(false);
    }
  }, [alias]);

  useEffect(() => {
    void load();
  }, [load]);

  const cachePath = loading ? "加载中…" : error ? "-" : info?.cache_dir || "未记录";

  return (
    <>
      <dt>缓存路径</dt>
      <dd className="mono">{cachePath}</dd>
      <dt>缓存与 lock</dt>
      <dd>
        {error ? (
          <span className="error-text">{error}</span>
        ) : (
          <>
            <button
              type="button"
              className="link-button"
              onClick={() => setExpanded(!expanded)}
            >
              {expanded ? "收起" : "查看 uv 缓存"}
            </button>
            {expanded && info ? (
              <div>
                <div className="python-meta">
                  uv {info.uv_path || "-"} · 缓存{" "}
                  {formatBytes(info.cache_size_bytes)} · 已安装{" "}
                  {info.installed?.length ?? 0} 个包 · Python{" "}
                  {info.python_versions.length > 0
                    ? info.python_versions.join(", ")
                    : "-"}{" "}
                  · 缓存包 {info.packages.length} 个 · lock {info.locks.length} 个
                </div>
                {info.packages.length > 0 ? (
                  <div className="entry-list">
                    {info.packages.map((item) => (
                      <div
                        className="entry-row"
                        key={packageKey(item.name, item.version)}
                      >
                        <span className="entry-name mono">{item.name}</span>
                        <span className="entry-size">{item.version}</span>
                      </div>
                    ))}
                  </div>
                ) : (
                  <span className="muted">缓存里没有包</span>
                )}
                {info.locks.map((lock) => {
                  const missing = missingPackages(info, lock);
                  return (
                    <div className="lock-block" key={lock.path}>
                      <button
                        type="button"
                        className="link-button"
                        onClick={() =>
                          setOpenLock(openLock === lock.path ? "" : lock.path)
                        }
                      >
                        {lock.project}
                      </button>
                      <span className="entry-size">
                        {lock.packages.length} 个包
                        {missing.length > 0
                          ? `，缺 ${missing.length} 个`
                          : "，全部已缓存"}
                      </span>
                      {lock.error ? (
                        <div className="error-text">{lock.error}</div>
                      ) : null}
                      {openLock === lock.path && missing.length > 0 ? (
                        <div className="entry-list">
                          {missing.map((item) => (
                            <div
                              className="entry-row"
                              key={packageKey(item.name, item.version)}
                            >
                              <span className="entry-name mono">{item.name}</span>
                              <span className="entry-size">{item.version}</span>
                            </div>
                          ))}
                        </div>
                      ) : null}
                    </div>
                  );
                })}
              </div>
            ) : null}
          </>
        )}
      </dd>
    </>
  );
}
