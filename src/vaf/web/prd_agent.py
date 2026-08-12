"""A deterministic PRD-to-local-app agent used by the Web MVP.

The agent deliberately emits a small, inspectable template instead of claiming
that a model invented production behavior. A future model provider can replace
this class while keeping the same AgentPort and Gate contracts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Mapping

from vaf.agents.fake_agent import DraftResult
from vaf.ports.agents import CodeChange, CodeGenerationResult
from vaf.web.stacks import StackChoice


@dataclass(frozen=True)
class PrdContext:
    title: str
    objective: str
    source_text: str
    source_hash: str
    stack: StackChoice
    has_visual_evidence: bool = False


COMMERCE_REQUIREMENTS = (
    "REQ-001",
    "REQ-002",
    "REQ-003",
    "REQ-004",
    "REQ-005",
    "REQ-006",
)
COMMERCE_ACCEPTANCE_IDS = tuple(f"AC-{index:03d}" for index in range(1, 12))
COMMERCE_TEST_IDS = tuple(f"TC-{index:03d}" for index in range(1, 13))


class PrdTemplateAgent:
    """Generate a runnable FastAPI plus Vite application from a PRD context."""

    def __init__(self, context: PrdContext) -> None:
        self.context = context

    def draft_prd(self, change_id: str, title: str, objective: str, version: int = 1) -> DraftResult:
        now = datetime.now(timezone.utc).isoformat()
        excerpt = _source_excerpt(self.context.source_text)
        commerce = _is_commerce_prd(self.context.source_text)
        requirement_ids = _requirement_ids(self.context.source_text)
        requirement_yaml = "[" + ", ".join(requirement_ids) + "]"
        requirements_body = _commerce_requirements_body() if commerce else """## REQ-001：核心业务目标

WHEN 用户提交本 PRD 所描述的业务请求
THE SYSTEM SHALL 提供一个可以在本地启动、验证和追踪的前后端实现"""
        acceptance_body = _commerce_acceptance_body() if commerce else """- AC-001：后端健康接口可以通过自动化测试验证。
- AC-002：前端可以启动并展示 PRD 目标、技术栈和后端状态。"""
        content = f"""---
artifact_id: PRD-{change_id}
artifact_type: prd
change_id: {change_id}
version: {version}
status: waiting_review
requirements: {requirement_yaml}
source_hash: {self.context.source_hash}
created_by: vaf-local-prd-agent
created_at: {now}
prototype_evidence: {str(self.context.has_visual_evidence).lower()}
---

# PRD：{self.context.title}

## 来源与边界

本产物由上传文档生成，原始来源哈希为 `{self.context.source_hash}`。未出现在原始文档中的业务行为不会被自动扩展。

## 问题与目标

{self.context.objective}

{requirements_body}

## 验收条件

{acceptance_body}

## 原始需求证据

```text
{excerpt}
```

## 原型证据

摄取层检测结果：{"已发现原型图片或页面截图" if self.context.has_visual_evidence else "未发现原型图片或页面截图，当前 PRD 不具备进入下一阶段的条件"}。
"""
        return DraftResult(
            artifact_type="prd",
            content=content,
            assumptions=("未明确的业务规则只保留为原始证据，不自动推断为实现行为。",),
            questions=(),
        )

    def draft_artifact(
        self,
        artifact_type: str,
        change_id: str,
        title: str,
        objective: str,
        version: int = 1,
    ) -> DraftResult:
        now = datetime.now(timezone.utc).isoformat()
        commerce = _is_commerce_prd(self.context.source_text)
        requirement_yaml = "[" + ", ".join(_requirement_ids(self.context.source_text)) + "]"
        bodies = {
            "technical-design": f"""## 设计目标

实现 REQ-001 对应的可本地运行前后端，并保持 API、前端和数据库边界清晰。

## 技术栈

- 后端：{self.context.stack.backend}
- 前端：{self.context.stack.frontend}
- 数据库：{self.context.stack.database}

## 影响范围

变更限定在 `backend/`、`frontend/` 和 `tests/`，根目录 README 只记录启动方式。服务通过 `/api` 前缀提供接口，前端通过 Vite 代理访问后端。

## 安全与风险

默认不读取 Secret，不执行动态代码，不启用外部网络；输入和接口响应使用显式 Schema。{self.context.stack.warnings[0] if self.context.stack.warnings else "当前 PRD 没有识别出额外的技术栈风险。"}

## 风险与验证

使用后端 unittest 验证健康接口和需求摘要；前端保留独立 `npm run build` 入口，最终质量门绑定代码范围、Trace 和验证退出码。""",
            "test-cases": _commerce_test_cases_body() if commerce else """## 测试范围

覆盖后端健康检查、需求摘要接口、前端启动契约和失败时的可观察证据。

## TC-001：后端健康检查

前置条件：安装 `backend/requirements.txt`。

步骤：调用 `/api/health`。

预期：返回 `status=ok` 和当前生成应用名称。

## TC-002：前端状态展示

前置条件：启动 FastAPI 和 Vite。

步骤：打开前端首页并等待状态请求完成。

预期：页面展示 PRD 目标、技术栈和后端状态。""",
            "implementation-plan": self._implementation_plan_body(),
        }
        if artifact_type not in bodies:
            raise ValueError(f"unsupported artifact type: {artifact_type}")
        heading = {
            "technical-design": "技术方案",
            "test-cases": "测试用例",
            "implementation-plan": "实施计划",
        }[artifact_type]
        content = f"""---
artifact_id: {artifact_type.upper()}-{change_id}-V{version}
artifact_type: {artifact_type}
change_id: {change_id}
version: {version}
status: waiting_review
requirements: {requirement_yaml}
created_by: vaf-local-prd-agent
created_at: {now}
---

# {heading}：{self.context.title}

{bodies[artifact_type]}
"""
        return DraftResult(artifact_type, content, (), ())

    def generate_code(
        self,
        *,
        change_id: str,
        title: str,
        objective: str,
        implementation: Mapping[str, object],
    ) -> CodeGenerationResult:
        raw_changes = implementation.get("changes")
        if not isinstance(raw_changes, list) or not raw_changes:
            raise ValueError("implementation plan requires a non-empty changes list")
        changes: list[CodeChange] = []
        for item in raw_changes:
            if not isinstance(item, dict):
                raise ValueError("implementation change must be a mapping")
            changes.append(
                CodeChange(
                    task_id=str(item["task_id"]),
                    path=str(item["path"]),
                    content=str(item["content"]),
                    requirement_ids=tuple(str(value) for value in item.get("requirement_ids", [])),
                    acceptance_ids=tuple(str(value) for value in item.get("acceptance_ids", [])),
                    test_ids=tuple(str(value) for value in item.get("test_ids", [])),
                )
            )
        return CodeGenerationResult(
            changes=tuple(changes),
            assumptions=(f"已按 {self.context.stack.backend} + {self.context.stack.frontend} 本地模板生成。",),
            questions=(),
        )

    def implementation_items(self) -> list[dict[str, object]]:
        summary = json.dumps(self.context.objective, ensure_ascii=False)
        requirements = json.dumps([self.context.objective], ensure_ascii=False)
        frontend_is_vue = self.context.stack.frontend.startswith("Vue")
        commerce = _is_commerce_prd(self.context.source_text)
        requirement_ids = _requirement_ids(self.context.source_text)
        acceptance_ids = (
            ("AC-001", "AC-003", "AC-007", "AC-008", "AC-009")
            if commerce
            else ("AC-001", "AC-002")
        )
        test_ids = (
            ("TC-001", "TC-003", "TC-007", "TC-008", "TC-009")
            if commerce
            else ("TC-001", "TC-002")
        )
        items: list[dict[str, object]] = [
            _item("README.md", _readme(self.context, frontend_is_vue), requirement_ids=requirement_ids),
            _item("backend/__init__.py", "", requirement_ids=requirement_ids),
            _item("backend/app/__init__.py", "", requirement_ids=requirement_ids),
            _item("backend/requirements.txt", "fastapi>=0.115\nuvicorn[standard]>=0.30\npydantic>=2.0\n", requirement_ids=requirement_ids),
            _item("backend/app/main.py", _backend(self.context.title, self.context.objective, commerce), requirement_ids=requirement_ids),
            _item("frontend/package.json", _package_json(self.context.title, frontend_is_vue), requirement_ids=requirement_ids),
            _item("frontend/vite.config.js", _vite_config(frontend_is_vue), requirement_ids=requirement_ids),
            _item("frontend/index.html", _index_html(self.context.title), requirement_ids=requirement_ids),
            _item("frontend/src/main.jsx", _frontend_main(frontend_is_vue), requirement_ids=requirement_ids),
            _item(
                "frontend/src/App.vue" if frontend_is_vue else "frontend/src/App.jsx",
                _frontend_app(self.context.title, summary, requirements, frontend_is_vue, commerce),
                requirement_ids=requirement_ids,
            ),
            _item("frontend/src/styles.css", _frontend_css(), requirement_ids=requirement_ids),
            _item("tests/__init__.py", "", requirement_ids=requirement_ids),
            _item(
                "tests/test_backend.py",
                _backend_test(commerce),
                requirement_ids=requirement_ids,
                acceptance_ids=acceptance_ids,
                test_ids=test_ids,
            ),
        ]
        return items

    def _implementation_plan_body(self) -> str:
        paths = ", ".join(str(item["path"]) for item in self.implementation_items())
        if _is_commerce_prd(self.context.source_text):
            return f"""## TASK-001：实现商城 MVP 核心闭环

实现 {", ".join(COMMERCE_REQUIREMENTS)}，覆盖 {", ".join(COMMERCE_ACCEPTANCE_IDS)}。

实施范围：{paths}

测试绑定：{", ".join(COMMERCE_TEST_IDS)}；其中 API 用例必须由后端自动化执行，浏览器 E2E 和性能用例必须保留未验证状态，不能用单元测试结果替代。

完成条件：商城验收和回归用例通过；前端构建通过；后端测试退出码为 0；TraceLink 覆盖 REQ/AC/TC；未实现或未执行的 P0 能力必须阻断交付。"""
        return f"""## TASK-001：生成可本地运行的前后端

实现 REQ-001，覆盖 AC-001 和 AC-002，使用 {self.context.stack.backend} + {self.context.stack.frontend}。

实施范围：{paths}

完成条件：后端测试通过、前端具有启动脚本、所有文件在声明范围内、TraceLink 和质量门通过。"""


def _item(
    path: str,
    content: str,
    *,
    requirement_ids: tuple[str, ...] = ("REQ-001",),
    acceptance_ids: tuple[str, ...] = (),
    test_ids: tuple[str, ...] = (),
) -> dict[str, object]:
    item: dict[str, object] = {
        "task_id": "TASK-001",
        "path": path,
        "content": content,
        "requirement_ids": list(requirement_ids),
    }
    if acceptance_ids:
        item["acceptance_ids"] = list(acceptance_ids)
    if test_ids:
        item["test_ids"] = list(test_ids)
    return item


def _source_excerpt(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines[:80])[:8000]


def _requirement_ids(source_text: str) -> tuple[str, ...]:
    return COMMERCE_REQUIREMENTS if _is_commerce_prd(source_text) else ("REQ-001",)


def _commerce_requirements_body() -> str:
    return """## REQ-001：AI 搜索选品

WHEN 买家输入自然语言购物需求
THE SYSTEM SHALL 返回按意图和标签匹配的商品，并为每个商品提供推荐理由

## REQ-002：商品浏览

WHEN 买家进入商品列表或详情页
THE SYSTEM SHALL 展示图片、名称、价格、库存、规格、配送和加入购物车/立即购买入口

## REQ-003：购物车与默认货到付款

WHEN 买家提交购物车结算
THE SYSTEM SHALL 允许确认地址并直接创建默认 COD 订单，不展示支付方式选择

## REQ-004：订单状态

WHEN 商家更新订单状态
THE SYSTEM SHALL 支持待发货、待收货、已完成和已拒收，并允许买家查看订单状态

## REQ-005：AI 智能客服与转人工

WHEN 买家咨询商品、配送、COD、退换货或订单
THE SYSTEM SHALL 基于商品和 FAQ 知识回答；无法回答时应建议或创建人工留言

## REQ-006：商家后台

WHEN 商家进入后台
THE SYSTEM SHALL 支持商品、订单、FAQ 和人工留言的基础管理与查看
"""


def _commerce_acceptance_body() -> str:
    return """- AC-001（自动化/API）：输入“送女友的生日礼物 500 内”时返回相关商品，且每个商品包含推荐理由。
- AC-002（自动化/API）：携带历史意图“生日礼物”并追加“再便宜点”时，结果按新约束重新匹配。
- AC-003（自动化/API）：输入不存在的品类时返回空结果，并引导使用关键词或联系客服，不得返回无关商品。
- AC-004（浏览器 E2E）：商品列表和详情展示图片、名称、价格、库存、规格、配送说明，并提供加入购物车和立即购买入口。
- AC-005（浏览器 E2E）：购物车支持增加、减少、删除商品并实时计算总价。
- AC-006（浏览器 E2E）：结算按“确认商品 → 填写地址 → 提交订单”完成，不出现在线支付或支付方式选择页面。
- AC-007（自动化/API）：订单生成唯一订单号，订单标记为默认 COD，成功结果展示“送达后现金支付”及应付金额。
- AC-008（自动化/API）：订单状态支持待发货、待收货、已完成、已拒收，非法状态被拒绝。
- AC-009（自动化/API）：客服能够基于商品价格、库存、配送、退换货和 COD FAQ 回答，且无关问题被礼貌引导回商城范围。
- AC-010（自动化/API）：用户主动转人工或连续两次无法回答时创建留言，并携带对话上下文摘要供后台查看。
- AC-011（浏览器 E2E/API）：商家能够完成商品 CRUD、订单查看/状态修改、FAQ 管理和人工留言查看。"""


def _commerce_test_cases_body() -> str:
    return """## 测试范围

覆盖 AI 搜索、商品浏览、购物车、默认货到付款、订单状态、AI 客服、转人工、商家后台和回归约束。标记为“浏览器 E2E”的用例必须在真实前端页面执行，当前 M0 未将浏览器断言伪装成后端单测。

## TC-001：自然语言 AI 选品 [API]

追踪：REQ-001 / AC-001

前置：商品种子数据已加载。

步骤：POST `/api/ai-search`，输入“送女友的生日礼物 500 内”。

预期：返回相关商品；每个商品包含名称、价格、库存、推荐理由；价格不超过 500。

## TC-002：多轮细化重新匹配 [API]

追踪：REQ-001 / AC-002

步骤：第一次输入“生日礼物”，第二次携带历史输入并追加“再便宜点”。

预期：第二次结果重新计算，不复用第一次结果；结果满足新的价格或标签约束。

## TC-003：无结果保护 [API / 回归]

追踪：REQ-001 / AC-003

步骤：输入“不存在的商品品类”。

预期：返回空列表和关键词/客服引导；不得用全量商品填充结果，不得返回无关商品。

## TC-004：商品列表与详情 [浏览器 E2E]

追踪：REQ-002 / AC-004

步骤：分别从关键词入口和 AI 结果入口进入商品列表，再打开商品详情。

预期：列表和详情展示图片、名称、价格、库存、规格、富文本描述、配送说明；加入购物车和立即购买可用。

## TC-005：购物车数量和总价 [浏览器 E2E]

追踪：REQ-003 / AC-005

步骤：加入两个商品，增加/减少数量，再删除一个商品。

预期：数量、行金额和总价实时正确，刷新页面后的行为符合产品约定。

## TC-006：三步 COD 下单无支付步骤 [浏览器 E2E / 回归]

追踪：REQ-003 / AC-006

步骤：确认商品清单，填写姓名、电话、地址，提交订单。

预期：流程不出现支付方式选择、在线支付或优惠券步骤；提交后进入成功页。

## TC-007：COD 订单创建和金额提示 [API]

追踪：REQ-003 / AC-007

步骤：POST `/api/orders` 创建订单。

预期：返回唯一订单号、订单总额、`cash_on_delivery=true`、初始状态和收货信息；前端提示送达后现金支付金额。

## TC-008：订单状态流转 [API / 回归]

追踪：REQ-004 / AC-008

步骤：依次更新为待发货、待收货、已完成，再单独验证已拒收；提交未定义状态。

预期：合法状态更新成功，非法状态返回 400；买家查询到最新状态。

## TC-009：客服知识范围和商品上下文 [API]

追踪：REQ-005 / AC-009

步骤：携带商品 ID，分别询问价格、库存、配送、COD、退换货；再询问与商城无关的问题。

预期：回答引用商品/FAQ 事实；无关问题礼貌拒答并引导回商品咨询。

## TC-010：转人工和上下文摘要 [API / 回归]

追踪：REQ-005 / AC-010

步骤：主动输入“转人工”，或连续两次输入知识库无法回答的问题。

预期：创建人工留言，状态可供后台查看，并携带最近对话上下文摘要；AI 不伪造已转接成功。

## TC-011：商家后台基础管理 [API + 浏览器 E2E]

追踪：REQ-006 / AC-011

步骤：创建、编辑、上下架商品；查询订单并修改状态；新增、编辑 FAQ；查看人工留言和对话日志。

预期：每项操作结果可查询、错误输入有明确响应，买家端能看到商品和 FAQ 的最新版本。

## TC-012：MVP 回归和非功能检查 [回归]

追踪：REQ-001~006

步骤：执行全部 TC-001~TC-011；记录 AI 首响应 P95、无关问题拦截、COD 完单链路和前端构建结果。

预期：P0 功能无回归；首响应目标不超过 3 秒（P95）；前端构建、后端自动化测试和 Trace 证据均通过。未实际执行的浏览器或性能检查必须标记为未验证，不得计入通过分数。

## 回归用例清单

| 用例 | 防止的回归 |
|---|---|
| TC-003 | 无结果时错误返回全量或无关商品 |
| TC-006 | 误加入支付方式选择或在线支付步骤 |
| TC-007 | COD 标记、订单号或金额提示丢失 |
| TC-008 | 订单状态枚举漂移或非法状态写入 |
| TC-010 | AI 无法回答时虚假声称已转人工 |
| TC-011 | 后台修改未同步到买家端 |
"""


def _readme(context: PrdContext, frontend_is_vue: bool) -> str:
    frontend_command = "npm run dev" if frontend_is_vue else "npm run dev"
    return f"""# {context.title}

这是由 VAF 根据 PRD 自动生成的本地可运行项目。

## 技术栈

- Backend: {context.stack.backend}
- Frontend: {context.stack.frontend}
- Database: {context.stack.database}

## 启动

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt
uvicorn backend.app.main:app --reload --port 8000
cd frontend && npm install && {frontend_command}
```

前端默认运行在 `http://localhost:5173`，后端健康接口为 `http://localhost:8000/api/health`。

PRD 原文哈希：`{context.source_hash}`
"""


def _is_commerce_prd(source_text: str) -> bool:
    normalized = source_text.lower()
    return all(
        signal in normalized
        for signal in ("ai 搜索选品", "货到付款", "ai 智能客服")
    )


def _commerce_backend(title: str, objective: str) -> str:
    title_literal = repr(title)
    objective_literal = repr(objective)
    return '''\
"""Generated commerce API for __TITLE__."""

import hashlib
import math
import re
from itertools import count
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field


app = FastAPI(title=__TITLE__, version="0.1.0")


class RequirementSummary(BaseModel):
    title: str
    objective: str


class SearchRequest(BaseModel):
    query: str = ""
    history: list[str] = Field(default_factory=list)


class OrderLine(BaseModel):
    product_id: str
    quantity: int = Field(default=1, ge=1)


class OrderRequest(BaseModel):
    items: list[OrderLine]
    receiver_name: str
    receiver_phone: str
    receiver_address: str


class OrderStatusUpdate(BaseModel):
    status: str


class ChatRequest(BaseModel):
    message: str
    product_id: str | None = None
    order_id: str | None = None


SEED_PRODUCTS: list[dict[str, Any]] = [
    {"id": "P-001", "name": "轻盈针织开衫", "price": 259.0, "stock": 18, "tags": ["衣服", "女装", "通勤", "秋冬"], "image": "https://images.unsplash.com/photo-1551028719-00167b16eac5?auto=format&fit=crop&w=900&q=80"},
    {"id": "P-002", "name": "直筒牛仔裤", "price": 329.0, "stock": 12, "tags": ["衣服", "裤子", "牛仔", "休闲"], "image": "https://images.unsplash.com/photo-1542272604-787c3835535d?auto=format&fit=crop&w=900&q=80"},
    {"id": "P-003", "name": "生日香薰礼盒", "price": 299.0, "stock": 18, "tags": ["生日", "礼物", "香薰"], "image": "https://images.unsplash.com/photo-1603006905003-be475563bc59?auto=format&fit=crop&w=900&q=80"},
    {"id": "P-004", "name": "轻量通勤双肩包", "price": 399.0, "stock": 12, "tags": ["通勤", "实用", "礼物"], "image": "https://images.unsplash.com/photo-1553062407-98eeb64c6a62?auto=format&fit=crop&w=900&q=80"},
    {"id": "P-005", "name": "桌面无线充电器", "price": 159.0, "stock": 30, "tags": ["数码", "办公", "实用"], "image": "https://images.unsplash.com/photo-1591290619762-c5889f8a7f2c?auto=format&fit=crop&w=900&q=80"},
]
ORDERS: dict[str, dict[str, Any]] = {}
ORDER_SEQUENCE = count(1001)


SEARCH_ALIASES = {
    "衣服": {"服装", "穿搭", "上衣", "女装", "男装"},
    "服装": {"衣服", "穿搭", "上衣"},
    "裤子": {"下装", "牛仔", "衣服"},
    "礼物": {"礼品", "生日", "送人"},
}


def _search_tokens(text: str) -> set[str]:
    tokens = set(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]", text.lower()))
    for phrase, aliases in SEARCH_ALIASES.items():
        if phrase in text.lower():
            tokens.add(phrase)
            tokens.update(aliases)
    return tokens


def _vectorize(text: str, dimensions: int = 64) -> list[float]:
    vector = [0.0] * dimensions
    for token in _search_tokens(text):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:2], "big") % dimensions
        vector[index] += 1.0
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


class VectorSearchIndex:
    """Offline vector index for local MVP; replace with pgvector in production."""

    def __init__(self, products: list[dict[str, Any]]) -> None:
        self.rows = [
            (product, _search_tokens(_product_search_text(product)), _vectorize(_product_search_text(product)))
            for product in products
        ]

    def similarity_search(self, query: str, limit: int = 10) -> list[tuple[dict[str, Any], float]]:
        query_tokens = _search_tokens(query)
        query_vector = _vectorize(query)
        scored = [
            (product, round(sum(left * right for left, right in zip(query_vector, vector)), 4))
            for product, tokens, vector in self.rows
            if query_tokens.intersection(tokens)
        ]
        return [(product, score) for product, score in sorted(scored, key=lambda item: item[1], reverse=True) if score > 0.12][:limit]


def _product_search_text(product: dict[str, Any]) -> str:
    return " ".join([product["name"], *product["tags"]])


SEARCH_INDEX = VectorSearchIndex(SEED_PRODUCTS)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "generated-commerce-backend"}


@app.get("/api/requirements", response_model=RequirementSummary)
def requirements() -> RequirementSummary:
    return RequirementSummary(title=__TITLE__, objective=__OBJECTIVE__)


@app.get("/api/products")
def list_products(query: str = "") -> list[dict[str, Any]]:
    if not query.strip():
        return SEED_PRODUCTS
    return [product for product, _score in SEARCH_INDEX.similarity_search(query)]


@app.post("/api/ai-search")
def ai_search(payload: SearchRequest) -> dict[str, Any]:
    query = " ".join([*payload.history, payload.query]).strip()
    ranked_items = SEARCH_INDEX.similarity_search(query)
    price_match = re.search(r"(?:低于|以内|不超过|小于)\\s*(\\d+)", query)
    if price_match:
        max_price = float(price_match.group(1))
        ranked_items = [(product, score) for product, score in ranked_items if product["price"] <= max_price]
    return {
        "query": query,
        "items": [
            {
                **product,
                "similarity": score,
                "recommendation_reason": f"根据“{query or '你的需求'}”匹配商品标签，支持货到付款，库存充足",
            }
            for product, score in ranked_items[:10]
        ],
        "fallback_hint": "" if ranked_items else "没有找到匹配商品，请换个关键词或联系客服。",
    }


@app.post("/api/orders")
def create_order(payload: OrderRequest) -> dict[str, Any]:
    if not payload.items:
        raise HTTPException(status_code=400, detail="订单至少需要一个商品")
    product_map = {product["id"]: product for product in SEED_PRODUCTS}
    lines: list[dict[str, Any]] = []
    total = 0.0
    for item in payload.items:
        product = product_map.get(item.product_id)
        if product is None or product["stock"] < item.quantity:
            raise HTTPException(status_code=400, detail="商品不存在或库存不足")
        line_total = product["price"] * item.quantity
        total += line_total
        lines.append({"product_id": product["id"], "name": product["name"], "quantity": item.quantity, "line_total": line_total})
    order_no = f"COD-{next(ORDER_SEQUENCE)}"
    order = {
        "order_no": order_no,
        "items": lines,
        "total": total,
        "cash_on_delivery": True,
        "status": "待发货",
        "receiver_name": payload.receiver_name,
        "receiver_phone": payload.receiver_phone,
        "receiver_address": payload.receiver_address,
    }
    ORDERS[order_no] = order
    return order


@app.get("/api/orders")
def list_orders() -> list[dict[str, Any]]:
    return list(ORDERS.values())


@app.get("/api/orders/{order_no}")
def get_order(order_no: str) -> dict[str, Any]:
    order = ORDERS.get(order_no)
    if order is None:
        raise HTTPException(status_code=404, detail="订单不存在")
    return order


@app.patch("/api/orders/{order_no}")
def update_order(order_no: str, payload: OrderStatusUpdate) -> dict[str, Any]:
    order = ORDERS.get(order_no)
    if order is None:
        raise HTTPException(status_code=404, detail="订单不存在")
    if payload.status not in {"待发货", "待收货", "已完成", "已拒收"}:
        raise HTTPException(status_code=400, detail="不支持的订单状态")
    order["status"] = payload.status
    return order


@app.post("/api/ai-chat")
def ai_chat(payload: ChatRequest) -> dict[str, Any]:
    message = payload.message.lower()
    if "货到付款" in message or "cod" in message or "付款" in message:
        return {"answer": "本商城默认货到付款，快递送达后现金支付订单金额。", "handoff": False}
    if "库存" in message or "价格" in message or "多少钱" in message:
        return {"answer": "我可以查询商品价格和库存，请告诉我商品名称。", "handoff": False}
    return {"answer": "这个问题超出当前商品知识范围，我已建议转人工留言。", "handoff": True}


@app.get("/api/admin/products")
def admin_products() -> list[dict[str, Any]]:
    return SEED_PRODUCTS
'''.replace("__TITLE__", title_literal).replace("__OBJECTIVE__", objective_literal)


def _backend(title: str, objective: str, commerce: bool = False) -> str:
    if commerce:
        return _commerce_backend(title, objective)
    return f'''"""Generated API for {title}."""

from fastapi import FastAPI
from pydantic import BaseModel


app = FastAPI(title={title!r}, version="0.1.0")


class RequirementSummary(BaseModel):
    title: str
    objective: str


@app.get("/api/health")
def health() -> dict[str, str]:
    return {{"status": "ok", "service": "generated-backend"}}


@app.get("/api/requirements", response_model=RequirementSummary)
def requirements() -> RequirementSummary:
    return RequirementSummary(title={title!r}, objective={objective!r})
'''


def _backend_test(commerce: bool = False) -> str:
    if commerce:
        return '''import unittest

from fastapi import HTTPException
from backend.app.main import (
    ChatRequest,
    OrderLine,
    OrderRequest,
    OrderStatusUpdate,
    SearchRequest,
    ai_chat,
    ai_search,
    create_order,
    list_products,
    update_order,
)


class CommerceContractTests(unittest.TestCase):
    def test_ai_search_returns_reasons_and_products(self) -> None:
        result = ai_search(SearchRequest(query="生日礼物 500 元以内"))
        self.assertTrue(result["items"])
        self.assertTrue(result["items"][0]["recommendation_reason"])

    def test_ai_search_returns_relevant_category(self) -> None:
        result = ai_search(SearchRequest(query="衣服"))
        self.assertTrue(result["items"])
        self.assertTrue(all("衣服" in item["tags"] for item in result["items"]))

    def test_ai_search_does_not_fallback_to_unrelated_products(self) -> None:
        result = ai_search(SearchRequest(query="不存在的商品品类"))
        self.assertEqual(result["items"], [])
        self.assertTrue(result["fallback_hint"])

    def test_cod_order_and_customer_service_contracts(self) -> None:
        product = list_products()[0]
        order = create_order(
            OrderRequest(
                items=[OrderLine(product_id=product["id"], quantity=1)],
                receiver_name="测试用户",
                receiver_phone="13800000000",
                receiver_address="测试地址",
            )
        )
        self.assertTrue(order["order_no"])
        self.assertTrue(order["cash_on_delivery"])
        reply = ai_chat(ChatRequest(message="支持货到付款吗？"))
        self.assertIn("货到付款", reply["answer"])

    def test_order_status_rejects_invalid_values(self) -> None:
        product = list_products()[0]
        order = create_order(
            OrderRequest(
                items=[OrderLine(product_id=product["id"], quantity=1)],
                receiver_name="测试用户",
                receiver_phone="13800000000",
                receiver_address="测试地址",
            )
        )
        updated = update_order(order["order_no"], OrderStatusUpdate(status="待收货"))
        self.assertEqual(updated["status"], "待收货")
        with self.assertRaises(HTTPException):
            update_order(order["order_no"], OrderStatusUpdate(status="未知状态"))


if __name__ == "__main__":
    unittest.main()
'''
    return '''import unittest

from backend.app.main import health, requirements


class BackendContractTests(unittest.TestCase):
    def test_health_contract(self) -> None:
        self.assertEqual(health()["status"], "ok")

    def test_requirement_contract(self) -> None:
        result = requirements()
        self.assertTrue(result.title)
        self.assertTrue(result.objective)


if __name__ == "__main__":
    unittest.main()
'''


def _package_json(title: str, vue: bool) -> str:
    if vue:
        value = {
            "name": "vaf-generated-vue-app",
            "private": True,
            "version": "0.1.0",
            "type": "module",
            "scripts": {"dev": "vite", "build": "vite build"},
            "dependencies": {"@vitejs/plugin-vue": "^6.0.8", "vite": "^8.2.0", "vue": "^3.5.41"},
            "devDependencies": {},
        }
    else:
        value = {
            "name": "vaf-generated-react-app",
            "private": True,
            "version": "0.1.0",
            "type": "module",
            "scripts": {"dev": "vite", "build": "vite build"},
            "dependencies": {"@vitejs/plugin-react": "^6.0.5", "react": "^19.2.8", "react-dom": "^19.2.8", "vite": "^8.2.0"},
            "devDependencies": {},
        }
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def _vite_config(vue: bool) -> str:
    plugin = "vue" if vue else "react"
    return f'''import {{ defineConfig }} from "vite";
import {plugin} from "@vitejs/plugin-{plugin}";

export default defineConfig({{
  plugins: [{plugin}()],
  server: {{
    proxy: {{ "/api": "http://127.0.0.1:8000" }},
  }},
}});
'''


def _index_html(title: str) -> str:
    return f'''<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>{title}</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.jsx"></script>
  </body>
</html>
'''


def _frontend_main(vue: bool) -> str:
    if vue:
        return '''import { createApp } from "vue";
import App from "./App.vue";
import "./styles.css";

createApp(App).mount("#root");
'''
    return '''import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App.jsx";
import "./styles.css";

createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
'''


def _commerce_frontend(title: str, summary: str) -> str:
    title_literal = json.dumps(title, ensure_ascii=False)
    return """import { useEffect, useMemo, useState } from "react";

const fallback = { title: __TITLE__, objective: __OBJECTIVE__ };

export default function App() {
  const [requirement, setRequirement] = useState(fallback);
  const [products, setProducts] = useState([]);
  const [cart, setCart] = useState([]);
  const [query, setQuery] = useState("生日礼物");
  const [health, setHealth] = useState("loading");
  const [order, setOrder] = useState(null);
  const [message, setMessage] = useState("");
  const [chat, setChat] = useState([]);
  const [searchHint, setSearchHint] = useState("");

  useEffect(() => {
    Promise.all([fetch("/api/health"), fetch("/api/requirements"), fetch("/api/products")])
      .then(async ([healthResponse, requirementResponse, productsResponse]) => {
        setHealth(healthResponse.ok ? "online" : "offline");
        if (requirementResponse.ok) setRequirement(await requirementResponse.json());
        if (productsResponse.ok) setProducts(await productsResponse.json());
      })
      .catch(() => setHealth("offline"));
  }, []);

  const total = useMemo(() => cart.reduce((sum, item) => sum + item.price * item.quantity, 0), [cart]);

  async function search(event) {
    event.preventDefault();
    const response = await fetch("/api/ai-search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query }),
    });
    if (response.ok) {
      const result = await response.json();
      setProducts(result.items);
      setSearchHint(result.fallback_hint || "");
    }
  }

  function addToCart(product) {
    setCart((current) => {
      const existing = current.find((item) => item.id === product.id);
      if (existing) return current.map((item) => item.id === product.id ? { ...item, quantity: item.quantity + 1 } : item);
      return [...current, { ...product, quantity: 1 }];
    });
  }

  async function checkout(event) {
    event.preventDefault();
    const response = await fetch("/api/orders", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        items: cart.map((item) => ({ product_id: item.id, quantity: item.quantity })),
        receiver_name: "演示用户",
        receiver_phone: "13800000000",
        receiver_address: "本地演示地址",
      }),
    });
    if (response.ok) {
      setOrder(await response.json());
      setCart([]);
    }
  }

  async function ask(event) {
    event.preventDefault();
    if (!message.trim()) return;
    const current = message;
    setMessage("");
    const response = await fetch("/api/ai-chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: current }),
    });
    if (response.ok) {
      const reply = await response.json();
      setChat((items) => [...items, { role: "user", text: current }, { role: "assistant", text: reply.answer }]);
    }
  }

  return (
    <main className="commerce-shell">
      <header className="topbar">
        <div className="brand-lockup"><span className="brand-mark">V</span><div><span className="eyebrow">VAF MARKET</span><h1>{requirement.title}</h1></div></div>
        <div className="status" data-state={health}>Backend: {health}</div>
      </header>
      <p className="lead">{requirement.objective}</p>
      <nav className="nav"><a href="#search">/search</a><a href="#cart">/cart</a><a href="#checkout">/checkout</a><a href="#admin">/admin</a></nav>
      <section id="search" className="section">
        <div className="section-heading"><div><span className="eyebrow">AI SEARCH</span><h2>描述你想买什么</h2></div><span className="contract">POST /api/ai-search</span></div>
        <form className="search-form" onSubmit={search}><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="例如：送女友的生日礼物 500 内" /><button type="submit">AI 智能选品</button></form>
        {searchHint && <p className="search-hint">{searchHint}</p>}
        <div className="product-grid">{products.length ? products.map((product) => <article className="product" key={product.id}><div className="product-image">{product.image ? <img src={product.image} alt={product.name} /> : <div className="product-art">{product.tags[0]}</div>}</div><div className="product-meta"><span className="tag">{product.tags[0]}</span><span className="stock">库存 {product.stock}</span></div><h3>{product.name}</h3><p className="price">¥{product.price}</p><p className="muted">{product.recommendation_reason || product.tags.join(" / ")}</p><button type="button" onClick={() => addToCart(product)}>加入购物车</button></article>) : <div className="empty-state"><strong>暂时没有找到合适的商品</strong><span>换个说法，或者直接联系客服帮你找。</span></div>}</div>
      </section>
      <section id="cart" className="section split"><div><span className="eyebrow">COD CHECKOUT</span><h2>购物车与下单</h2><p className="muted">默认货到付款，无支付方式选择。</p>{cart.length === 0 ? <p className="empty">购物车为空</p> : cart.map((item) => <div className="cart-line" key={item.id}><span>{item.name} × {item.quantity}</span><strong>¥{item.price * item.quantity}</strong></div>)}</div><form id="checkout" className="checkout" onSubmit={checkout}><span className="contract">POST /api/orders</span><h3>确认地址</h3><label>收货人<input defaultValue="演示用户" /></label><label>收货地址<input defaultValue="本地演示地址" /></label><div className="total">合计 ¥{total.toFixed(2)}</div><button disabled={!cart.length} type="submit">提交 COD 订单</button>{order && <p className="success">订单 {order.order_no} 已提交，送达后现金支付 ¥{order.total}</p>}</form></section>
      <section id="admin" className="section split"><div><span className="eyebrow">AI CUSTOMER SERVICE</span><h2>商品咨询</h2><p className="muted">客服接口：POST /api/ai-chat · 无法回答时建议转人工。</p>{chat.map((item, index) => <p className={item.role === "user" ? "chat user" : "chat"} key={index}>{item.text}</p>)}<form className="chat-form" onSubmit={ask}><input value={message} onChange={(event) => setMessage(event.target.value)} placeholder="问问商品、库存或货到付款" /><button type="submit">发送</button></form></div><div className="panel"><span className="eyebrow">MERCHANT ADMIN</span><h3>管理接口</h3><p>/api/admin/products</p><p>/api/orders</p><p className="muted">商品、订单状态和客服留言均保留清晰的 API 边界。</p></div></section>
    </main>
  );
}
""".replace("__TITLE__", title_literal).replace("__OBJECTIVE__", summary)


def _frontend_app(title: str, summary: str, requirements: str, vue: bool, commerce: bool = False) -> str:
    if commerce and not vue:
        return _commerce_frontend(title, summary)
    if vue:
        return f'''<script setup>
import {{ onMounted, ref }} from "vue";

const health = ref("loading");
const requirement = ref({{ title: {title!r}, objective: {summary} }});
onMounted(async () => {{
  try {{
    const response = await fetch("/api/health");
    health.value = response.ok ? "online" : "offline";
  }} catch {{
    health.value = "offline";
  }}
}});
</script>

<template>
  <main class="shell">
    <span class="eyebrow">VAF GENERATED PRODUCT</span>
    <h1>{{{{ requirement.title }}}}</h1>
    <p class="lead">{{{{ requirement.objective }}}}</p>
    <div class="status" :data-state="health">Backend: {{{{ health }}}}</div>
    <section class="panel"><h2>Delivered contract</h2><p>REQ-001 / AC-001 / AC-002</p></section>
  </main>
</template>
'''
    return f'''import {{ useEffect, useState }} from "react";

const fallback = {{ title: {title!r}, objective: {summary} }};

export default function App() {{
  const [health, setHealth] = useState("loading");
  const [requirement, setRequirement] = useState(fallback);
  useEffect(() => {{
    Promise.all([fetch("/api/health"), fetch("/api/requirements")])
      .then(async ([healthResponse, requirementResponse]) => {{
        setHealth(healthResponse.ok ? "online" : "offline");
        if (requirementResponse.ok) setRequirement(await requirementResponse.json());
      }})
      .catch(() => setHealth("offline"));
  }}, []);
  return (
    <main className="shell">
      <span className="eyebrow">VAF GENERATED PRODUCT</span>
      <h1>{{requirement.title}}</h1>
      <p className="lead">{{requirement.objective}}</p>
      <div className="status" data-state={{health}}>Backend: {{health}}</div>
      <section className="panel"><h2>Delivered contract</h2><p>REQ-001 / AC-001 / AC-002</p></section>
    </main>
  );
}}
'''


def _frontend_css() -> str:
    return '''
:root { color: #1f2937; background: #f7f8fa; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
* { box-sizing: border-box; }
body { margin: 0; min-width: 320px; background: #f7f8fa; }
.shell { max-width: 880px; min-height: 100vh; margin: 0 auto; padding: 12vh 28px; }
.eyebrow { color: #e85d32; font-size: 11px; font-weight: 800; letter-spacing: .12em; }
h1 { max-width: 720px; margin: 18px 0 12px; font-size: clamp(38px, 7vw, 76px); line-height: 1.02; }
.lead { max-width: 680px; color: #667085; font-size: 20px; line-height: 1.6; }
.status { display: inline-block; margin: 28px 0; padding: 8px 12px; border: 1px solid #a7e5c3; border-radius: 999px; color: #16834b; background: #effcf4; font-size: 13px; }
.status[data-state="offline"] { border-color: #f2b5ae; color: #c53030; background: #fff4f2; }
.panel { max-width: 560px; padding: 22px; border: 1px solid #e5e7eb; border-radius: 14px; background: #fff; box-shadow: 0 10px 30px rgba(16, 24, 40, .05); }
.panel h2 { margin-top: 0; font-size: 16px; }
.panel p { color: #667085; }
.commerce-shell { max-width: 1240px; min-height: 100vh; margin: 0 auto; padding: 24px 32px 72px; }
.topbar, .section-heading, .split { display: flex; justify-content: space-between; gap: 28px; align-items: flex-start; }
.brand-lockup { display: flex; gap: 12px; align-items: center; }
.brand-mark { display: grid; place-items: center; width: 38px; height: 38px; border-radius: 10px; color: #fff; background: #e85d32; font-size: 22px; font-weight: 900; }
.topbar h1 { margin: 4px 0 0; color: #111827; font-size: 24px; }
.commerce-shell .lead { max-width: 820px; margin: 34px 0 24px; color: #667085; font-size: 16px; }
.nav { display: flex; gap: 24px; padding: 16px 0; border-top: 1px solid #e5e7eb; border-bottom: 1px solid #e5e7eb; }
.nav a, .contract { color: #e85d32; font-size: 13px; text-decoration: none; }
.section { margin-top: 34px; padding-top: 28px; border-top: 1px solid #e5e7eb; }
.section h2 { margin: 6px 0 18px; color: #111827; font-size: 30px; letter-spacing: -.01em; }
.search-form, .chat-form { display: flex; gap: 10px; margin-bottom: 12px; }
input { width: 100%; padding: 13px 15px; border: 1px solid #d0d5dd; border-radius: 8px; outline: none; color: #1f2937; background: #fff; font: inherit; }
input:focus { border-color: #e85d32; box-shadow: 0 0 0 3px rgba(232, 93, 50, .12); }
button { padding: 12px 18px; border: 0; border-radius: 8px; color: #fff; background: #e85d32; font: inherit; font-weight: 800; cursor: pointer; white-space: nowrap; }
button:hover { background: #c94b25; }
button:disabled { cursor: not-allowed; opacity: .4; }
.search-hint { margin: 14px 0 20px; color: #667085; }
.product-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 16px; }
.product, .checkout, .panel { padding: 0; border: 1px solid #e5e7eb; border-radius: 12px; background: #fff; box-shadow: 0 8px 24px rgba(16, 24, 40, .04); overflow: hidden; }
.product { transition: transform .18s ease, box-shadow .18s ease; }
.product:hover { transform: translateY(-2px); box-shadow: 0 14px 28px rgba(16, 24, 40, .09); }
.product-image { aspect-ratio: 1 / 1; overflow: hidden; background: #f2f4f7; }
.product-image img { display: block; width: 100%; height: 100%; object-fit: cover; }
.product-art { display: grid; place-items: center; height: 100%; color: #e85d32; background: #fff2ed; font-size: 24px; font-weight: 800; }
.product-meta { display: flex; justify-content: space-between; gap: 8px; padding: 14px 16px 0; }
.tag { padding: 4px 7px; border-radius: 4px; color: #c94b25; background: #fff2ed; font-size: 11px; font-weight: 800; }
.stock { color: #98a2b3; font-size: 12px; }
.product h3, .product .price, .product .muted, .product button { margin-left: 16px; margin-right: 16px; }
.product h3 { margin-top: 10px; margin-bottom: 8px; color: #1f2937; font-size: 16px; }
.price { margin-top: 0; margin-bottom: 8px; color: #e85d32; font-size: 22px; font-weight: 900; }
.muted, .empty { color: #667085; font-size: 13px; line-height: 1.5; }
.product button { width: calc(100% - 32px); margin-top: 10px; margin-bottom: 16px; }
.empty-state { display: grid; gap: 8px; min-height: 180px; place-items: center; grid-column: 1 / -1; padding: 32px; border: 1px dashed #d0d5dd; border-radius: 12px; color: #667085; background: #fff; text-align: center; }
.empty-state strong { color: #344054; }
.split > * { flex: 1; min-width: 0; }
.cart-line { display: flex; justify-content: space-between; gap: 16px; padding: 14px 0; border-bottom: 1px solid #eaecf0; }
.checkout { max-width: 430px; padding: 22px; }
.checkout label { display: block; margin: 12px 0; color: #667085; font-size: 13px; }
.checkout label input { margin-top: 6px; }
.total { margin: 20px 0; color: #e85d32; font-size: 24px; font-weight: 900; }
.success { color: #16834b; line-height: 1.5; }
.chat { max-width: 620px; padding: 10px 12px; border-radius: 8px; color: #344054; background: #f2f4f7; }
.chat.user { margin-left: auto; color: #fff; background: #e85d32; }
.chat-form { max-width: 620px; }
@media (max-width: 960px) { .product-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 760px) { .topbar, .section-heading, .split { display: block; } .topbar .status { margin: 18px 0 0; } .product-grid { grid-template-columns: 1fr; } .checkout { max-width: none; margin-top: 24px; } .search-form { display: grid; grid-template-columns: 1fr auto; } }
'''
