from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional


class ServiceError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status: Optional[int] = None,
        detail: Any = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.detail = detail


@dataclass
class ServiceClient:
    server: str
    timeout_s: int = 30

    @classmethod
    def from_environment(
        cls,
        *,
        server: Optional[str] = None,
    ) -> "ServiceClient":
        base = server or os.environ.get("ADAPTIVE_SERVER", "http://127.0.0.1:8765")
        return cls(server=base.rstrip("/"))

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Optional[Mapping[str, Any]] = None,
        timeout_s: Optional[int] = None,
    ) -> Any:
        data = None
        headers: Dict[str, str] = {}
        if body is not None:
            data = json.dumps(dict(body), ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.server + path,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(
                request, timeout=timeout_s or self.timeout_s
            ) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            parsed: Any = None
            try:
                payload = json.loads(detail)
                parsed = payload.get("detail") if isinstance(payload, Mapping) else payload
            except json.JSONDecodeError:
                parsed = None
            raise ServiceError(
                f"{method} {path} failed with {exc.code}: {detail}",
                status=int(exc.code),
                detail=parsed,
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise ServiceError(f"cannot reach {self.server}: {exc}") from exc
        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ServiceError(f"{method} {path} returned invalid JSON: {exc}") from exc

    def snapshot(self) -> Dict[str, Any]:
        return self._request("GET", "/api/v1/snapshot")

    def snapshot_id(self) -> Dict[str, Any]:
        payload = self.snapshot()
        return {
            "snapshot_id": payload.get("snapshot_id"),
            "generated_at": payload.get("generated_at"),
        }

    def report_environment(self, record: Mapping[str, Any]) -> Dict[str, Any]:
        return self._request("POST", "/api/v1/environments", body=record)

    def register_path(
        self, alias: str, kind: str, path: str, reporter: str = ""
    ) -> Dict[str, Any]:
        return self._request(
            "POST",
            f"/api/v1/nodes/{alias}/paths/{kind}",
            body={"path": path, "reporter": reporter},
        )

    def allocations(self) -> Dict[str, Any]:
        return self._request("GET", "/api/v1/allocations")

    def health(self) -> Dict[str, Any]:
        return self._request("GET", "/healthz")

    def nodes(self) -> Dict[str, Any]:
        return self._request("GET", "/api/v1/nodes")

    def node(self, alias: str) -> Dict[str, Any]:
        return self._request("GET", f"/api/v1/nodes/{_quote(alias)}")

    def node_series(self, alias: str, limit: int = 120) -> Dict[str, Any]:
        return self._request(
            "GET", f"/api/v1/nodes/{_quote(alias)}/series?limit={int(limit)}"
        )

    def snapshot_history(self, limit: int = 60) -> Dict[str, Any]:
        return self._request("GET", f"/api/v1/snapshots?limit={int(limit)}")

    def snapshot_by_id(self, snapshot_id: int) -> Dict[str, Any]:
        return self._request("GET", f"/api/v1/snapshot/{int(snapshot_id)}")

    def path(self, alias: str, kind: str) -> Dict[str, Any]:
        return self._request(
            "GET", f"/api/v1/nodes/{_quote(alias)}/paths/{_quote(kind)}"
        )

    def paths(self, alias: str) -> Dict[str, Optional[Dict[str, Any]]]:
        records: Dict[str, Optional[Dict[str, Any]]] = {}
        for kind in ("work", "data", "model"):
            try:
                records[kind] = self.path(alias, kind)
            except ServiceError as exc:
                if "failed with 404" not in str(exc):
                    raise
                records[kind] = None
        return records

    def directories(self, alias: str, kind: str) -> Dict[str, Any]:
        return self._request(
            "GET",
            f"/api/v1/nodes/{_quote(alias)}/directories/{_quote(kind)}",
            timeout_s=150,
        )

    def uv_cache(self, alias: str) -> Dict[str, Any]:
        return self._request(
            "GET", f"/api/v1/nodes/{_quote(alias)}/uv-cache", timeout_s=150
        )

    def nvitop(self, alias: str, retry: bool = False) -> Dict[str, Any]:
        suffix = "?retry=1" if retry else ""
        return self._request(
            "GET", f"/api/v1/nodes/{_quote(alias)}/nvitop{suffix}", timeout_s=60
        )

    def gpu_state(self, alias: str) -> Dict[str, Any]:
        return self._request(
            "GET", f"/api/v1/nodes/{_quote(alias)}/gpu-state", timeout_s=60
        )

    def environments(self) -> Dict[str, Any]:
        return self._request("GET", "/api/v1/environments")

    def environment(self, alias: str) -> Dict[str, Any]:
        return self._request("GET", f"/api/v1/environments/{_quote(alias)}")

    def environment_conflict(self, alias: str) -> Dict[str, Any]:
        return self._request(
            "GET", f"/api/v1/environments/{_quote(alias)}/conflict"
        )

    def audit(self, limit: int = 100) -> Dict[str, Any]:
        return self._request("GET", f"/api/v1/audit?limit={int(limit)}")

    def upsert_allocation(self, record: Mapping[str, Any]) -> Dict[str, Any]:
        return self._request("POST", "/api/v1/allocations", body=record)

    def delete_allocation(self, alias: str, gpu_index: int) -> Dict[str, Any]:
        return self._request(
            "DELETE",
            f"/api/v1/allocations/{_quote(alias)}/{int(gpu_index)}",
        )

    def plans(
        self,
        *,
        status: Optional[str] = None,
        owner: Optional[str] = None,
        manifest_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        query = []
        if status:
            query.append(f"status={urllib.parse.quote(str(status), safe='')}")
        if owner:
            query.append(f"owner={urllib.parse.quote(str(owner), safe='')}")
        if manifest_key:
            query.append(
                f"manifest_key={urllib.parse.quote(str(manifest_key), safe='')}"
            )
        suffix = "?" + "&".join(query) if query else ""
        return self._request("GET", f"/api/v1/plans{suffix}")

    def plan(self, plan_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/api/v1/plans/{_quote(plan_id)}")

    def claim_plan(
        self, plan_id: str, *, target: str, ttl: str = "12h", task_id: Optional[str] = None
    ) -> Dict[str, Any]:
        return self._request(
            "POST",
            f"/api/v1/plans/{_quote(plan_id)}/claim",
            body={"target": target, "ttl": ttl, "task_id": task_id},
        )

    def release_plan(self, plan_id: str) -> Dict[str, Any]:
        return self._request(
            "POST", f"/api/v1/plans/{_quote(plan_id)}/release", body={}
        )

    def pause_plan(self, plan_id: str) -> Dict[str, Any]:
        return self._request("POST", f"/api/v1/plans/{_quote(plan_id)}/pause", body={})

    def resume_plan(self, plan_id: str) -> Dict[str, Any]:
        return self._request("POST", f"/api/v1/plans/{_quote(plan_id)}/resume", body={})

    def delete_plan(self, plan_id: str) -> Dict[str, Any]:
        return self._request("DELETE", f"/api/v1/plans/{_quote(plan_id)}")


def _quote(value: str) -> str:
    return urllib.parse.quote(str(value), safe="")
