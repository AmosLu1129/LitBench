# SPDX-License-Identifier: AGPL-3.0-or-later
"""迁移与模型的一致性测试。

手写迁移最大的风险不是写错语法，而是**和模型悄悄漂移**：模型加了列，
迁移没跟上，线上建出来的表就少一列，而且要到第一次写入才炸。

这里不连数据库：用一个记录型的假 ``alembic.op`` **按顺序执行所有迁移**，
把最终的建表状态抓下来，再和 ``Base.metadata`` 逐表逐列逐索引比对。
"""

from __future__ import annotations

import ast
import importlib.util
import sys
import types
from pathlib import Path

import sqlalchemy as sa

from litbench.models import Base

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations" / "versions"
# 迁移文件按文件名排序只用于「按顺序回放结构」；修订链本身靠文件里的
# revision/down_revision 校验（见文件末尾的链测试），不靠文件名。
MIGRATION_FILES = sorted(MIGRATIONS_DIR.glob("[0-9]*.py"))


class _Recorder:
    """记录迁移执行后的最终结构。"""

    def __init__(self) -> None:
        self.columns: dict[str, set[str]] = {}
        self.indexes: set[tuple[str, str]] = set()

    # --- 迁移里会调用到的 op.* ---
    def create_table(self, name: str, *args, **kwargs) -> None:
        # 只统计 Column；PrimaryKeyConstraint / UniqueConstraint 也走位置参数，
        # 但它们有 .name，直接 getattr 会把 "pk_paper" 当成列名。
        self.columns[name] = {arg.name for arg in args if isinstance(arg, sa.Column)}

    def add_column(self, table_name: str, column, **kwargs) -> None:
        self.columns.setdefault(table_name, set()).add(column.name)

    def drop_column(self, table_name: str, column_name: str, **kwargs) -> None:
        self.columns.get(table_name, set()).discard(column_name)

    def create_index(self, name: str, table: str, columns, **kwargs) -> None:
        self.indexes.add((name, table))

    def drop_index(self, name: str, table_name: str | None = None, **kwargs) -> None:
        self.indexes = {idx for idx in self.indexes if idx[0] != name}

    def drop_table(self, name: str, **kwargs) -> None:
        self.columns.pop(name, None)
        self.indexes = {idx for idx in self.indexes if idx[1] != name}

    def create_unique_constraint(self, *args, **kwargs) -> None:  # pragma: no cover
        pass


def _install_fake_alembic(rec: _Recorder) -> None:
    alembic_pkg = types.ModuleType("alembic")
    alembic_pkg.__path__ = []  # type: ignore[attr-defined]
    op_mod = types.ModuleType("alembic.op")
    for method in (
        "create_table",
        "add_column",
        "drop_column",
        "create_index",
        "drop_index",
        "drop_table",
        "create_unique_constraint",
    ):
        setattr(op_mod, method, getattr(rec, method))
    alembic_pkg.op = op_mod  # type: ignore[attr-defined]
    sys.modules["alembic"] = alembic_pkg
    sys.modules["alembic.op"] = op_mod


def _run_all_migrations() -> _Recorder:
    rec = _Recorder()
    _install_fake_alembic(rec)

    assert MIGRATION_FILES, f"没有找到迁移文件：{MIGRATIONS_DIR}"
    for path in MIGRATION_FILES:
        spec = importlib.util.spec_from_file_location(f"migration_{path.stem}", path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.upgrade()
    return rec


def test_tables_match_models() -> None:
    rec = _run_all_migrations()
    model_tables = set(Base.metadata.tables)
    assert set(rec.columns) == model_tables, (
        f"迁移与模型表不一致：仅迁移 {set(rec.columns) - model_tables}，"
        f"仅模型 {model_tables - set(rec.columns)}"
    )


def test_columns_match_models() -> None:
    rec = _run_all_migrations()
    for table_name, table in Base.metadata.tables.items():
        model_cols = {c.name for c in table.columns}
        assert rec.columns[table_name] == model_cols, (
            f"{table_name} 列不一致：仅迁移 {rec.columns[table_name] - model_cols}，"
            f"仅模型 {model_cols - rec.columns[table_name]}"
        )


def test_indexes_match_models() -> None:
    rec = _run_all_migrations()
    model_idx = {
        (idx.name, table.name)
        for table in Base.metadata.tables.values()
        for idx in table.indexes
    }
    assert rec.indexes == model_idx, (
        f"索引不一致：仅迁移 {rec.indexes - model_idx}，仅模型 {model_idx - rec.indexes}"
    )


# ---------------------------------------------------------------------------
# 修订链完整性
#
# 上面的结构比对是按文件名顺序回放的，抓不到「链断」：0003 文件里
# revision = "0003"，而 0004 的 down_revision 曾经写成 "0003_label_quality"，
# 结构测试全绿，但任何 `alembic upgrade head` 都会「找不到父修订」。
# 本机/CI 没装 alembic，所以这里不 import alembic，也不靠文件名排序，而是用
# ast 直接读每个文件顶层的 revision / down_revision 来校验整条链。
# ---------------------------------------------------------------------------


def _read_revision_graph() -> dict[str, str | None]:
    """读迁移文件的修订图：``{revision: down_revision}``。"""
    graph: dict[str, str | None] = {}
    for path in MIGRATION_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        values: dict[str, object] = {}
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in ("revision", "down_revision"):
                    values[target.id] = ast.literal_eval(node.value)

        name = path.name
        assert "revision" in values, f"{name} 缺少 revision"
        revision = values["revision"]
        assert isinstance(revision, str) and revision, f"{name} 的 revision 必须是非空字符串"
        assert "down_revision" in values, f"{name} 缺少 down_revision（根修订要显式写 None）"
        down = values["down_revision"]
        assert down is None or isinstance(down, str), (
            f"{name} 的 down_revision 必须是字符串或 None；branches/merge 的 tuple 不在本链里"
        )
        assert revision not in graph, f"revision 重复：{revision}（{name}）"
        graph[revision] = down
    assert len(graph) == len(MIGRATION_FILES), (
        f"有迁移文件没被解析到（或 revision 重复）：{len(graph)} != {len(MIGRATION_FILES)}"
    )
    return graph


def test_revision_graph_has_one_root_and_all_parents_exist() -> None:
    graph = _read_revision_graph()
    revisions = set(graph)

    dangling = {
        revision: down
        for revision, down in graph.items()
        if down is not None and down not in revisions
    }
    assert not dangling, f"down_revision 指向不存在的 revision：{dangling}"

    roots = sorted(revision for revision, down in graph.items() if down is None)
    assert len(roots) == 1, f"必须有且只有一个根修订（down_revision=None），实际：{roots}"


def test_revision_graph_is_a_single_cycle_free_chain() -> None:
    graph = _read_revision_graph()
    revisions = set(graph)

    dangling = {
        revision: down
        for revision, down in graph.items()
        if down is not None and down not in revisions
    }
    assert not dangling, f"down_revision 指向不存在的 revision：{dangling}"

    parents = {down for down in graph.values() if down is not None}
    heads = sorted(revisions - parents)
    assert len(heads) == 1, f"必须有且只有一个 head（没有任何修订以它为 down_revision），实际：{heads}"

    # 从 head 沿 down_revision 一路走到根：不能重复（环），且要覆盖所有迁移文件。
    visited: list[str] = []
    revision: str | None = heads[0]
    while revision is not None:
        assert revision not in visited, f"迁移链有环：{' -> '.join([*visited, revision])}"
        visited.append(revision)
        revision = graph[revision]

    missing = sorted(revisions - set(visited))
    assert not missing, f"这些迁移不在 head→根的链上（或自成一个环）：{missing}"
