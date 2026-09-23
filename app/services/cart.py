from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from app.core.errors import AppError
from app.schemas import Cart, CartItem, CartProposal, PendingAction, Product
from app.services.catalog import CatalogService
from app.services.sessions import Session, now


class CartService:
    def __init__(self, catalog: CatalogService):
        self.catalog = catalog

    def view(self, session: Session) -> Cart:
        return Cart(items=list(session.items.values()),
                    total=sum((item.quantity * item.unit_price for item in session.items.values()), Decimal(0)),
                    cart_url=f"/api/v1/sessions/{session.id}/cart")

    def validate(self, product: Product, quantity: Decimal):
        if product.price is None or product.quantity is None:
            raise AppError(409, "unknown_price_or_stock", "Цена или остаток не подтверждены.")
        if quantity > product.quantity:
            raise AppError(409, "insufficient_stock", f"Доступно только {product.quantity}.")
        if product.min_quantity and quantity < product.min_quantity:
            raise AppError(409, "minimum_quantity", f"Минимальное количество: {product.min_quantity}.")
        if product.quantity_step and quantity % product.quantity_step:
            raise AppError(409, "invalid_quantity_step", f"Количество должно быть кратно {product.quantity_step}.")

    async def propose(self, session: Session, request: CartProposal) -> PendingAction:
        product = await self.catalog.detail(request.product_id)
        existing = session.items.get(product.id)
        total = request.quantity + (existing.quantity if existing else 0)
        self.validate(product, request.quantity)
        self.validate(product, total)
        proposal = PendingAction(id=str(uuid4()), product=product, quantity=request.quantity,
                                 expires_at=now() + timedelta(minutes=10))
        session.pending = proposal
        return proposal

    async def confirm(self, session: Session, action_id: str) -> Cart:
        # The caller holds the session lock across read/check/write.
        if action_id in session.confirmed:
            return self.view(session)
        pending = session.pending
        if pending is None or pending.id != action_id:
            raise AppError(404, "action_not_found", "Предложение не найдено или заменено новым.")
        if pending.expires_at <= now():
            session.pending = None
            raise AppError(409, "action_expired", "Срок подтверждения истёк. Создайте предложение заново.")
        product = await self.catalog.detail(pending.product.id)
        if product.price != pending.product.price:
            raise AppError(409, "price_changed", "Цена изменилась. Создайте новое предложение и подтвердите новую цену.")
        existing = session.items.get(product.id)
        quantity = pending.quantity + (existing.quantity if existing else 0)
        self.validate(product, pending.quantity)
        self.validate(product, quantity)
        if len(session.confirmed) >= 100:
            raise AppError(409, "cart_action_limit", "Достигнут лимит изменений корзины за сессию.")
        session.items[product.id] = CartItem(product_id=product.id, article=product.article,
                                           name=product.name, quantity=quantity, unit_price=product.price)
        session.confirmed.add(action_id)
        session.pending = None
        return self.view(session)
