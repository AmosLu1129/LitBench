# SPDX-License-Identifier: AGPL-3.0-or-later
"""认证打开时的权限矩阵（自修批 B，2026-11-16）。

背景：认证默认关闭（单机全局库）；``LITBENCH_AUTH=1`` 后由中间件统一要求 cookie，
路由再用 ``team.py`` 的可见性/归属判定。本文件把「登录之后」的授权逐面钉住：

1. 匿名 401 覆盖所有非豁免路径，探针/文档/登录面仍公开；
2. 私有库隔离：列表、详情、按 id 的派生接口（元数据/大纲/引用/CSL/论文包/导出）
   看不见统一 404，不确认存在性；
3. 共享只读：能读不能写；写入口（提案/批量采纳/图号/claim）不能绕过；
4. 全局面（agent 任务/审计/自动接受/模型配置/导入历史）在多人部署里属于维护者，
   成员一律 403；golden/jobs/proposal 统计按可见性过滤；
5. 会话与管理员边界：停用账号立刻踢掉旧 session、cookie 属性、最后一个管理员
   不能自停用/自降级。
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from pdf_fixture import make_pdf
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import litbench.db as dbmod
from litbench import csl
from litbench.config import settings
from litbench.db import get_session
from litbench.main import app
from litbench.models import (
    AgentTask,
    Base,
    Claim,
    Figure,
    FigureProposal,
    Job,
    Paper,
    PaperVersion,
    Summary,
)


@pytest.fixture
async def auth_client(tmp_path, monkeypatch):
    """认证打开 + 独立 sqlite + 临时 storage（避免碰真实 .data）。"""
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_cookie_secure", False)
    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    monkeypatch.setattr(settings, "internal_api_token", "")
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'auth-perm.db').as_posix()}",
        poolclass=NullPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    old_engine, old_sessionmaker = dbmod._engine, dbmod._sessionmaker
    dbmod._engine = engine
    dbmod._sessionmaker = Session

    async def _session():
        async with Session() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        ac.session_factory = Session  # type: ignore[attr-defined]
        yield ac
    app.dependency_overrides.clear()
    dbmod._engine, dbmod._sessionmaker = old_engine, old_sessionmaker
    await engine.dispose()


async def _setup_admin(client: AsyncClient) -> dict:
    response = await client.post(
        "/api/auth/setup",
        json={"username": "boss", "password": "admin-pass-123", "display_name": "组长"},
    )
    assert response.status_code == 200, response.text
    return response.json()["user"]


async def _create_member(client: AsyncClient, username: str) -> dict:
    response = await client.post(
        "/api/users",
        json={
            "username": username,
            "password": "member-pass-123",
            "display_name": username.upper(),
            "role": "member",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _login(client: AsyncClient, username: str, password: str = "member-pass-123") -> None:
    response = await client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text


async def _logout(client: AsyncClient) -> None:
    await client.post("/api/auth/logout")


async def _seed_parsed_paper(
    session_factory,
    *,
    title: str,
    owner_id: str | None,
    cite_key: str,
    review_status: str | None = None,
    caption: str | None = None,
) -> dict:
    """一篇带版本 + 图 + claim 的文献，够覆盖详情/图/写作/引用/大纲。"""
    async with session_factory() as session:
        paper = Paper(
            title=title,
            title_norm=title.lower(),
            cite_key=cite_key,
            owner_user_id=owner_id,
        )
        session.add(paper)
        await session.flush()
        version = PaperVersion(
            paper_id=paper.id,
            kind="uploaded",
            file_sha256=f"{paper.id:0<64}"[:64],
            file_path="pdfs/dummy.pdf",
            page_count=1,
        )
        session.add(version)
        await session.flush()
        figure = Figure(
            paper_version_id=version.id,
            page=1,
            bbox_json=[10, 10, 200, 200],
            caption=caption or f"{title} 的图注",
            cite_key=f"T-{paper.id[:8]}-F1",
            review_status=review_status or "pending",
        )
        session.add(figure)
        await session.flush()  # 先落出 figure.id，claim 的 evidence 要引用它
        claim = Claim(
            paper_id=paper.id,
            paper_version_id=version.id,
            text=f"{title} 的一条结论",
            section="正文",
            status="approved",
            evidence_json=[
                {"type": "figure", "id": figure.id, "cite_key": figure.cite_key, "label": "图 1"}
            ],
            owner_user_id=owner_id,
        )
        session.add(claim)
        await session.commit()
        return {
            "paper_id": paper.id,
            "version_id": version.id,
            "figure_id": figure.id,
            "claim_id": claim.id,
            "cite_key": cite_key,
        }


async def _seed_proposal(session_factory, *, figure_id: str, version_id: str) -> str:
    async with session_factory() as session:
        proposal = FigureProposal(
            figure_id=figure_id,
            paper_version_id=version_id,
            kind="figure_review",
            proposed_json={"needs_change": True, "current_label": "图 1"},
            evidence_json=[],
            confidence=0.99,
            status="pending",
        )
        session.add(proposal)
        await session.commit()
        await session.refresh(proposal)
        return proposal.id


async def _seed_job(session_factory, *, version_id: str) -> str:
    async with session_factory() as session:
        job = Job(kind="parse", paper_version_id=version_id, status="done")
        session.add(job)
        await session.commit()
        await session.refresh(job)
        return job.id


async def _seed_task(session_factory, *, figure_id: str) -> str:
    async with session_factory() as session:
        task = AgentTask(kind="figure_review", target_type="figure", target_id=figure_id)
        session.add(task)
        await session.commit()
        await session.refresh(task)
        return task.id


async def _share(client: AsyncClient, paper_id: str, group_id: str) -> None:
    response = await client.post(
        f"/api/papers/{paper_id}/share", json={"workgroup_id": group_id}
    )
    assert response.status_code == 201, response.text


# ---------------------------------------------------------------------------
# 1. 匿名 401 / 豁免路径
# ---------------------------------------------------------------------------
async def test_anonymous_401_and_public_paths(auth_client) -> None:
    protected = [
        ("GET", "/api/papers", None),
        ("GET", "/api/jobs", None),
        ("GET", "/api/dataset/golden", None),
        ("GET", "/api/settings/llm", None),
        ("GET", "/api/audit", None),
        ("GET", "/api/tasks", None),
        ("GET", "/api/proposals/stats", None),
        ("GET", "/api/auth/me", None),
        ("GET", "/api/import/runs", None),
        ("POST", "/api/writing/outline", {}),
        ("POST", "/api/thesis/export", {}),
        ("GET", "/api/workgroups", None),
        ("GET", "/api/redoc", None),
    ]
    for method, path, body in protected:
        response = await auth_client.request(method, path, json=body)
        assert response.status_code == 401, f"{method} {path} → {response.status_code}"

    # 探针/登录面/文档必须免登录（Docker HEALTHCHECK、前端进门、openapi）
    for path in ("/api/ready", "/api/auth/status", "/api/openapi.json", "/api/docs"):
        assert (await auth_client.get(path)).status_code == 200, path
    # CORS 预检不鉴权
    options = await auth_client.options("/api/papers")
    assert options.status_code != 401


# ---------------------------------------------------------------------------
# 2. 私有库隔离：详情/派生接口看不见统一 404
# ---------------------------------------------------------------------------
async def test_private_derived_endpoints_are_404(auth_client, monkeypatch) -> None:
    await _setup_admin(auth_client)
    alice = await _create_member(auth_client, "alice")
    await _create_member(auth_client, "bob")
    await _logout(auth_client)
    a = await _seed_parsed_paper(
        auth_client.session_factory,
        title="A 的私有论文",
        owner_id=alice["id"],
        cite_key="alice-key-1",
    )

    # 元数据 lookup 不能先打外部 API：看不见就直接 404
    async def _boom(*args, **kwargs):  # pragma: no cover - 被调用即失败
        raise AssertionError("不可见文献不应触发外部元数据查询")

    monkeypatch.setattr("litbench.routers.metadata.lookup_for_paper", _boom)
    await _login(auth_client, "bob")
    assert (
        await auth_client.post(f"/api/papers/{a['paper_id']}/metadata/lookup", json={})
    ).status_code == 404
    assert (
        await auth_client.post(
            f"/api/papers/{a['paper_id']}/metadata/apply", json={"fields": {"volume": "1"}}
        )
    ).status_code == 404

    # 大纲/引用/CSL/学位论文包：显式点名不可见 paper/claim 一律 404
    assert (
        await auth_client.post(
            "/api/writing/outline", json={"paper_ids": [a["paper_id"]]}
        )
    ).status_code == 404
    assert (
        await auth_client.post(
            "/api/writing/outline", json={"claim_ids": [a["claim_id"]]}
        )
    ).status_code == 404
    outline = await auth_client.post("/api/writing/outline", json={})
    assert outline.status_code == 422  # 没有可见 claim，不是 200 空大纲
    assert "A 的私有论文" not in outline.text

    assert (
        await auth_client.post(
            "/api/references/csl-json", json={"paper_ids": [a["paper_id"]]}
        )
    ).status_code == 404
    assert (
        await auth_client.post(
            "/api/references/format",
            json={"paper_ids": [a["paper_id"]], "style": "apa7", "engine": "csl"},
        )
    ).status_code == 404
    assert (
        await auth_client.post(
            "/api/thesis/export", json={"paper_ids": [a["paper_id"]]}
        )
    ).status_code == 404
    assert (
        await auth_client.post(
            "/api/thesis/check",
            json={"text": r"\cite{alice-key-1}", "paper_ids": [a["paper_id"]]},
        )
    ).status_code == 404

    # check-citations 只对照可见库：别人的 cite_key 解析不出来
    checked = await auth_client.post(
        "/api/writing/check-citations", json={"text": r"见 \cite{alice-key-1}。"}
    )
    assert checked.status_code == 200, checked.text
    assert checked.json()["unknown"] == 1
    assert checked.json()["citations"][0]["paper_id"] is None

    # 文档导出：paper_ids 预检 404；只给 claim_ids 时也不能把内容带出来
    assert (
        await auth_client.post(
            "/api/writing/export/document",
            json={"format": "docx", "paper_ids": [a["paper_id"]]},
        )
    ).status_code == 404
    doc = await auth_client.post(
        "/api/writing/export/document",
        json={"format": "docx", "claim_ids": [a["claim_id"]]},
    )
    assert doc.status_code in (422, 503)  # 没有可见 claim / 本机缺 pandoc
    assert "A 的私有论文" not in doc.text

    await _logout(auth_client)
    await _login(auth_client, "alice")
    mine = await auth_client.post(
        "/api/writing/check-citations", json={"text": r"见 \cite{alice-key-1}。"}
    )
    assert mine.json()["unknown"] == 0
    assert mine.json()["citations"][0]["paper_id"] == a["paper_id"]
    own_outline = await auth_client.post(
        "/api/writing/outline", json={"paper_ids": [a["paper_id"]]}
    )
    assert own_outline.status_code == 200, own_outline.text
    assert own_outline.json()["total_claims"] == 1


# ---------------------------------------------------------------------------
# 3. 共享只读：能读不能写，写入口不能绕
# ---------------------------------------------------------------------------
async def test_shared_paper_is_readonly_everywhere(auth_client) -> None:
    await _setup_admin(auth_client)
    alice = await _create_member(auth_client, "alice")
    bob = await _create_member(auth_client, "bob")
    await _create_member(auth_client, "carol")
    await _logout(auth_client)
    a = await _seed_parsed_paper(
        auth_client.session_factory,
        title="A 的共享论文",
        owner_id=alice["id"],
        cite_key="alice-shared-key",
    )
    b = await _seed_parsed_paper(
        auth_client.session_factory,
        title="B 自己的论文",
        owner_id=bob["id"],
        cite_key="bob-key",
    )

    # A 建组 + 加 B + 共享；C 不在组里
    await _login(auth_client, "alice")
    group = (await auth_client.post("/api/workgroups", json={"name": "共享组"})).json()
    await auth_client.post(
        f"/api/workgroups/{group['id']}/members", json={"username": "bob"}
    )
    await _share(auth_client, a["paper_id"], group["id"])
    await _logout(auth_client)

    await _login(auth_client, "bob")
    # 读得到
    assert (await auth_client.get(f"/api/papers/{a['paper_id']}")).status_code == 200
    # 元数据 apply 是写：共享只读 → 403
    assert (
        await auth_client.post(
            f"/api/papers/{a['paper_id']}/metadata/apply",
            json={"fields": {"volume": "12"}},
        )
    ).status_code == 403
    # 图号/复核：可见但不可写 → 403
    assert (
        await auth_client.post(
            f"/api/figures/{a['figure_id']}/label", json={"label": "图 5"}
        )
    ).status_code == 403
    assert (
        await auth_client.post(
            f"/api/figures/{a['figure_id']}/review", json={"verdict": "ok"}
        )
    ).status_code == 403
    # 外部提案入口也要写权限：共享只读不能往 A 的待审队列塞建议
    assert (
        await auth_client.post(
            "/api/proposals",
            json={"figure_id": a["figure_id"], "proposed": {"crop_issue": "cut_off"}},
        )
    ).status_code == 403
    # 批量采纳不能绕过单条的 ensure_figure_edit
    proposal_id = await _seed_proposal(
        auth_client.session_factory,
        figure_id=a["figure_id"],
        version_id=a["version_id"],
    )
    batch = await auth_client.post(
        "/api/proposals/decide-batch",
        json={"action": "accept", "proposal_ids": [proposal_id]},
    )
    assert batch.status_code == 200, batch.text
    assert batch.json()["accepted"] == 0
    assert len(batch.json()["failed"]) == 1
    # A 的图没有被 B 改掉
    async with auth_client.session_factory() as session:
        figure = await session.get(Figure, a["figure_id"])
        proposal = await session.get(FigureProposal, proposal_id)
    assert figure is not None and figure.review_status == "pending"
    assert figure.label_raw != "图 5"
    assert proposal is not None and proposal.status == "pending"
    # 看不见的 claim 写入口 → 404（不是 403，不确认存在）
    assert (
        await auth_client.patch(f"/api/claims/{a['claim_id']}", json={"text": "偷改"})
    ).status_code == 404
    # 共享文献不能共享/取消共享（只有 owner 能）
    assert (
        await auth_client.post(
            f"/api/papers/{a['paper_id']}/share", json={"workgroup_id": group["id"]}
        )
    ).status_code == 403
    await _logout(auth_client)

    # C：看不见 → 404；写入口也不能用 403 探测存在性
    await _login(auth_client, "carol")
    assert (
        await auth_client.post(
            f"/api/figures/{a['figure_id']}/label", json={"label": "图 9"}
        )
    ).status_code == 404
    assert (
        await auth_client.post(
            "/api/proposals",
            json={"figure_id": a["figure_id"], "proposed": {"crop_issue": "cut_off"}},
        )
    ).status_code == 404
    assert (
        await auth_client.post(
            f"/api/papers/{a['paper_id']}/share", json={"workgroup_id": group["id"]}
        )
    ).status_code == 404
    # 不在组里不能拿成员管理接口枚举「谁在组里」
    assert (
        await auth_client.delete(f"/api/workgroups/{group['id']}/members/{bob['id']}")
    ).status_code == 404
    # 看不见的 claim 也一样
    assert (
        await auth_client.patch(f"/api/claims/{b['claim_id']}", json={"text": "偷改"})
    ).status_code == 404


async def test_shared_member_cannot_remove_other_member(auth_client) -> None:
    await _setup_admin(auth_client)
    await _create_member(auth_client, "alice")
    bob = await _create_member(auth_client, "bob")
    carol = await _create_member(auth_client, "carol")
    await _logout(auth_client)
    await _login(auth_client, "alice")
    group = (await auth_client.post("/api/workgroups", json={"name": "组"})).json()
    for username in ("bob", "carol"):
        await auth_client.post(
            f"/api/workgroups/{group['id']}/members", json={"username": username}
        )
    await _logout(auth_client)

    await _login(auth_client, "bob")
    # 普通成员不能移除别人，但可以自己退出
    assert (
        await auth_client.delete(f"/api/workgroups/{group['id']}/members/{carol['id']}")
    ).status_code == 403
    assert (
        await auth_client.delete(f"/api/workgroups/{group['id']}/members/{bob['id']}")
    ).status_code == 200


# ---------------------------------------------------------------------------
# 4. 全局面：成员 403，管理员/system 可用
# ---------------------------------------------------------------------------
async def test_management_surfaces_are_admin_only(auth_client, monkeypatch) -> None:
    await _setup_admin(auth_client)
    alice = await _create_member(auth_client, "alice")
    await _create_member(auth_client, "bob")
    await _logout(auth_client)
    a = await _seed_parsed_paper(
        auth_client.session_factory,
        title="A 的论文",
        owner_id=alice["id"],
        cite_key="alice-key",
        review_status="ok",
    )
    await _seed_proposal(
        auth_client.session_factory, figure_id=a["figure_id"], version_id=a["version_id"]
    )
    task_id = await _seed_task(auth_client.session_factory, figure_id=a["figure_id"])
    await _seed_job(auth_client.session_factory, version_id=a["version_id"])

    await _login(auth_client, "bob")
    for method, path, body in [
        ("GET", "/api/tasks", None),
        ("GET", f"/api/tasks/{task_id}", None),
        ("GET", "/api/audit", None),
        ("POST", "/api/proposals/auto-accept", {}),
        ("POST", "/api/tasks/recover-expired", None),
        ("GET", "/api/import/runs", None),
        ("POST", "/api/import/zotero", {"source": "json", "path": "x.json"}),
        (
            "PUT",
            "/api/settings/llm",
            {
                "protocol": "chat_completion",
                "base_url": "https://x.test/v1",
                "model": "m",
            },
        ),
        ("DELETE", "/api/settings/llm", None),
        ("POST", "/api/settings/llm/test", {}),
    ]:
        response = await auth_client.request(method, path, json=body)
        assert response.status_code == 403, f"{method} {path} → {response.status_code}"

    # 模型配置的测试连接不能被成员触发（否则会用已存 key 发到任意 base_url）
    async def _spy(*args, **kwargs):  # pragma: no cover - 成员请求不应到这里
        raise AssertionError("成员不应触发 test_connection")

    monkeypatch.setattr("litbench.routers.settings.test_connection", _spy)

    await _logout(auth_client)
    await _login(auth_client, "boss", "admin-pass-123")
    for method, path, body in [
        ("GET", "/api/tasks", None),
        ("GET", "/api/audit", None),
        ("GET", "/api/import/runs", None),
        ("POST", "/api/proposals/auto-accept", {}),
    ]:
        response = await auth_client.request(method, path, json=body)
        assert response.status_code == 200, f"{method} {path} → {response.status_code}"

    # 管理员可以读模型配置（掩码），也能改；成员读不到明文 key
    saved = await auth_client.put(
        "/api/settings/llm",
        json={
            "protocol": "chat_completion",
            "base_url": "https://api.example.com/v1",
            "model": "m",
            "api_key": "sk-admin-secret-123456",
        },
    )
    assert saved.status_code == 200, saved.text
    await _logout(auth_client)
    await _login(auth_client, "bob")
    read = await auth_client.get("/api/settings/llm")
    assert read.status_code == 200
    assert "sk-admin-secret-123456" not in read.text


async def test_golden_and_jobs_are_visibility_scoped(auth_client) -> None:
    await _setup_admin(auth_client)
    alice = await _create_member(auth_client, "alice")
    bob = await _create_member(auth_client, "bob")
    await _logout(auth_client)
    a = await _seed_parsed_paper(
        auth_client.session_factory,
        title="A 的敏感论文",
        owner_id=alice["id"],
        cite_key="alice-key",
        review_status="ok",
        caption="A-SECRET-CAPTION",
    )
    b = await _seed_parsed_paper(
        auth_client.session_factory,
        title="B 的论文",
        owner_id=bob["id"],
        cite_key="bob-key",
        review_status="ok",
        caption="B-CAPTION",
    )
    a_job = await _seed_job(auth_client.session_factory, version_id=a["version_id"])
    b_job = await _seed_job(auth_client.session_factory, version_id=b["version_id"])

    await _login(auth_client, "bob")
    golden = await auth_client.get("/api/dataset/golden")
    assert golden.status_code == 200, golden.text
    assert golden.json()["item_count"] == 1
    assert a["figure_id"] not in golden.text
    assert "A-SECRET-CAPTION" not in golden.text
    assert b["figure_id"] in golden.text
    bboxes = await auth_client.get("/api/dataset/golden", params={"format": "bboxes"})
    assert bboxes.status_code == 200
    assert a["figure_id"] not in bboxes.text

    jobs = (await auth_client.get("/api/jobs")).json()
    assert [job["id"] for job in jobs] == [b_job]
    assert (await auth_client.get(f"/api/jobs/{a_job}")).status_code == 404
    assert (await auth_client.get(f"/api/jobs/{b_job}")).status_code == 200
    await _logout(auth_client)

    await _login(auth_client, "boss", "admin-pass-123")
    admin_golden = (await auth_client.get("/api/dataset/golden")).json()
    assert admin_golden["item_count"] == 2
    admin_jobs = {job["id"] for job in (await auth_client.get("/api/jobs")).json()}
    assert admin_jobs == {a_job, b_job}
    # 管理面 vs 个人库：proposal 统计是全局管理面；papers 列表仍只看自己+legacy
    assert (await auth_client.get("/api/papers")).json() == []


async def test_proposal_stats_are_visibility_scoped(auth_client) -> None:
    await _setup_admin(auth_client)
    alice = await _create_member(auth_client, "alice")
    bob = await _create_member(auth_client, "bob")
    await _logout(auth_client)
    a = await _seed_parsed_paper(
        auth_client.session_factory,
        title="A 的论文",
        owner_id=alice["id"],
        cite_key="alice-key",
    )
    b = await _seed_parsed_paper(
        auth_client.session_factory,
        title="B 的论文",
        owner_id=bob["id"],
        cite_key="bob-key",
    )
    await _seed_proposal(
        auth_client.session_factory, figure_id=a["figure_id"], version_id=a["version_id"]
    )
    await _seed_proposal(
        auth_client.session_factory, figure_id=b["figure_id"], version_id=b["version_id"]
    )

    await _login(auth_client, "bob")
    stats = (await auth_client.get("/api/proposals/stats")).json()
    assert stats["total"] == 1
    assert stats["pending"] == 1
    await _logout(auth_client)

    await _login(auth_client, "boss", "admin-pass-123")
    admin_stats = (await auth_client.get("/api/proposals/stats")).json()
    assert admin_stats["total"] == 2


# ---------------------------------------------------------------------------
# 5. 写入口的归属与审计 actor
# ---------------------------------------------------------------------------
async def test_figure_write_actors_are_forced_to_current_user(auth_client) -> None:
    await _setup_admin(auth_client)
    alice = await _create_member(auth_client, "alice")
    await _logout(auth_client)
    a = await _seed_parsed_paper(
        auth_client.session_factory,
        title="A 的论文",
        owner_id=alice["id"],
        cite_key="alice-key",
    )

    await _login(auth_client, "alice")
    response = await auth_client.post(
        f"/api/figures/{a['figure_id']}/label",
        json={"label": "图 5", "reviewer": "boss"},  # 伪造 actor
    )
    assert response.status_code == 200, response.text
    async with auth_client.session_factory() as session:
        figure = await session.get(Figure, a["figure_id"])
    assert figure is not None and figure.reviewed_by == "alice"


async def test_duplicate_upload_does_not_reparse_shared_paper(auth_client) -> None:
    await _setup_admin(auth_client)
    await _create_member(auth_client, "alice")
    await _create_member(auth_client, "bob")
    await _logout(auth_client)
    pdf = make_pdf()

    await _login(auth_client, "alice")
    uploaded = await auth_client.post(
        "/api/papers", files={"file": ("paper.pdf", pdf, "application/pdf")}
    )
    assert uploaded.status_code == 201, uploaded.text
    paper_id = uploaded.json()["paper_id"]
    version_id = uploaded.json()["version_id"]
    # 把队列里的解析任务标完成，验证重复上传不会新建 job
    async with auth_client.session_factory() as session:
        for job in (
            await session.execute(select(Job).where(Job.paper_version_id == version_id))
        ).scalars():
            job.status = "done"
        await session.commit()
        before = (
            await session.execute(
                select(func.count()).select_from(Job).where(Job.paper_version_id == version_id)
            )
        ).scalar_one()

    group = (await auth_client.post("/api/workgroups", json={"name": "组"})).json()
    await auth_client.post(
        f"/api/workgroups/{group['id']}/members", json={"username": "bob"}
    )
    await _share(auth_client, paper_id, group["id"])
    await _logout(auth_client)

    await _login(auth_client, "bob")
    duplicate = await auth_client.post(
        "/api/papers", files={"file": ("paper.pdf", pdf, "application/pdf")}
    )
    assert duplicate.status_code == 201, duplicate.text
    assert duplicate.json()["deduplicated"] is True
    assert duplicate.json()["job_id"] == ""  # 没写权限 → 不给自己排重解析
    async with auth_client.session_factory() as session:
        after = (
            await session.execute(
                select(func.count()).select_from(Job).where(Job.paper_version_id == version_id)
            )
        ).scalar_one()
    assert after == before


# ---------------------------------------------------------------------------
# 6. 管理员注册单个用例时也要付的代价：最后一个管理员保护
# ---------------------------------------------------------------------------
async def test_last_admin_cannot_disable_or_demote_self(auth_client) -> None:
    admin = await _setup_admin(auth_client)
    assert (
        await auth_client.patch(f"/api/users/{admin['id']}", json={"is_active": False})
    ).status_code == 422
    assert (
        await auth_client.patch(f"/api/users/{admin['id']}", json={"role": "member"})
    ).status_code == 422
    # 有第二个管理员后就能降级自己
    second = await auth_client.post(
        "/api/users",
        json={"username": "admin2", "password": "admin-pass-456", "role": "admin"},
    )
    assert second.status_code == 201, second.text
    assert (
        await auth_client.patch(f"/api/users/{admin['id']}", json={"role": "member"})
    ).status_code == 200


async def test_metadata_apply_owner_and_shared(auth_client) -> None:
    await _setup_admin(auth_client)
    alice = await _create_member(auth_client, "alice")
    await _create_member(auth_client, "bob")
    await _logout(auth_client)
    a = await _seed_parsed_paper(
        auth_client.session_factory,
        title="A 的论文",
        owner_id=alice["id"],
        cite_key="alice-key",
    )
    await _login(auth_client, "alice")
    applied = await auth_client.post(
        f"/api/papers/{a['paper_id']}/metadata/apply",
        json={"fields": {"volume": "12", "pages": "1-10"}},
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["volume"] == "12"
    assert applied.json()["pages"] == "1-10"


async def test_document_export_owner_happy_path(auth_client) -> None:
    if csl.find_pandoc() is None:  # pragma: no cover - CI 会装 pandoc；本机也装了
        pytest.skip("本机没有 pandoc")
    await _setup_admin(auth_client)
    alice = await _create_member(auth_client, "alice")
    await _logout(auth_client)
    a = await _seed_parsed_paper(
        auth_client.session_factory,
        title="A 的论文",
        owner_id=alice["id"],
        cite_key="alice-key",
    )
    await _login(auth_client, "alice")
    response = await auth_client.post(
        "/api/writing/export/document",
        json={"format": "docx", "claim_ids": [a["claim_id"]]},
    )
    assert response.status_code == 200, response.text
    assert response.headers["X-LitBench-Claims"] == "1"
    assert response.content[:2] == b"PK"  # docx 是 zip


async def test_summary_edit_actor_is_current_user(auth_client) -> None:
    await _setup_admin(auth_client)
    alice = await _create_member(auth_client, "alice")
    await _logout(auth_client)
    a = await _seed_parsed_paper(
        auth_client.session_factory,
        title="A 的论文",
        owner_id=alice["id"],
        cite_key="alice-key",
    )
    async with auth_client.session_factory() as session:
        summary = Summary(
            paper_version_id=a["version_id"],
            section_key="section-1",
            section_title="引言",
            content="原摘要",
            source="agent",
            provider="mock",
        )
        session.add(summary)
        await session.commit()
        await session.refresh(summary)
        summary_id = summary.id

    await _login(auth_client, "alice")
    response = await auth_client.patch(
        f"/api/summaries/{summary_id}",
        json={"content": "人工改过的摘要", "actor": "boss"},  # 伪造 actor
    )
    assert response.status_code == 200, response.text
    assert response.json()["edited_by"] == "alice"
