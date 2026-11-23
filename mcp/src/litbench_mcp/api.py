# SPDX-License-Identifier: MIT
"""LitBench REST API 的只读客户端。

许可边界：本模块**不 import** 主仓 ``litbench`` 任何代码，只按 HTTP 契约取 JSON；
返回的也是原始 ``dict``/``list``（不复制服务端的 ORM/schema 类型）。

错误分三类，工具层据此映射为 ``not_found`` / ``upstream_error``：

* 404 → :class:`APIError`（``status_code == 404``），工具层转 ``not_found``；
* 其他 4xx/5xx / 连不上 / 坏 JSON → ``upstream_error``；
* 上游返回 200 但形状不对 → 工具层当上游错误处理（见 ``tools.py``）。
"""

from __future__ import annotations

import json
from typing import Any, Self

import httpx

_MAX_BODY_CHARS = 400


class APIError(RuntimeError):
    """调用 LitBench API 失败（可读错误）。"""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code

    @property
    def error_code(self) -> str:
        """工具层错误码：资源不存在 → not_found；写冲突/参数错单独可辨。"""
        if self.status_code == 404:
            return "not_found"
        if self.status_code == 409:
            return "conflict"
        if self.status_code == 422:
            return "invalid_request"
        return "upstream_error"


def _clean_params(params: dict[str, Any] | None) -> dict[str, Any] | None:
    if not params:
        return None
    return {key: value for key, value in params.items() if value is not None}


def _body_snippet(response: httpx.Response) -> str:
    text = (response.text or "").strip()
    if not text:
        return "（空响应体）"
    try:
        payload = json.loads(text)
    except ValueError:
        return text[:_MAX_BODY_CHARS]
    if isinstance(payload, dict) and "detail" in payload:
        detail = payload["detail"]
        if isinstance(detail, str):
            return detail[:_MAX_BODY_CHARS]
        return json.dumps(detail, ensure_ascii=False)[:_MAX_BODY_CHARS]
    return text[:_MAX_BODY_CHARS]


class LitBenchAPI:
    """只读 HTTP 客户端。

    测试注入 ``transport=httpx.MockTransport(handler)`` 即可完全脱离真实 API，
    不依赖 8000 端口与真实数据。
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 20.0,
        api_token: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        headers = {"Accept": "application/json"}
        if api_token:
            # 认证打开时，API 把内部服务令牌当作 system 主体；空 = 单机模式不带。
            headers["X-LitBench-Internal-Token"] = api_token
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout_s,
            transport=transport,
            headers=headers,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # HTTP 基元
    # ------------------------------------------------------------------
    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            response = await self._client.get(path, params=_clean_params(params))
        except httpx.HTTPError as exc:
            raise APIError(f"连不上 LitBench API（{self.base_url}{path}）：{exc}") from exc

        return self._decode(response, f"GET {path}")

    async def _post(self, path: str, payload: dict[str, Any] | None = None) -> Any:
        try:
            response = await self._client.post(path, json=payload or {})
        except httpx.HTTPError as exc:
            raise APIError(f"连不上 LitBench API（{self.base_url}{path}）：{exc}") from exc

        return self._decode(response, f"POST {path}")

    @staticmethod
    def _decode(response: httpx.Response, what: str) -> Any:
        if response.status_code == 404:
            raise APIError(
                f"LitBench API 找不到资源（{what}）：{_body_snippet(response)}",
                status_code=404,
            )
        if response.status_code >= 400:
            raise APIError(
                f"LitBench API 返回错误（HTTP {response.status_code} {what}）："
                f"{_body_snippet(response)}",
                status_code=response.status_code,
            )
        try:
            return response.json()
        except ValueError as exc:
            raise APIError(
                f"LitBench API 返回的不是 JSON（{what}）：{_body_snippet(response)}"
            ) from exc

    @staticmethod
    def _as_list(payload: Any, path: str) -> list[dict[str, Any]]:
        if not isinstance(payload, list):
            raise APIError(f"LitBench API 响应形状不对（GET {path} 期望数组，实际 {type(payload).__name__}）")
        return [item for item in payload if isinstance(item, dict)]

    @staticmethod
    def _as_dict(payload: Any, path: str) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise APIError(f"LitBench API 响应形状不对（GET {path} 期望对象，实际 {type(payload).__name__}）")
        return payload

    # ------------------------------------------------------------------
    # 文献
    # ------------------------------------------------------------------
    async def list_papers(
        self,
        *,
        q: str | None = None,
        collection_id: str | None = None,
        favorite: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        payload = await self._get(
            "/api/papers",
            {
                "q": q,
                "collection_id": collection_id,
                "favorite": favorite,
                "limit": limit,
                "offset": offset,
            },
        )
        return self._as_list(payload, "/api/papers")

    async def get_paper(self, paper_id: str) -> dict[str, Any]:
        return self._as_dict(await self._get(f"/api/papers/{paper_id}"), "/api/papers/{id}")

    async def get_paper_text(self, paper_id: str) -> dict[str, Any]:
        return self._as_dict(
            await self._get(f"/api/papers/{paper_id}/text"), "/api/papers/{id}/text"
        )

    async def list_paper_figures(self, paper_id: str) -> list[dict[str, Any]]:
        payload = await self._get(f"/api/papers/{paper_id}/figures")
        return self._as_list(payload, "/api/papers/{id}/figures")

    async def list_paper_tables(self, paper_id: str) -> list[dict[str, Any]]:
        payload = await self._get(f"/api/papers/{paper_id}/tables")
        return self._as_list(payload, "/api/papers/{id}/tables")

    # ------------------------------------------------------------------
    # 图 / 表
    # ------------------------------------------------------------------
    async def search_figures(
        self,
        *,
        q: str | None = None,
        paper: str | None = None,
        label: str | None = None,
        favorite: bool | None = None,
        collection_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        payload = await self._get(
            "/api/figures",
            {
                "q": q,
                "paper": paper,
                "label": label,
                "favorite": favorite,
                "collection_id": collection_id,
                "limit": limit,
                "offset": offset,
            },
        )
        return self._as_dict(payload, "/api/figures")

    async def get_figure(self, figure_id: str) -> dict[str, Any]:
        return self._as_dict(await self._get(f"/api/figures/{figure_id}"), "/api/figures/{id}")

    # ------------------------------------------------------------------
    # 摘要
    # ------------------------------------------------------------------
    async def get_summary(self, summary_id: str) -> dict[str, Any]:
        return self._as_dict(
            await self._get(f"/api/summaries/{summary_id}"), "/api/summaries/{id}"
        )

    async def list_paper_summaries(self, paper_id: str) -> list[dict[str, Any]]:
        payload = await self._get(f"/api/papers/{paper_id}/summaries")
        return self._as_list(payload, "/api/papers/{id}/summaries")

    async def get_paper_summary(self, paper_id: str) -> dict[str, Any]:
        return self._as_dict(
            await self._get(f"/api/papers/{paper_id}/summary"), "/api/papers/{id}/summary"
        )

    # ------------------------------------------------------------------
    # 知识库 / 数据集
    # ------------------------------------------------------------------
    async def list_collections(self) -> list[dict[str, Any]]:
        payload = await self._get("/api/collections")
        return self._as_list(payload, "/api/collections")

    async def get_golden(
        self, *, include_pending: bool = False, bbox_only: bool = False
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "include_pending": "true" if include_pending else "false"
        }
        if bbox_only:
            params["format"] = "bboxes"
        return self._as_dict(
            await self._get("/api/dataset/golden", params),
            "/api/dataset/golden",
        )

    async def review_citation(
        self, *, claim_text: str, paper_id: str, block_ids: list[str]
    ) -> dict[str, Any]:
        """模型复核档（M9.4）：POST 只读端点，交给服务端用已配置的模型判断。"""
        return self._as_dict(
            await self._post(
                "/api/citations/review",
                {
                    "claim_text": claim_text,
                    "paper_id": paper_id,
                    "block_ids": block_ids,
                },
            ),
            "/api/citations/review",
        )

    # ------------------------------------------------------------------
    # M5 写侧：提案 / 任务（HTTP 契约见 docs/09 §7）
    # ------------------------------------------------------------------
    async def create_proposal(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._as_dict(await self._post("/api/proposals", payload), "/api/proposals")

    async def list_proposals(
        self,
        *,
        status: str = "pending",
        paper_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        payload = await self._get(
            "/api/proposals",
            {
                "status": status,
                "paper_id": paper_id,
                "limit": limit,
            },
        )
        return self._as_list(payload, "/api/proposals")

    async def get_proposal(self, proposal_id: str) -> dict[str, Any]:
        return self._as_dict(
            await self._get(f"/api/proposals/{proposal_id}"), "/api/proposals/{id}"
        )

    async def list_tasks(
        self,
        *,
        status: str | None = None,
        kind: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        payload = await self._get(
            "/api/tasks",
            {"status": status, "kind": kind, "limit": limit, "offset": offset},
        )
        return self._as_list(payload, "/api/tasks")

    async def get_task(self, task_id: str) -> dict[str, Any]:
        return self._as_dict(await self._get(f"/api/tasks/{task_id}"), "/api/tasks/{id}")

    async def claim_task(
        self, task_id: str, *, actor: str, lease_seconds: int | None = None
    ) -> dict[str, Any]:
        return self._as_dict(
            await self._post(
                f"/api/tasks/{task_id}/claim",
                {"actor": actor, "lease_seconds": lease_seconds},
            ),
            "/api/tasks/{id}/claim",
        )

    async def heartbeat_task(
        self, task_id: str, *, actor: str, lease_seconds: int | None = None
    ) -> dict[str, Any]:
        return self._as_dict(
            await self._post(
                f"/api/tasks/{task_id}/heartbeat",
                {"actor": actor, "lease_seconds": lease_seconds},
            ),
            "/api/tasks/{id}/heartbeat",
        )

    async def complete_task(
        self,
        task_id: str,
        *,
        actor: str,
        result: dict[str, Any] | None = None,
        cost: float = 0.0,
    ) -> dict[str, Any]:
        return self._as_dict(
            await self._post(
                f"/api/tasks/{task_id}/complete",
                {"actor": actor, "result": result or {}, "cost": cost},
            ),
            "/api/tasks/{id}/complete",
        )

    async def fail_task(
        self,
        task_id: str,
        *,
        actor: str,
        error: str,
        retry: bool = True,
    ) -> dict[str, Any]:
        return self._as_dict(
            await self._post(
                f"/api/tasks/{task_id}/fail",
                {"actor": actor, "error": error, "retry": retry},
            ),
            "/api/tasks/{id}/fail",
        )

    async def retry_task(self, task_id: str) -> dict[str, Any]:
        return self._as_dict(
            await self._post(f"/api/tasks/{task_id}/retry", {}), "/api/tasks/{id}/retry"
        )


__all__ = ["APIError", "LitBenchAPI"]
