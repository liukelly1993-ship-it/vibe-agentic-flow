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


class PrdTemplateAgent:
    """Generate a runnable FastAPI plus Vite application from a PRD context."""

    def __init__(self, context: PrdContext) -> None:
        self.context = context

    def draft_prd(self, change_id: str, title: str, objective: str, version: int = 1) -> DraftResult:
        now = datetime.now(timezone.utc).isoformat()
        excerpt = _source_excerpt(self.context.source_text)
        content = f"""---
artifact_id: PRD-{change_id}
artifact_type: prd
change_id: {change_id}
version: {version}
status: waiting_review
requirements: [REQ-001]
source_hash: {self.context.source_hash}
created_by: vaf-local-prd-agent
created_at: {now}
---

# PRD：{self.context.title}

## 来源与边界

本产物由上传文档生成，原始来源哈希为 `{self.context.source_hash}`。未出现在原始文档中的业务行为不会被自动扩展。

## 问题与目标

{self.context.objective}

## REQ-001：核心业务目标

WHEN 用户提交本 PRD 所描述的业务请求
THE SYSTEM SHALL 提供一个可以在本地启动、验证和追踪的前后端实现

## 验收条件

- AC-001：后端健康接口可以通过自动化测试验证。
- AC-002：前端可以启动并展示 PRD 目标、技术栈和后端状态。

## 原始需求证据

```text
{excerpt}
```
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
        bodies = {
            "technical-design": f"""## 设计目标

实现 REQ-001 对应的可本地运行前后端，并保持 API、前端和数据库边界清晰。

## 技术栈

- 后端：{self.context.stack.backend}
- 前端：{self.context.stack.frontend}
- 数据库候选：{self.context.stack.database}；本地模板默认 SQLite

## 影响范围

变更限定在 `backend/`、`frontend/` 和 `tests/`，根目录 README 只记录启动方式。服务通过 `/api` 前缀提供接口，前端通过 Vite 代理访问后端。

## 安全与风险

默认不读取 Secret，不执行动态代码，不启用外部网络；输入和接口响应使用显式 Schema。{self.context.stack.warnings[0] if self.context.stack.warnings else "当前 PRD 没有识别出额外的技术栈风险。"}

## 风险与验证

使用后端 unittest 验证健康接口和需求摘要；前端保留独立 `npm run build` 入口，最终质量门绑定代码范围、Trace 和验证退出码。""",
            "test-cases": """## 测试范围

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
requirements: [REQ-001]
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
        items: list[dict[str, object]] = [
            _item("README.md", _readme(self.context, frontend_is_vue)),
            _item("backend/__init__.py", ""),
            _item("backend/app/__init__.py", ""),
            _item("backend/requirements.txt", "fastapi>=0.115\nuvicorn[standard]>=0.30\npydantic>=2.0\n"),
            _item("backend/app/main.py", _backend(self.context.title, self.context.objective, commerce)),
            _item("frontend/package.json", _package_json(self.context.title, frontend_is_vue)),
            _item("frontend/vite.config.js", _vite_config(frontend_is_vue)),
            _item("frontend/index.html", _index_html(self.context.title)),
            _item("frontend/src/main.jsx", _frontend_main(frontend_is_vue)),
            _item(
                "frontend/src/App.vue" if frontend_is_vue else "frontend/src/App.jsx",
                _frontend_app(self.context.title, summary, requirements, frontend_is_vue, commerce),
            ),
            _item("frontend/src/styles.css", _frontend_css()),
            _item("tests/__init__.py", ""),
            _item("tests/test_backend.py", _backend_test(commerce)),
        ]
        return items

    def _implementation_plan_body(self) -> str:
        paths = ", ".join(str(item["path"]) for item in self.implementation_items())
        return f"""## TASK-001：生成可本地运行的前后端

实现 REQ-001，覆盖 AC-001 和 AC-002，使用 {self.context.stack.backend} + {self.context.stack.frontend}。

实施范围：{paths}

完成条件：后端测试通过、前端具有启动脚本、所有文件在声明范围内、TraceLink 和质量门通过。"""


def _item(path: str, content: str) -> dict[str, object]:
    item: dict[str, object] = {
        "task_id": "TASK-001",
        "path": path,
        "content": content,
        "requirement_ids": ["REQ-001"],
    }
    if path.startswith("tests/"):
        item["acceptance_ids"] = ["AC-001", "AC-002"]
        item["test_ids"] = ["TC-001", "TC-002"]
    return item


def _source_excerpt(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines[:80])[:8000]


def _readme(context: PrdContext, frontend_is_vue: bool) -> str:
    frontend_command = "npm run dev" if frontend_is_vue else "npm run dev"
    return f"""# {context.title}

这是由 VAF 根据 PRD 自动生成的本地可运行项目。

## 技术栈

- Backend: {context.stack.backend}
- Frontend: {context.stack.frontend}
- Database boundary: {context.stack.database}（本地默认 SQLite）

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
    {"id": "P-001", "name": "生日香薰礼盒", "price": 299.0, "stock": 18, "tags": ["生日", "礼物", "香薰"]},
    {"id": "P-002", "name": "轻量通勤双肩包", "price": 399.0, "stock": 12, "tags": ["通勤", "实用", "礼物"]},
    {"id": "P-003", "name": "桌面无线充电器", "price": 159.0, "stock": 30, "tags": ["数码", "办公", "实用"]},
]
ORDERS: dict[str, dict[str, Any]] = {}
ORDER_SEQUENCE = count(1001)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "generated-commerce-backend"}


@app.get("/api/requirements", response_model=RequirementSummary)
def requirements() -> RequirementSummary:
    return RequirementSummary(title=__TITLE__, objective=__OBJECTIVE__)


@app.get("/api/products")
def list_products(query: str = "") -> list[dict[str, Any]]:
    terms = [term for term in re.split(r"\\s+", query.lower()) if term]
    if not terms:
        return SEED_PRODUCTS
    return [
        product for product in SEED_PRODUCTS
        if any(term in (product["name"] + " " + " ".join(product["tags"])).lower() for term in terms)
    ]


@app.post("/api/ai-search")
def ai_search(payload: SearchRequest) -> dict[str, Any]:
    query = " ".join([*payload.history, payload.query]).strip()
    items = list_products(query)
    if not items:
        items = list_products()
    return {
        "query": query,
        "items": [
            {
                **product,
                "recommendation_reason": f"匹配“{query or '你的需求'}”，支持货到付款，库存充足",
            }
            for product in items[:10]
        ],
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

from backend.app.main import (
    ChatRequest,
    OrderLine,
    OrderRequest,
    SearchRequest,
    ai_chat,
    ai_search,
    create_order,
    list_products,
)


class CommerceContractTests(unittest.TestCase):
    def test_ai_search_returns_reasons_and_products(self) -> None:
        result = ai_search(SearchRequest(query="生日礼物 500 元以内"))
        self.assertTrue(result["items"])
        self.assertTrue(result["items"][0]["recommendation_reason"])

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
    if (response.ok) setProducts((await response.json()).items);
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
        <div><span className="eyebrow">VAF GENERATED COMMERCE MVP</span><h1>{requirement.title}</h1></div>
        <div className="status" data-state={health}>Backend: {health}</div>
      </header>
      <p className="lead">{requirement.objective}</p>
      <nav className="nav"><a href="#search">/search</a><a href="#cart">/cart</a><a href="#checkout">/checkout</a><a href="#admin">/admin</a></nav>
      <section id="search" className="section">
        <div className="section-heading"><div><span className="eyebrow">AI SEARCH</span><h2>描述你想买什么</h2></div><span className="contract">POST /api/ai-search</span></div>
        <form className="search-form" onSubmit={search}><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="例如：送女友的生日礼物 500 内" /><button type="submit">AI 选品</button></form>
        <div className="product-grid">{products.map((product) => <article className="product" key={product.id}><div className="product-art">{product.tags[0]}</div><h3>{product.name}</h3><p className="price">¥{product.price}</p><p className="muted">库存 {product.stock} · {product.recommendation_reason || product.tags.join(" / ")}</p><button type="button" onClick={() => addToCart(product)}>加入购物车</button></article>)}</div>
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
:root { color: #eaf2ff; background: #09111f; font-family: Inter, ui-sans-serif, system-ui, sans-serif; }
* { box-sizing: border-box; }
body { margin: 0; min-width: 320px; background: radial-gradient(circle at 80% 0%, #163c52 0, transparent 42%), #09111f; }
.shell { max-width: 880px; min-height: 100vh; margin: 0 auto; padding: 12vh 28px; }
.eyebrow { color: #69d6c4; font-size: 12px; font-weight: 800; letter-spacing: .12em; }
h1 { max-width: 720px; margin: 18px 0 12px; font-size: clamp(38px, 7vw, 76px); line-height: 1.02; }
.lead { max-width: 680px; color: #a9bad2; font-size: 20px; line-height: 1.6; }
.status { display: inline-block; margin: 28px 0; padding: 10px 14px; border: 1px solid #2f8f88; border-radius: 999px; color: #8df3db; }
.status[data-state="offline"] { border-color: #db6a75; color: #ff9ca6; }
.panel { max-width: 560px; padding: 22px; border: 1px solid #23415a; border-radius: 10px; background: #0e1d31; }
.panel h2 { margin-top: 0; font-size: 16px; }
.panel p { color: #a9bad2; }
.commerce-shell { max-width: 1180px; min-height: 100vh; margin: 0 auto; padding: 34px 28px 72px; }
.topbar, .section-heading, .split { display: flex; justify-content: space-between; gap: 28px; align-items: flex-start; }
.topbar h1 { margin: 8px 0 0; font-size: clamp(30px, 5vw, 54px); }
.commerce-shell .lead { max-width: 820px; margin: 18px 0; font-size: 17px; }
.nav { display: flex; gap: 18px; padding: 16px 0; border-top: 1px solid #1d3850; border-bottom: 1px solid #1d3850; }
.nav a, .contract { color: #69d6c4; font-size: 13px; text-decoration: none; }
.section { margin-top: 34px; padding-top: 26px; border-top: 1px solid #1d3850; }
.section h2 { margin: 6px 0 18px; font-size: 28px; }
.search-form, .chat-form { display: flex; gap: 10px; margin-bottom: 22px; }
input { width: 100%; padding: 12px 14px; border: 1px solid #31516a; border-radius: 6px; color: #eaf2ff; background: #0e1d31; font: inherit; }
button { padding: 11px 16px; border: 0; border-radius: 6px; color: #08141f; background: #69d6c4; font: inherit; font-weight: 800; cursor: pointer; }
button:disabled { cursor: not-allowed; opacity: .4; }
.product-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; }
.product, .checkout, .panel { padding: 18px; border: 1px solid #23415a; border-radius: 8px; background: #0e1d31; }
.product-art { display: grid; place-items: center; aspect-ratio: 16 / 9; margin-bottom: 16px; color: #8df3db; background: #17354b; }
.product h3 { margin: 0 0 8px; }
.price { margin: 0 0 8px; color: #ffcf70; font-size: 24px; font-weight: 800; }
.muted, .empty { color: #a9bad2; }
.product button { width: 100%; margin-top: 12px; }
.split > * { flex: 1; min-width: 0; }
.cart-line { display: flex; justify-content: space-between; gap: 16px; padding: 12px 0; border-bottom: 1px solid #1d3850; }
.checkout { max-width: 430px; }
.checkout label { display: block; margin: 12px 0; color: #a9bad2; font-size: 13px; }
.checkout label input { margin-top: 6px; }
.total { margin: 20px 0; color: #ffcf70; font-size: 24px; font-weight: 800; }
.success { color: #8df3db; line-height: 1.5; }
.chat { max-width: 620px; padding: 10px 12px; border-radius: 6px; background: #17354b; }
.chat.user { margin-left: auto; color: #08141f; background: #69d6c4; }
.chat-form { max-width: 620px; }
@media (max-width: 760px) { .topbar, .section-heading, .split { display: block; } .topbar .status { margin: 18px 0 0; } .product-grid { grid-template-columns: 1fr; } .checkout { max-width: none; margin-top: 24px; } }
'''
