from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, computed_field, field_validator

Quantity = Annotated[Decimal, Field(gt=0, le=1000000, max_digits=12, decimal_places=3)]
Money = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Warehouse(BaseModel):
    id: str
    name: str
    quantity: Decimal = Field(ge=0, allow_inf_nan=False)


class Product(BaseModel):
    id: str
    article: str
    name: str
    description: str = ""
    category: str | None = None
    price: Money | None = None
    currency: Literal["KZT"] = "KZT"
    quantity: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    min_quantity: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)
    quantity_step: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)
    stores: list[Warehouse] = Field(default_factory=list)
    properties: dict[str, str | list[str]] = Field(default_factory=dict)
    certificates: list[HttpUrl] = Field(default_factory=list)
    url: HttpUrl | None = None
    image: str | None = None
    source: Literal["demo", "ekt"]
    fetched_at: datetime

    @field_validator("image")
    @classmethod
    def validate_image(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value.startswith("/") and not value.startswith("//") and ".." not in value:
            return value
        return str(HttpUrl(value))

    @computed_field
    @property
    def availability(self) -> Literal["unknown", "in_stock", "out_of_stock"]:
        if self.quantity is None:
            return "unknown"
        return "in_stock" if self.quantity > 0 else "out_of_stock"


class ProductPage(BaseModel):
    items: list[Product]
    page: int
    has_more: bool
    search_scope: str


class Alternative(BaseModel):
    product: Product
    reason: str


class Alternatives(BaseModel):
    items: list[Alternative]
    search_scope: str


class PurchaseTerms(BaseModel):
    payment: str
    delivery: str
    minimum_order: str
    source: Literal["demo", "unavailable"]


class SessionCreated(BaseModel):
    session_id: str
    access_token: str
    expires_at: datetime


class CartItem(BaseModel):
    product_id: str
    article: str
    name: str
    quantity: Quantity
    unit_price: Money


class Cart(BaseModel):
    items: list[CartItem]
    total: Money
    currency: Literal["KZT"] = "KZT"
    cart_url: str
    mode: Literal["local"] = "local"
    notice: str = "Локальная корзина прототипа; заказ в ekt.kz не создан, остатки не зарезервированы."


class CartProposal(InputModel):
    product_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    quantity: Quantity


class PendingAction(BaseModel):
    id: str
    product: Product
    quantity: Quantity
    expires_at: datetime
    confirmation_required: Literal[True] = True


class ConfirmAction(InputModel):
    confirmed: Literal[True]


class ChatRequest(InputModel):
    message: str = Field(min_length=1, max_length=4000)


class ChatResponse(BaseModel):
    message: str
    products: list[Product] = Field(default_factory=list)
    alternatives: list[Alternative] = Field(default_factory=list)
    pending_action: PendingAction | None = None
    cart: Cart
    assistant_mode: Literal["demo", "openai"]
