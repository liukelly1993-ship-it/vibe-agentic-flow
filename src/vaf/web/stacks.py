"""Deterministic stack selection for the first local code-generation slice."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StackChoice:
    backend: str
    frontend: str
    database: str
    reason: str
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "backend": self.backend,
            "frontend": self.frontend,
            "database": self.database,
            "reason": self.reason,
            "warnings": list(self.warnings),
        }


def choose_stack(prd_text: str) -> StackChoice:
    text = prd_text.lower()
    frontend = "Vue 3 + Vite" if any(token in text for token in ("vue", "nuxt")) else "React + Vite"
    database = "PostgreSQL" if any(token in text for token in ("postgres", "postgresql", "高并发", "多租户")) else "SQLite"
    warnings: list[str] = []
    if "next.js" in text or "nextjs" in text:
        warnings.append(
            "PRD 明确建议 Next.js 全栈；当前 M0 使用 FastAPI + Vite 本地适配器，先保证隔离生成和可验证运行，尚未生成 Next.js API Routes。"
        )
    if "prisma" in text:
        warnings.append("PRD 提到了 Prisma；当前 M0 只生成 SQLite 边界和本地内存模板，尚未接入 Prisma schema 和迁移。")
    if "openai" in text or "gpt-4o-mini" in text:
        warnings.append("PRD 提到了 OpenAI；当前 M0 使用确定性本地 AI 适配器，不读取密钥也不调用外部模型。")
    if "vercel" in text:
        warnings.append("PRD 提到了 Vercel；当前 M0 只交付本地项目和构建证据，尚未执行云端部署。")
    if any(token in text for token in ("java", "spring boot", "nestjs", "express")):
        warnings.append("PRD 提到了其他后端生态；当前本地闭环采用 FastAPI 适配器，保留清晰的领域边界，后续可替换 Provider。")
    if database == "PostgreSQL":
        warnings.append("当前演示模板默认使用 SQLite 以保证本地零配置运行，数据库端口保持可替换。")
    return StackChoice(
        backend="FastAPI",
        frontend=frontend,
        database=database,
        reason="Python 后端适合快速本地验证，Vite 前端适合独立开发和构建；数据库按需求关键词选择本地默认或生产候选。",
        warnings=tuple(warnings),
    )
