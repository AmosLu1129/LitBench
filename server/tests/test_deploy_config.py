# SPDX-License-Identifier: AGPL-3.0-or-later
"""部署链静态测试：只读文件，不需要 Docker / Postgres / alembic。

这类文件平时没人跑测试，坏起来又都很安静：DSN 少个 ``+asyncpg``、worker 继承
了 api 的 HEALTHCHECK、迁移入口被删掉、宿主 node_modules 盖掉容器依赖……
这里把「部署链路必须成立」的硬约束固定成断言，改 compose / Dockerfile 时至少
会先红一次。

不引入 PyYAML（CI 只装 requirements.txt）：compose 用缩进块 + 正则做够用的解析。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_DIR = REPO_ROOT / "server"

COMPOSE = (REPO_ROOT / "compose.yaml").read_text(encoding="utf-8")
DOCKERFILE = (SERVER_DIR / "Dockerfile").read_text(encoding="utf-8")
ENTRYPOINT = (SERVER_DIR / "docker-entrypoint.sh").read_text(encoding="utf-8")
DOCKERIGNORE = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")
ENV_EXAMPLE = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
REQUIREMENTS = (SERVER_DIR / "requirements.txt").read_text(encoding="utf-8")
REQUIREMENTS_DOCLING = (SERVER_DIR / "requirements-docling.txt").read_text(encoding="utf-8")

# compose 里必须显式注入容器的配置：pydantic 字段名 -> 环境变量名。
# 默认值另由 config.py 的类默认值核对（见 test_compose_env_defaults_match_config）。
INJECTED_SETTINGS = {
    "llm_provider": "LLM_PROVIDER",
    "llm_base_url": "LLM_BASE_URL",
    "llm_api_key": "LLM_API_KEY",
    "llm_model": "LLM_MODEL",
    "llm_timeout_s": "LLM_TIMEOUT_S",
    "llm_max_tokens": "LLM_MAX_TOKENS",
    "ocr_backend": "OCR_BACKEND",
    "ocr_cross_enabled": "OCR_CROSS_ENABLED",
    "ocr_cross_dpi": "OCR_CROSS_DPI",
    "escalate_backend": "ESCALATE_BACKEND",
    "escalate_min_missing": "ESCALATE_MIN_MISSING",
    "escalate_min_caption_chars": "ESCALATE_MIN_CAPTION_CHARS",
    "render_figure_images": "RENDER_FIGURE_IMAGES",
    "filter_qr_figures": "FILTER_QR_FIGURES",
    "max_upload_mb": "MAX_UPLOAD_MB",
    "cors_origins": "CORS_ORIGINS",
    "summary_auto_accept_confidence": "SUMMARY_AUTO_ACCEPT_CONFIDENCE",
}


def _section(text: str, header: str) -> str:
    """取 ``header:`` 起、到下一个同级或更浅缩进行之前的块正文。"""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not re.match(rf"^(\s*){re.escape(header)}\s*:", line):
            continue
        indent = len(line) - len(line.lstrip())
        body: list[str] = []
        for following in lines[index + 1 :]:
            if following.strip() and len(following) - len(following.lstrip()) <= indent:
                break
            body.append(following)
        return "\n".join(body)
    raise AssertionError(f"compose.yaml 里找不到 {header}: 块")


def _compose_service(name: str) -> str:
    return _section(COMPOSE, name)


def _compose_env_defaults() -> dict[str, str]:
    """把 x-app-env 里 ``KEY: ${KEY:-default}`` 的 default 抽出来。"""
    defaults: dict[str, str] = {}
    for line in _section(COMPOSE, "x-app-env").splitlines():
        match = re.match(r"^\s+([A-Z0-9_]+):\s*(.+?)\s*$", line)
        if not match:
            continue
        key, raw = match.group(1), match.group(2)
        interpolation = re.fullmatch(r"\$\{([A-Z0-9_]+):-(.*)\}", raw)
        if interpolation is None:
            continue
        assert interpolation.group(1) == key, f"{key} 的插值变量名写错了：{raw}"
        defaults[key] = interpolation.group(2)
    return defaults


def _config_defaults() -> dict[str, object]:
    """用 ast 读 Settings 的类默认值，避免 import pydantic-settings / 受 .env 影响。"""
    tree = ast.parse((SERVER_DIR / "litbench" / "config.py").read_text(encoding="utf-8"))
    defaults: dict[str, object] = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != "Settings":
            continue
        for statement in node.body:
            if not isinstance(statement, ast.AnnAssign):
                continue
            if not isinstance(statement.target, ast.Name) or statement.value is None:
                continue
            try:
                defaults[statement.target.id] = ast.literal_eval(statement.value)
            except ValueError:  # Path(...) 之类的构造不参与本测试
                continue
    return defaults


def _env_text(default: object) -> str:
    if default is None:
        return ""
    if isinstance(default, bool):
        return "true" if default else "false"
    return str(default)


def _dockerignore_patterns() -> list[str]:
    return [
        line.strip()
        for line in DOCKERIGNORE.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_compose_uses_asyncpg_dsn() -> None:
    assert "DATABASE_URL: postgresql+asyncpg://" in COMPOSE, "compose 的 DSN 必须带 +asyncpg 驱动"
    assert "DATABASE_URL: postgresql://" not in COMPOSE, "裸 postgresql:// 会缺 asyncpg 驱动名"
    # 镜像里必须真的有这个驱动，否则 DSN 写对也起不来
    assert "asyncpg" in REQUIREMENTS


def test_api_runs_migrations_worker_does_not() -> None:
    api = _compose_service("api")
    worker = _compose_service("worker")
    assert re.search(r'RUN_MIGRATIONS:\s*"1"', api), "api 必须 RUN_MIGRATIONS=1（启动前跑迁移）"
    assert re.search(r'RUN_MIGRATIONS:\s*"0"', worker), "worker 不能自己跑迁移"
    assert "ENTRYPOINT" in DOCKERFILE and "docker-entrypoint.sh" in DOCKERFILE
    assert "chmod +x /app/docker-entrypoint.sh" in DOCKERFILE, "镜像里要给 entrypoint 可执行位"


def test_worker_waits_for_healthy_api() -> None:
    worker = _compose_service("worker")
    assert re.search(
        r"^\s+api:\s*\n\s+condition:\s*service_healthy", worker, re.MULTILINE
    ), "worker 必须 depends_on api healthy（避免并发迁移）"
    assert re.search(r"healthcheck:\s*\n\s+disable:\s*true", worker), (
        "worker 不监听 8000，必须关掉镜像继承的 HEALTHCHECK，否则永远 unhealthy"
    )


def test_web_waits_for_healthy_api() -> None:
    web = _compose_service("web")
    assert re.search(r"depends_on:\s*\n\s+api:\s*\n\s+condition:\s*service_healthy", web), (
        "web 要等 api healthy"
    )


def test_api_keeps_the_image_healthcheck() -> None:
    # worker/web 的 service_healthy 条件依赖 api 的 HEALTHCHECK；api 自己不能 disable。
    api = _compose_service("api")
    assert "disable: true" not in api, "api 不能关掉 HEALTHCHECK，否则 worker/web 永远等不到 healthy"
    assert not re.search(r"^\s+healthcheck:\s*$", api, re.MULTILINE), (
        "api 沿用镜像层 HEALTHCHECK 即可，不要在这里另写一份"
    )


def test_healthcheck_uses_ready_probe() -> None:
    assert "http://127.0.0.1:8000/api/ready" in DOCKERFILE, "HEALTHCHECK 必须打 /api/ready（查库）"
    assert "http://127.0.0.1:8000/health" not in DOCKERFILE, "/health 不查库，不能当健康判定"


def test_entrypoint_is_posix_sh_and_runs_migrations() -> None:
    first_line = ENTRYPOINT.splitlines()[0] if ENTRYPOINT.splitlines() else ""
    assert first_line == "#!/bin/sh", "entrypoint 必须是 POSIX sh（镜像里没有 bash 保证）"
    assert "set -e" in ENTRYPOINT
    assert "RUN_MIGRATIONS" in ENTRYPOINT
    assert "alembic upgrade head" in ENTRYPOINT
    assert 'exec "$@"' in ENTRYPOINT, "entrypoint 最后必须 exec 主进程（别留 shell 当 PID 1）"
    assert "\r" not in ENTRYPOINT, "entrypoint 必须是 LF；CRLF 会让 /bin/sh 报错"


def test_compose_injects_env_into_both_app_containers() -> None:
    for service in ("api", "worker"):
        block = _compose_service(service)
        assert "<<: *app-env" in block, f"{service} 必须继承 x-app-env（否则变量进不了容器）"


def test_compose_env_defaults_match_config() -> None:
    compose_env = _compose_env_defaults()
    config_defaults = _config_defaults()
    for field, variable in INJECTED_SETTINGS.items():
        assert field in config_defaults, f"config.py 的 Settings 里找不到 {field}"
        assert variable in compose_env, f"compose 的 x-app-env 没有注入 {variable}"
        expected = _env_text(config_defaults[field])
        assert compose_env[variable] == expected, (
            f"{variable} 的 compose 默认值 {compose_env[variable]!r} 与 config.py 默认值 "
            f"{expected!r} 不一致"
        )


def test_env_example_documents_injected_vars() -> None:
    for variable in INJECTED_SETTINGS.values():
        assert re.search(rf"^{variable}=", ENV_EXAMPLE, re.MULTILINE), (
            f".env.example 缺少 {variable} 的示例"
        )
    assert "POSTGRES_PASSWORD=" in ENV_EXAMPLE
    # 由 compose 直接注入的变量：不要求用户填，但要有说明
    assert "DATABASE_URL" in ENV_EXAMPLE and "STORAGE_DIR" in ENV_EXAMPLE


def test_dockerignore_excludes_heavy_dirs_but_keeps_build_inputs() -> None:
    patterns = _dockerignore_patterns()
    normalized = {pattern.rstrip("/") for pattern in patterns}
    normalized |= {pattern.removeprefix("**/").rstrip("/") for pattern in patterns}
    for required in (
        ".git",
        ".venv",
        ".data",
        "node_modules",
        "dist",
        "__pycache__",
        ".pytest_cache",
        "docs/assets",
    ):
        assert required in normalized, f".dockerignore 没有排除 {required}"
    assert any(pattern.endswith(".db") for pattern in patterns), ".dockerignore 没有排除 *.db"
    assert any(pattern.endswith(".env") for pattern in patterns), ".dockerignore 没有排除 .env"
    for build_input in ("server", "web", "schemas", "mcp"):
        assert build_input not in normalized, f".dockerignore 不能排除构建需要的 {build_input}/"
        assert not any(pattern.startswith(f"{build_input}/") for pattern in patterns)


def test_docling_is_not_a_default_dependency() -> None:
    assert "docling" not in REQUIREMENTS, "docling 不能进默认依赖（镜像 +2GB）"
    assert "docling" in REQUIREMENTS_DOCLING, "docling 依赖应该只在 requirements-docling.txt"
