import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic

from app.core.errors import AppError
from app.integrations.ekt import EktClient
from app.schemas import Alternative, Alternatives, Product, ProductPage, PurchaseTerms


class CatalogService:
    def __init__(self, ekt: EktClient | None, search_pages: int):
        self.ekt = ekt
        self.search_pages = search_pages
        self._cache: dict[int, tuple[float, list[Product], bool]] = {}
        self._cache_lock = asyncio.Lock()
        self.demo: dict[str, Product] = {}
        if ekt is None:
            path = Path(__file__).resolve().parents[1] / "data" / "products.json"
            self.demo = {p["id"]: Product(**p, source="demo", fetched_at=datetime.now(timezone.utc))
                         for p in json.loads(path.read_text(encoding="utf-8"))}

    async def _page(self, page: int) -> tuple[list[Product], bool]:
        if self.ekt is None:
            return (list(self.demo.values()), False) if page == 1 else ([], False)
        async with self._cache_lock:
            cached = self._cache.get(page)
            if cached and monotonic() - cached[0] < 60:
                return cached[1], cached[2]
            products, more = await self.ekt.page(page)
            if len(self._cache) >= 100:
                self._cache.clear()
            self._cache[page] = (monotonic(), products, more)
            return products, more

    async def list(self, page: int, query: str | None = None) -> ProductPage:
        if not query:
            products, more = await self._page(page)
            return ProductPage(items=products, page=page, has_more=more, search_scope="page")
        # EKT product IDs can be resolved without scanning only the first catalog pages.
        if self.ekt and query.isdecimal():
            try:
                product = await self.detail(query)
            except AppError as exc:
                if exc.status != 404:
                    raise
            else:
                return ProductPage(items=[product] if page == 1 else [], page=page,
                                   has_more=False, search_scope="exact_id")
        found = []
        more = False
        for number in range(1, self.search_pages + 1):
            products, more = await self._page(number)
            terms = query.casefold().split()
            found.extend(p for p in products if all(t in f"{p.id} {p.article} {p.name}".casefold() for t in terms))
            if not more:
                break
        start = (page - 1) * 20
        return ProductPage(items=found[start:start + 20], page=page,
                           has_more=len(found) > start + 20,
                           search_scope=f"first_{number}_pages" if more else "complete_catalog")

    async def detail(self, product_id: str) -> Product:
        if self.ekt:
            return await self.ekt.detail(product_id)
        if product_id not in self.demo:
            raise AppError(404, "product_not_found", "Товар не найден.")
        return self.demo[product_id].model_copy(update={"fetched_at": datetime.now(timezone.utc)})

    async def alternatives(self, product_id: str) -> Alternatives:
        original = await self.detail(product_id)
        # Conservative matching: only compare supported electrical protection attributes.
        keys = ("KOLICHESTVO_POLYUSOV", "NOMINALNOE_NAPRYAZHENIE", "NOMINALNYY_TOK",
                "NOMINALNAYA_OTKLYUCHAYUSHCHAYA_SPOSOBNOST")
        if not original.category or not all(original.properties.get(k) for k in keys):
            return Alternatives(items=[], search_scope="Недостаточно структурированных характеристик для подбора.")
        candidates = []
        more = False
        for page in range(1, self.search_pages + 1):
            products, more = await self._page(page)
            candidates.extend(p for p in products if p.id != product_id and p.category == original.category)
            if not more:
                break
        result = []
        # Bound detail calls; this is deliberately not advertised as a full-catalog search.
        for candidate in candidates[:10]:
            candidate = await self.detail(candidate.id)
            if candidate.quantity and all(candidate.properties.get(k) == original.properties[k] for k in keys):
                result.append(Alternative(product=candidate,
                    reason="Совпадают категория, полюса, напряжение, ток и отключающая способность. "
                           "Проверьте монтажные размеры и применимость перед заменой."))
            if len(result) == 3:
                break
        return Alternatives(items=result, search_scope=f"Первые {page} страниц; до 10 карточек той же категории.")

    def terms(self) -> PurchaseTerms:
        if self.ekt:
            return PurchaseTerms(payment="Подтверждённые условия оплаты не предоставлены партнёром; уточните у менеджера ekt.kz.",
                delivery="Стоимость, сроки и регионы доставки уточняйте у менеджера ekt.kz.",
                minimum_order="Кратность KRATNOST_MIN возвращается в карточке товара; общие условия минимального заказа неизвестны.",
                source="unavailable")
        return PurchaseTerms(payment="ДЕМО: безналичная оплата по счёту; реальные платежи не принимаются.",
            delivery="ДЕМО: самовывоз со склада, доставка согласуется с менеджером.",
            minimum_order="ДЕМО: минимум и кратность указаны в карточке товара.", source="demo")
