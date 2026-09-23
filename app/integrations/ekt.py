"""Adapter for the two read-only endpoints supplied by the partner.

Do not infer stock from list responses or treat RECOMMEND as equivalent products.
"""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

import httpx
from pydantic import ValidationError

from app.core.errors import AppError
from app.schemas import Product, Warehouse


def normalize_product(data: dict) -> Product:
    properties = data.get("properties") or {}
    if not isinstance(properties, dict):
        raise ValueError("Invalid properties")
    properties = {str(k): [str(x) for x in v] if isinstance(v, list) else str(v)
                  for k, v in properties.items() if v is not None}
    # Observed KRATNOST_MIN is a pack multiple. Preserve unknown values as null.
    multiple = properties.get("KRATNOST_MIN")
    step = Decimal(multiple.replace(",", ".")) if isinstance(multiple, str) and multiple else None
    if step is not None and step <= 0:
        step = None
    url = data.get("url")
    category = urlparse(url).path.rsplit("/", 2)[0] if url else None
    return Product(
        id=str(data["id"]), article=data["article"], name=data["name"],
        description=data.get("description") or "", category=category,
        price=data.get("price"), quantity=data.get("quantity"),
        min_quantity=step, quantity_step=step,
        stores=[Warehouse(id=str(s["id"]), name=s["name"], quantity=s["quantity"])
                for s in data.get("stores", [])],
        properties=properties,
        # No certificate field was present in the observed partner response.
        certificates=data.get("certificates") or [],
        url=url, image=data.get("image"), source="ekt", fetched_at=datetime.now(timezone.utc),
    )


class EktClient:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def _get(self, path: str, params: dict) -> dict:
        try:
            response = await self.client.get(path, params=params)
            if response.status_code == 404:
                raise AppError(404, "product_not_found", "Товар не найден в EKT.")
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("Expected an object")
            return data
        except httpx.TimeoutException:
            raise AppError(504, "ekt_timeout", "EKT не ответил вовремя.") from None
        except (httpx.HTTPError, ValueError):
            raise AppError(502, "ekt_unavailable", "Не удалось получить корректный ответ EKT.") from None

    async def page(self, page: int) -> tuple[list[Product], bool]:
        data = await self._get("/api/products", {"page": page})
        try:
            items = [normalize_product(p) for p in data["items"]]
            return items, len(items) >= int(data["per_page"])
        except (KeyError, TypeError, ValueError, InvalidOperation, ValidationError):
            raise AppError(502, "ekt_schema_error", "Формат каталога EKT изменился.") from None

    async def detail(self, product_id: str) -> Product:
        data = await self._get("/api/products/detail", {"id": product_id})
        try:
            product = normalize_product(data)
            if product.id != product_id:
                raise ValueError("Product identity mismatch")
            return product
        except (KeyError, TypeError, ValueError, InvalidOperation, ValidationError):
            raise AppError(502, "ekt_schema_error", "Формат карточки EKT изменился.") from None
