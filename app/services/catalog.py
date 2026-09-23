import asyncio
import re
from time import monotonic

from app.core.errors import AppError
from app.integrations.ekt import EktClient
from app.schemas import Alternative, Alternatives, Product, ProductPage, PurchaseTerms


STOP_WORDS = {"найди", "найти", "покажи", "подбери", "мне", "пожалуйста", "хочу",
              "купить", "товар", "товары", "самый", "самую", "самое", "самые",
              "дешевый", "дешевая", "дешевые", "дешево", "недорогой", "недорогая",
              "бюджетный", "бюджетная", "цена", "цены", "какой", "какая", "какие",
              "есть", "наличии", "для", "под"}
PRICE_WORDS = ("дешев", "дешёв", "недорог", "бюджет", "минимальн")
ENDINGS = ("ыми", "ими", "ого", "ему", "ому", "ами", "ями", "ый", "ий", "ой",
           "ая", "яя", "ое", "ее", "ые", "ие", "ым", "им", "ом", "ем", "ую",
           "юю", "ых", "их", "ов", "ев", "а", "у", "ы", "и", "е", "я", "ю")


def search_terms(query: str) -> list[str]:
    words = re.findall(r"[\w-]+", query.casefold().replace("ё", "е"))
    terms = []
    for word in words:
        if len(word) < 3 or word in STOP_WORDS or any(word.startswith(part) for part in PRICE_WORDS):
            continue
        for ending in ENDINGS:
            if len(word) > len(ending) + 3 and word.endswith(ending):
                word = word[:-len(ending)]
                break
        terms.append(word)
    return terms


class CatalogService:
    def __init__(self, ekt: EktClient, search_pages: int):
        self.ekt = ekt
        self.search_pages = search_pages
        self._cache: dict[int, tuple[float, list[Product], bool]] = {}
        self._index: tuple[float, list[Product], bool] | None = None
        self._index_lock = asyncio.Lock()

    async def _page(self, page: int) -> tuple[list[Product], bool]:
        cached = self._cache.get(page)
        if cached and monotonic() - cached[0] < 60:
            return cached[1], cached[2]
        products, more = await self.ekt.page(page)
        if len(self._cache) >= 100:
            self._cache.clear()
        self._cache[page] = (monotonic(), products, more)
        return products, more

    async def _index_products(self) -> tuple[list[Product], bool]:
        if self._index and monotonic() - self._index[0] < 600:
            return self._index[1], self._index[2]
        async with self._index_lock:
            if self._index and monotonic() - self._index[0] < 600:
                return self._index[1], self._index[2]
            first, more = await self._page(1)
            products = list(first)
            seen = {product.id for product in first}
            first_ids = tuple(product.id for product in first)
            complete = not more
            # EKT ignores text search parameters and repeats page 1 after its last page.
            for start in range(2, self.search_pages + 1, 20):
                page_numbers = range(start, min(start + 20, self.search_pages + 1))
                batches = await asyncio.gather(*(self._page(number) for number in page_numbers))
                for batch, has_more in batches:
                    if not batch or tuple(item.id for item in batch) == first_ids:
                        complete = True
                        break
                    products.extend(item for item in batch if item.id not in seen)
                    seen.update(item.id for item in batch)
                    if not has_more:
                        complete = True
                        break
                if complete:
                    break
            self._index = (monotonic(), products, complete)
            return products, complete

    async def list(self, page: int, query: str | None = None,
                   sort: str = "relevance") -> ProductPage:
        if not query:
            products, more = await self._page(page)
            return ProductPage(items=products, page=page, has_more=more, search_scope="page")
        # EKT product IDs can be resolved without scanning only the first catalog pages.
        if query.isdecimal():
            try:
                product = await self.detail(query)
            except AppError as exc:
                if exc.status != 404:
                    raise
            else:
                return ProductPage(items=[product] if page == 1 else [], page=page,
                                   has_more=False, search_scope="exact_id")
        terms = search_terms(query)
        if not terms:
            return ProductPage(items=[], page=page, has_more=False,
                               search_scope="query_needs_product_name")
        products, complete = await self._index_products()
        found = []
        for product in products:
            text = f"{product.id} {product.article} {product.name} {product.category or ''}".casefold().replace("ё", "е")
            if all(term in text for term in terms):
                found.append(product)
        if sort == "price_asc" or any(word in query.casefold() for word in PRICE_WORDS):
            found.sort(key=lambda item: (item.price is None, item.price or 0, item.name))
        else:
            found.sort(key=lambda item: (item.article.casefold() != query.casefold(),
                                         len(item.name), item.name))
        start = (page - 1) * 20
        return ProductPage(items=found[start:start + 20], page=page,
                           has_more=len(found) > start + 20,
                           search_scope="complete_catalog" if complete else f"first_{self.search_pages}_pages")

    async def detail(self, product_id: str) -> Product:
        return await self.ekt.detail(product_id)

    async def alternatives(self, product_id: str) -> Alternatives:
        original = await self.detail(product_id)
        # Conservative matching: only compare supported electrical protection attributes.
        keys = ("KOLICHESTVO_POLYUSOV", "NOMINALNOE_NAPRYAZHENIE", "NOMINALNYY_TOK",
                "NOMINALNAYA_OTKLYUCHAYUSHCHAYA_SPOSOBNOST")
        if not original.category or not all(original.properties.get(k) for k in keys):
            return Alternatives(items=[], search_scope="Недостаточно структурированных характеристик для подбора.")
        products, complete = await self._index_products()
        candidates = [p for p in products if p.id != product_id and p.category == original.category]
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
        scope = "Весь каталог" if complete else f"Первые {self.search_pages} страниц"
        return Alternatives(items=result, search_scope=f"{scope}; до 10 карточек той же категории.")

    def terms(self) -> PurchaseTerms:
        return PurchaseTerms(payment="Подтверждённые условия оплаты не предоставлены партнёром; уточните у менеджера ekt.kz.",
            delivery="Стоимость, сроки и регионы доставки уточняйте у менеджера ekt.kz.",
            minimum_order="Кратность KRATNOST_MIN возвращается в карточке товара; общие условия минимального заказа неизвестны.",
            source="unavailable")
