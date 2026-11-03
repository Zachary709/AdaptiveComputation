import { useCallback, useEffect, useState } from "react";

import { api } from "../api";
import { shortGpuName, shortNodeName } from "../format";
import type { NodeDocument, PlanDevice, PlanRecord } from "../types";

const STATUS_LABEL: Record<string, string> = {
  available: "可用",
  in_use: "使用中",
  paused: "已暂停",
  invalid: "已失效",
};

const STATUS_CLASS: Record<string, string> = {
  available: "good",
  in_use: "warn",
  paused: "warn",
  invalid: "bad",
};

function groupedDevices(plan: PlanRecord) {
  const groups = new Map<string, { indexes: number[]; name: string | null }>();
  for (const device of plan.devices) {
    const group = groups.get(device.alias) ?? { indexes: [], name: null };
    group.indexes.push(device.gpu_index);
    if (!group.name && device.gpu_name) {
      group.name = device.gpu_name;
    }
    groups.set(device.alias, group);
  }
  return [...groups.entries()].map(([alias, group]) => ({
    alias,
    indexes: [...group.indexes].sort((left, right) => left - right),
    name: group.name,
  }));
}

export function PlanBoard({ nodes }: { nodes: NodeDocument[] }) {
  const [plans, setPlans] = useState<PlanRecord[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [formOpen, setFormOpen] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [scope, setScope] = useState("static");
  const [selection, setSelection] = useState<Record<string, Set<number>>>({});

  const load = useCallback(async () => {
    try {
      const payload = await api<{ plans: PlanRecord[] }>("/api/v1/plans");
      setPlans(payload.plans);
      setError("");
    } catch (caught) {
      setError(String(caught));
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 10000);
    return () => window.clearInterval(timer);
  }, [load]);

  function openCreate() {
    setEditingId(null);
    setName("");
    setScope("static");
    setSelection({});
    setError("");
    setFormOpen(true);
  }

  function openEdit(plan: PlanRecord) {
    const next: Record<string, Set<number>> = {};
    for (const device of plan.devices) {
      const current = next[device.alias] ?? new Set<number>();
      current.add(device.gpu_index);
      next[device.alias] = current;
    }
    setEditingId(plan.plan_id);
    setName(plan.name);
    setScope(plan.scope || "static");
    setSelection(next);
    setError("");
    setFormOpen(true);
  }

  function closeForm() {
    setFormOpen(false);
    setEditingId(null);
    setName("");
    setScope("static");
    setSelection({});
  }

  function toggleGpu(alias: string, index: number) {
    setSelection((current) => {
      const next = { ...current };
      const selected = new Set(next[alias] ?? []);
      if (selected.has(index)) {
        selected.delete(index);
      } else {
        selected.add(index);
      }
      next[alias] = selected;
      return next;
    });
  }

  async function act(planId: string, action: string) {
    setBusy(`${planId}:${action}`);
    try {
      if (action === "claim") {
        const target = window.prompt("申请给哪个用户？", "board");
        if (!target) {
          return;
        }
        await api(`/api/v1/plans/${planId}/claim`, {
          method: "POST",
          body: { target, ttl: "12h" },
        });
      } else if (action === "delete") {
        await api(`/api/v1/plans/${planId}`, { method: "DELETE" });
      } else {
        await api(`/api/v1/plans/${planId}/${action}`, { method: "POST" });
      }
      await load();
    } catch (caught) {
      setError(String(caught));
    } finally {
      setBusy("");
    }
  }

  async function submitPlan() {
    const devices: PlanDevice[] = [];
    for (const [alias, indices] of Object.entries(selection)) {
      for (const index of [...indices].sort((left, right) => left - right)) {
        devices.push({ alias, gpu_index: index });
      }
    }
    if (scope === "static" && devices.length === 0) {
      setError("固定设备规划至少需要选择一张显卡");
      return;
    }
    setBusy("save");
    try {
      const body = {
        name: name.trim() || "未命名规划",
        owner: "board",
        scope,
        devices,
      };
      if (editingId) {
        await api(`/api/v1/plans/${editingId}`, { method: "PUT", body });
      } else {
        await api("/api/v1/plans", { method: "POST", body });
      }
      closeForm();
      setError("");
      await load();
    } catch (caught) {
      setError(String(caught));
    } finally {
      setBusy("");
    }
  }

  return (
    <section className="panel board">
      <div className="board-title">
        <h2>规划</h2>
        <span className="muted">后台维护的设备配置</span>
      </div>
      {error ? <div className="error-text">{error}</div> : null}
      <div className="plan-toolbar">
        <button type="button" onClick={openCreate}>
          新建规划
        </button>
        <button type="button" onClick={() => void load()}>
          刷新
        </button>
      </div>
      {formOpen ? (
        <div className="plan-form">
          <label>
            规划名称
            <input
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="例如 单设备（两张卡）"
            />
          </label>
          <label>
            设备范围
            <select value={scope} onChange={(event) => setScope(event.target.value)}>
              <option value="static">固定设备</option>
              <option value="all">全部可用设备</option>
            </select>
          </label>
          {scope === "all" ? (
            <span className="muted">该规划在申请时使用当前所有可达节点的显卡。</span>
          ) : (
            <div className="plan-device-list">
              {nodes.map((node) => {
                const devices = node.hardware?.gpu?.devices ?? [];
                return (
                  <div className="plan-device" key={node.alias}>
                    <strong>{shortNodeName(node.alias)}</strong>
                    <div className="plan-gpu-list">
                      {devices.map((device) => (
                        <label key={device.index}>
                          <input
                            type="checkbox"
                            checked={
                              selection[node.alias]?.has(device.index) ?? false
                            }
                            onChange={() => toggleGpu(node.alias, device.index)}
                          />
                          GPU{device.index} {shortGpuName(device.name)}
                        </label>
                      ))}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
          <div className="plan-actions">
            <button
              type="button"
              disabled={busy !== ""}
              onClick={() => void submitPlan()}
            >
              {editingId ? "保存修改" : "创建规划"}
            </button>
            <button type="button" onClick={closeForm}>
              取消
            </button>
          </div>
        </div>
      ) : null}
      <div className="device-card-list">
        {plans.map((plan) => (
          <article className="device-card plan-card" key={plan.plan_id}>
            <header>
              <strong>{plan.name}</strong>
              <span className={`badge ${STATUS_CLASS[plan.status] ?? ""}`}>
                {STATUS_LABEL[plan.status] ?? plan.status}
              </span>
            </header>
            <dl className="facts">
              <dt>规划 ID</dt>
              <dd className="mono">{plan.plan_id}</dd>
              <dt>设备范围</dt>
              <dd>{plan.scope === "all" ? "全部可用设备" : "固定设备"}</dd>
              <dt>创建者</dt>
              <dd>
                {plan.owner}
                {plan.claimed_by ? ` → ${plan.claimed_by}` : ""}
              </dd>
              <dt>设备</dt>
              <dd>
                {plan.devices.length === 0 ? (
                  "当前没有可用设备"
                ) : (
                  <div className="entry-list">
                    {groupedDevices(plan).map((group) => (
                      <div className="entry-row" key={group.alias}>
                        <span className="entry-name mono">
                          {shortNodeName(group.alias)}:GPU
                          {group.indexes.join(",")}
                        </span>
                        <span className="entry-size">
                          {shortGpuName(group.name)}
                        </span>
                      </div>
                    ))}
                  </div>
                )}
              </dd>
              {plan.expires_at ? (
                <>
                  <dt>到期</dt>
                  <dd className="mono">{plan.expires_at}</dd>
                </>
              ) : null}
            </dl>
            <div className="plan-actions">
              {plan.status === "available" ? (
                <button
                  type="button"
                  disabled={busy !== ""}
                  onClick={() => void act(plan.plan_id, "claim")}
                >
                  申请使用
                </button>
              ) : null}
              {plan.status === "in_use" ? (
                <button
                  type="button"
                  disabled={busy !== ""}
                  onClick={() => void act(plan.plan_id, "release")}
                >
                  释放
                </button>
              ) : null}
              {plan.status === "paused" ? (
                <button
                  type="button"
                  disabled={busy !== ""}
                  onClick={() => void act(plan.plan_id, "resume")}
                >
                  重新启用
                </button>
              ) : null}
              <button
                type="button"
                disabled={busy !== ""}
                onClick={() => openEdit(plan)}
              >
                编辑
              </button>
              {plan.status === "available" || plan.status === "in_use" ? (
                <button
                  type="button"
                  disabled={busy !== ""}
                  onClick={() => void act(plan.plan_id, "pause")}
                >
                  暂停使用
                </button>
              ) : null}
              <button
                type="button"
                disabled={busy !== ""}
                onClick={() => {
                  if (window.confirm(`删除规划 ${plan.name}？`)) {
                    void act(plan.plan_id, "delete");
                  }
                }}
              >
                删除
              </button>
            </div>
          </article>
        ))}
        {plans.length === 0 ? (
          <span className="muted">还没有规划，先新建一个</span>
        ) : null}
      </div>
    </section>
  );
}
