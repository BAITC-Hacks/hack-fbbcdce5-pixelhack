import asyncio
import os
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.errors import AppError
from app.integrations.ekt import EktClient, normalize_product
from app.services.catalog import CatalogService, search_terms

with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key",
                             "EKT_USERNAME": "test-user", "EKT_PASSWORD": "test-password"}):
    from app.main import create_app


def product(product_id, name, price, quantity, article=None, properties=None):
    return normalize_product({
        "id": product_id, "article": article or f"ART-{product_id}", "name": name,
        "price": price, "quantity": quantity, "properties": properties or {},
    })


class FakeEkt:
    def __init__(self):
        self.products = {
            "101": product(101, "Вилка прямая с прямым вводом", "3200", 12),
            "102": product(102, "Вилка с угловым вводом", "2900", 5),
            "201": product(201, "Шуруповёрт сетевой недорогой", "15500", 3),
            "202": product(202, "Шуруповёрт аккумуляторный", "30000", 4),
        }
        self.page_calls = []

    async def page(self, page):
        self.page_calls.append(page)
        return (list(self.products.values()), False) if page == 1 else ([], False)

    async def detail(self, product_id):
        if product_id not in self.products:
            raise AppError(404, "product_not_found", "Товар не найден в EKT.")
        return self.products[product_id].model_copy(deep=True)


class ApiTests(unittest.TestCase):
    def setUp(self):
        settings = Settings(_env_file=None, openai_api_key="test-key",
                            ekt_username="test-user", ekt_password="test-password",
                            ekt_search_pages=3)
        self.app = create_app(settings)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.ekt = FakeEkt()
        self.app.state.catalog.ekt = self.ekt
        created = self.client.post("/api/v1/sessions").json()
        self.sid = created["session_id"]
        self.base = f"/api/v1/sessions/{self.sid}"
        self.auth = {"Authorization": f"Bearer {created['access_token']}"}

    def tearDown(self):
        self.client.__exit__(None, None, None)

    def test_catalog_search_and_live_only_interface(self):
        health = self.client.get("/api/v1/health").json()
        self.assertEqual((health["catalog_mode"], health["assistant_mode"]), ("live", "openai"))
        exact = self.client.get("/api/v1/products?q=201").json()
        self.assertEqual(exact["items"][0]["id"], "201")
        self.assertEqual(exact["search_scope"], "exact_id")
        plug = self.client.get("/api/v1/products?q=вилка+с+прямым+вводом").json()
        self.assertEqual([item["id"] for item in plug["items"]], ["101"])
        drills = self.client.get("/api/v1/products?q=дешевый+шуруповерт").json()
        self.assertEqual([item["id"] for item in drills["items"]], ["201", "202"])
        self.assertEqual(self.client.get("/api/v1/products/201").json()["source"], "ekt")
        self.assertIn("Найди недорогой шуруповёрт", self.client.get("/chat.html").text)

    def test_cart_confirmation_rechecks_live_price_and_stock(self):
        proposal = self.client.post(self.base + "/cart/proposals", headers=self.auth,
                                    json={"product_id": "201", "quantity": "2"})
        self.assertEqual(proposal.status_code, 201, proposal.text)
        action = proposal.json()["id"]
        self.assertEqual(self.client.get(self.base + "/cart", headers=self.auth).json()["items"], [])
        self.assertEqual(self.client.post(self.base + f"/cart/proposals/{action}/confirm",
                                          headers=self.auth, json={"confirmed": False}).status_code, 422)
        self.ekt.products["201"].price += 1
        self.assertEqual(self.client.post(self.base + f"/cart/proposals/{action}/confirm",
                                          headers=self.auth, json={"confirmed": True}).json()["error"]["code"],
                         "price_changed")
        self.ekt.products["201"].price -= 1
        confirmed = self.client.post(self.base + f"/cart/proposals/{action}/confirm",
                                     headers=self.auth, json={"confirmed": True}).json()
        self.assertEqual(confirmed["items"][0]["quantity"], "2")
        self.assertEqual(self.client.post(self.base + f"/cart/proposals/{action}/confirm",
                                          headers=self.auth, json={"confirmed": True}).json()["items"][0]["quantity"], "2")
        self.assertEqual(self.client.get(self.base + "/cart").status_code, 401)
        self.assertEqual(self.client.post(self.base + "/cart/proposals", headers=self.auth,
                                          json={"product_id": "201", "quantity": "2"}).status_code, 409)

    def test_ai_tools_use_catalog_and_chat_keeps_offtopic_instructions(self):
        from agents import Runner
        from agents.tool_context import ToolContext

        async def fake_run(agent, input, **kwargs):
            self.assertIn("На вопросы вне этой темы", agent.instructions)
            tools = {tool.name: tool for tool in agent.tools}
            context = ToolContext(context=None, tool_name="search_products",
                                  tool_call_id="search", tool_arguments='{}')
            result = await tools["search_products"].on_invoke_tool(
                context, '{"query":"недорогой шуруповерт","sort":"price_asc"}')
            self.assertIn('"id": "201"', result)
            context = ToolContext(context=None, tool_name="product_details",
                                  tool_call_id="detail", tool_arguments='{}')
            detail = await tools["product_details"].on_invoke_tool(
                context, '{"product_id":"201"}')
            self.assertIn('"price":"15500"', detail)
            return SimpleNamespace(final_output="По каталогу EKT есть шуруповёрт ART-201.")

        with patch.object(Runner, "run", side_effect=fake_run):
            response = self.client.post(self.base + "/chat", headers=self.auth,
                                        json={"message": "Найди недорогой шуруповёрт"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["products"][0]["id"], "201")
        self.assertNotIn("assistant_mode", response.json())

    def test_session_and_validation(self):
        self.assertEqual(self.client.get(self.base + "/cart").status_code, 401)
        self.assertEqual(self.client.post(self.base + "/cart/proposals", headers=self.auth,
                                          json={"product_id": "201", "quantity": "0"}).status_code, 422)
        session = self.app.state.sessions.sessions[self.sid]
        session.expires_at -= timedelta(hours=2)
        self.assertEqual(self.client.get(self.base + "/cart", headers=self.auth).status_code, 401)


class SearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_reads_multiple_pages_and_stops_at_repeated_first_page(self):
        class PagedEkt:
            def __init__(self):
                self.calls = []

            async def page(self, number):
                self.calls.append(number)
                if number == 1:
                    return [product(1, "Кабель", "100", 1)], True
                if number == 2:
                    return [product(2, "Вилка прямой ввод", "400", 1)], True
                return [product(1, "Кабель", "100", 1)], True

            async def detail(self, product_id):
                return product(int(product_id), "Кабель", "100", 1)

        catalog = CatalogService(PagedEkt(), 5)
        result = await catalog.list(1, "вилка с прямым вводом")
        self.assertEqual([item.id for item in result.items], ["2"])
        self.assertEqual(result.search_scope, "complete_catalog")
        self.assertIn("прям", search_terms("вилка с прямым вводом"))
        self.assertEqual(search_terms("вилку с прямым вводом"),
                         search_terms("вилка с прямым вводом"))

    async def test_adapter_errors(self):
        for status, expected in ((404, 404), (500, 502)):
            async with httpx.AsyncClient(base_url="https://ekt.kz", transport=httpx.MockTransport(
                    lambda request: httpx.Response(status, json={}))) as client:
                with self.assertRaises(AppError) as error:
                    await EktClient(client).detail("1")
                self.assertEqual(error.exception.status, expected)


if __name__ == "__main__":
    unittest.main()
