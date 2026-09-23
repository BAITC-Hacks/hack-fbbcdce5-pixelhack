import asyncio
import unittest
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from pydantic import HttpUrl

from app.core.config import Settings
from app.core.errors import AppError
from app.integrations.ekt import EktClient, normalize_product
from app.main import create_app
from app.schemas import PurchaseTerms


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(Settings(_env_file=None, catalog_mode="demo", assistant_mode="demo"))
        self.client = TestClient(self.app)
        self.client.__enter__()
        created = self.client.post("/api/v1/sessions").json()
        self.sid = created["session_id"]
        self.base = f"/api/v1/sessions/{self.sid}"
        self.auth = {"Authorization": f"Bearer {created['access_token']}"}

    def tearDown(self):
        self.client.__exit__(None, None, None)

    def proposal(self, quantity="2", product_id="demo-1"):
        return self.client.post(self.base + "/cart/proposals", headers=self.auth,
                                json={"product_id": product_id, "quantity": quantity})

    def confirm(self, action_id, **kwargs):
        return self.client.post(self.base + f"/cart/proposals/{action_id}/confirm", headers=self.auth, **kwargs)

    def test_catalog_and_analog(self):
        product = self.client.get("/api/v1/products/demo-2").json()
        self.assertEqual(product["availability"], "out_of_stock")
        alternatives = self.client.get("/api/v1/products/demo-2/alternatives").json()["items"]
        self.assertEqual([a["product"]["id"] for a in alternatives], ["demo-1"])
        self.assertTrue(alternatives[0]["reason"])
        self.assertEqual(self.client.get("/api/v1/products/missing").status_code, 404)
        self.assertEqual(self.client.get("/api/v1/products?q=DEMO-C16-OLD").json()["items"][0]["id"], "demo-2")

    def test_demo_drill_has_local_photo_in_chat(self):
        product = self.client.get("/api/v1/products/demo-5").json()
        self.assertEqual(product["article"], "DEMO-DRILL18")
        self.assertEqual(product["image"], "/demo-drill.jpg")
        image = self.client.get(product["image"])
        self.assertEqual(image.status_code, 200)
        self.assertEqual(image.headers["content-type"], "image/jpeg")
        response = self.chat("Покажи шуруповерт")
        self.assertEqual(response["products"][0]["image"], product["image"])
        self.assertEqual(response["cart"]["items"], [])

    def test_confirmation_is_required_and_idempotent(self):
        action = self.proposal().json()["id"]
        self.assertEqual(self.client.get(self.base + "/cart", headers=self.auth).json()["items"], [])
        self.assertEqual(self.confirm(action, json={"confirmed": False}).status_code, 422)
        self.assertEqual(self.confirm(action, json={}).status_code, 422)
        result = self.confirm(action, json={"confirmed": True})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["items"][0]["quantity"], "2")
        repeated = self.confirm(action, json={"confirmed": True}).json()
        self.assertEqual(repeated["items"][0]["quantity"], "2")
        self.assertEqual(self.client.get(repeated["cart_url"], headers=self.auth).json(), repeated)

    def test_quantity_and_stock(self):
        for quantity in ("0", "-1", "NaN", "Infinity", "1.0001"):
            self.assertEqual(self.proposal(quantity).status_code, 422)
        self.assertEqual(self.proposal("11").status_code, 409)
        self.assertEqual(self.proposal("1.5").status_code, 409)
        self.assertEqual(self.proposal("0.5", "demo-4").status_code, 201)
        action = self.proposal("8").json()["id"]
        self.confirm(action, json={"confirmed": True})
        self.assertEqual(self.proposal("3").status_code, 409)

    def test_changed_stock_price_and_expiry(self):
        action = self.proposal().json()["id"]
        product = self.app.state.catalog.demo["demo-1"]
        product.price += 1
        self.assertEqual(self.confirm(action, json={"confirmed": True}).json()["error"]["code"], "price_changed")
        product.price -= 1
        product.quantity = Decimal("1")
        self.assertEqual(self.confirm(action, json={"confirmed": True}).json()["error"]["code"], "insufficient_stock")
        product.quantity = Decimal("10")
        self.app.state.sessions.sessions[self.sid].pending.expires_at -= timedelta(hours=1)
        self.assertEqual(self.confirm(action, json={"confirmed": True}).json()["error"]["code"], "action_expired")

    def test_session_isolation_deletion_and_expiry(self):
        other = self.client.post("/api/v1/sessions").json()
        wrong = {"Authorization": f"Bearer {other['access_token']}"}
        self.assertEqual(self.client.get(self.base + "/cart", headers=wrong).status_code, 401)
        self.assertEqual(self.client.get(self.base + "/cart").status_code, 401)
        self.assertEqual(self.client.delete(self.base, headers=self.auth).status_code, 204)
        self.assertEqual(self.client.get(self.base + "/cart", headers=self.auth).status_code, 401)
        session = self.app.state.sessions.sessions[other["session_id"]]
        session.expires_at -= timedelta(hours=2)
        self.assertEqual(self.client.get(f"/api/v1/sessions/{session.id}/cart", headers=wrong).status_code, 401)

    def test_chat_does_not_mutate_cart(self):
        response = self.client.post(self.base + "/chat", headers=self.auth,
                                    json={"message": "Добавь DEMO-C16 2 шт"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNotNone(response.json()["pending_action"])
        response = self.client.post(self.base + "/chat", headers=self.auth, json={"message": "да, добавь"})
        self.assertEqual(response.json()["cart"]["items"], [])
        self.assertEqual(len(self.app.state.sessions.sessions[self.sid].history), 4)

    def chat(self, message):
        response = self.client.post(self.base + "/chat", headers=self.auth, json={"message": message})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(set(body), {"message", "products", "alternatives", "pending_action", "cart", "assistant_mode"})
        return body

    def test_assistant_product_facts_and_context(self):
        product = self.app.state.catalog.demo["demo-1"]
        product.certificates = [HttpUrl("https://example.org/certificate.pdf")]
        first = self.chat("Есть DEMO-C16?")
        self.assertEqual(first["products"][0]["id"], "demo-1")
        self.assertIn("1500.00 KZT", first["message"])
        self.assertIn("NOMINALNYY_TOK: 16А", first["message"])
        self.assertIn("https://example.org/certificate.pdf", first["message"])
        followup = self.chat("а сертификат?")
        self.assertEqual(followup["products"][0]["id"], "demo-1")
        self.assertIn("certificate.pdf", followup["message"])
        self.assertIn("10", self.chat("сколько осталось?")["message"])
        unknown = self.chat("Есть артикул UNKNOWN-404?")
        self.assertEqual(unknown["products"], [])
        self.assertIn("не найдена", unknown["message"])

    def test_assistant_zero_stock_and_analog(self):
        response = self.chat("Есть DEMO-C16-OLD?")
        self.assertEqual(response["products"][0]["id"], "demo-2")
        self.assertEqual(response["alternatives"][0]["product"]["id"], "demo-1")
        self.assertIn("Совпадают", response["message"])

    def test_assistant_unknown_stock_and_terms(self):
        self.app.state.catalog.demo["demo-1"].quantity = None
        response = self.chat("Есть DEMO-C16?")
        self.assertIn("Остаток: неизвестен", response["message"])
        self.assertEqual(response["alternatives"], [])
        with patch.object(self.app.state.catalog, "terms", return_value=PurchaseTerms(
            payment="Условия оплаты неизвестны.", delivery="Условия доставки неизвестны.",
            minimum_order="Минимальная партия неизвестна.", source="unavailable")):
            terms = self.chat("Какие условия оплаты и доставки?")
        self.assertIn("Условия оплаты неизвестны", terms["message"])
        self.assertIn("Минимальная партия неизвестна", terms["message"])

    def test_assistant_ambiguous_request_and_pending_only(self):
        ambiguous = self.chat("Есть автомат C16?")
        self.assertIn("Уточните артикул", ambiguous["message"])
        self.assertIsNone(ambiguous["pending_action"])
        self.chat("Покажи DEMO-C16")
        proposal = self.chat("добавь две штуки этого")
        self.assertEqual(proposal["pending_action"]["quantity"], "2")
        self.assertEqual(proposal["cart"]["items"], [])
        consent = self.chat("да, добавь")
        self.assertEqual(consent["cart"]["items"], [])
        self.assertEqual(consent["pending_action"]["id"], proposal["pending_action"]["id"])

    def test_errors_and_cors(self):
        response = self.client.post(self.base + "/chat", headers=self.auth, json={"message": "  "})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "validation_error")
        self.assertEqual(self.client.get("/api/v1/products?page=0").status_code, 422)
        self.assertEqual(self.client.get("/api/v1/products?q=%20").status_code, 422)
        self.assertEqual(self.client.get("/api/v1/health").headers["cache-control"], "no-store")
        response = self.client.options(self.base + "/chat", headers={"Origin": "http://localhost:5500",
            "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "authorization,content-type"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["access-control-allow-origin"], "http://localhost:5500")

    def test_frontend_and_docs_are_served(self):
        home = self.client.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertIn('src="home.js"', home.text)
        self.assertIn('href="chat.html">Спросить у ИИ', home.text)
        chat = self.client.get("/chat.html")
        self.assertEqual(chat.status_code, 200)
        self.assertIn('id="back-button"', chat.text)
        self.assertIn('class="product-image"', chat.text)
        self.assertIn('DEMO-DRILL18', chat.text)
        self.assertIn('placeholder="Например: покажи шуруповёрт"', chat.text)
        self.assertEqual(self.client.get("/chat-navigation.js").status_code, 200)
        cart = self.client.get("/cart.html")
        self.assertEqual(cart.status_code, 200)
        self.assertIn('href="/"', cart.text)
        self.assertIn("Вернуться на главную", cart.text)
        self.assertEqual(self.client.get("/docs").status_code, 200)
        self.assertEqual(self.client.get("/openapi.json").status_code, 200)

    def test_concurrent_confirmation(self):
        action = self.proposal().json()["id"]

        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test") as client:
                responses = await asyncio.gather(*[client.post(self.base + f"/cart/proposals/{action}/confirm",
                    headers=self.auth, json={"confirmed": True}) for _ in range(5)])
                self.assertTrue(all(r.status_code == 200 for r in responses))
                self.assertEqual(self.app.state.sessions.sessions[self.sid].items["demo-1"].quantity, Decimal("2"))
        asyncio.run(run())

    def test_sdk_tools_and_history_without_network(self):
        from types import SimpleNamespace
        from agents.tool_context import ToolContext

        async def fake_run(agent, input, **kwargs):
            self.assertTrue(kwargs["run_config"].tracing_disabled)
            self.assertFalse(agent.model_settings.store)
            tools = {tool.name: tool for tool in agent.tools}
            self.assertNotIn("confirm", tools)
            context = ToolContext(context=None, tool_name="search_products", tool_call_id="search", tool_arguments='{}')
            summary = await tools["search_products"].on_invoke_tool(context, '{"query":"DEMO-C16"}')
            self.assertNotIn('"price"', summary)
            self.assertNotIn('"quantity"', summary)
            context = ToolContext(context=None, tool_name="product_details", tool_call_id="test", tool_arguments='{}')
            result = await tools["product_details"].on_invoke_tool(context, '{"product_id":"DEMO-C16"}')
            self.assertIn("DEMO-C16", result)
            context = ToolContext(context=None, tool_name="find_alternatives", tool_call_id="alternative", tool_arguments='{}')
            result = await tools["find_alternatives"].on_invoke_tool(context, '{"product_id":"DEMO-C16-OLD"}')
            self.assertIn("DEMO-C16", result)
            context = ToolContext(context=None, tool_name="propose_cart_addition", tool_call_id="test2", tool_arguments='{}')
            await tools["propose_cart_addition"].on_invoke_tool(context, '{"product_id":"DEMO-C16","quantity":"2"}')
            return SimpleNamespace(final_output="Нужно подтверждение.")

        service = self.app.state.assistant
        service.settings.assistant_mode = "openai"
        # SDK model construction requires a client, but no requests are made.
        from openai import AsyncOpenAI
        service.openai_client = AsyncOpenAI(api_key="test-key")
        with patch("agents.Runner.run", side_effect=fake_run):
            response = self.client.post(self.base + "/chat", headers=self.auth, json={"message": "Добавь 2 штуки"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["products"][0]["id"], "demo-1")
        self.assertEqual(response.json()["cart"]["items"], [])
        asyncio.run(service.openai_client.close())

    def test_attachment_endpoint_and_no_file_retention(self):
        from types import SimpleNamespace
        from openai import AsyncOpenAI
        endpoint = self.base + "/chat/attachment"
        self.assertEqual(self.client.post(endpoint, headers=self.auth, content=b"%PDF-").status_code, 503)
        self.app.state.settings.assistant_mode = "openai"
        self.app.state.assistant.openai_client = AsyncOpenAI(api_key="test-key")

        async def fake_run(agent, input, **kwargs):
            self.assertEqual(input[-1]["content"][1]["type"], "input_file")
            return SimpleNamespace(final_output="Уточните артикул.")

        with patch("agents.Runner.run", side_effect=fake_run):
            response = self.client.post(endpoint, headers={**self.auth, "Content-Type": "application/pdf"}, content=b"%PDF-1.7\ntest")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("file_data", str(self.app.state.sessions.sessions[self.sid].history))
        self.assertEqual(self.client.post(endpoint, headers={**self.auth, "Content-Type": "application/pdf"}, content=b"bad").status_code, 422)
        asyncio.run(self.app.state.assistant.openai_client.close())

    def test_failed_chat_rolls_back_proposal_and_history(self):
        session = self.app.state.sessions.sessions[self.sid]
        action = self.proposal().json()["id"]

        async def failure(session, message):
            session.pending = None
            raise AppError(502, "upstream_error", "Test failure")

        with patch.object(self.app.state.assistant, "_demo", side_effect=failure):
            response = self.client.post(self.base + "/chat", headers=self.auth, json={"message": "test"})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(session.pending.id, action)
        self.assertEqual(session.history, [])

    def test_rate_and_session_capacity_limits(self):
        self.app.state.sessions.capacity = 1
        self.assertEqual(self.client.post("/api/v1/sessions").status_code, 503)
        for _ in range(10):
            self.assertEqual(self.client.post(self.base + "/chat", headers=self.auth, json={"message": "test"}).status_code, 200)
        self.assertEqual(self.client.post(self.base + "/chat", headers=self.auth, json={"message": "test"}).status_code, 429)


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_contract_and_unknown_stock(self):
        source = {"id": 515291, "article": "200300285_", "name": "Legrand", "price": 64920,
                  "properties": {"KRATNOST_MIN": "1"}}
        self.assertEqual(normalize_product(source).availability, "unknown")
        source.update(quantity=23, stores=[{"id": 13, "name": "Алматы", "quantity": 5}])
        async with httpx.AsyncClient(base_url="https://ekt.kz", transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=source))) as client:
            product = await EktClient(client).detail("515291")
            self.assertEqual(product.quantity, 23)
            self.assertEqual(product.stores[0].quantity, 5)

    async def test_upstream_errors(self):
        for status, body, expected in [(401, {}, 502), (500, {}, 502), (404, {}, 404), (200, {}, 502)]:
            async with httpx.AsyncClient(base_url="https://ekt.kz", transport=httpx.MockTransport(
                    lambda r: httpx.Response(status, json=body))) as client:
                with self.assertRaises(AppError) as error:
                    await EktClient(client).detail("1")
                self.assertEqual(error.exception.status, expected)

    async def test_timeout(self):
        def timeout(request):
            raise httpx.ReadTimeout("private upstream details")
        async with httpx.AsyncClient(base_url="https://ekt.kz", transport=httpx.MockTransport(timeout)) as client:
            with self.assertRaises(AppError) as error:
                await EktClient(client).detail("1")
            self.assertEqual(error.exception.status, 504)


if __name__ == "__main__":
    unittest.main()
