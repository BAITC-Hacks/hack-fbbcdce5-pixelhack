from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.errors import AppError
from app.schemas import (Alternatives, Cart, CartProposal, ChatRequest, ChatResponse,
                         ConfirmAction, PendingAction, Product, ProductPage, PurchaseTerms, SessionCreated)
from app.services.sessions import Session
from app.services.attachments import MAX_FILE_BYTES, SUPPORTED_TYPES, content_block

router = APIRouter(prefix="/api/v1")
bearer = HTTPBearer(auto_error=False)
ProductId = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]


async def authorized_session(request: Request, session_id: str,
                             credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
    if credentials is None:
        raise AppError(401, "missing_token", "Передайте токен сессии в Authorization: Bearer.")
    store = request.app.state.sessions
    session = store.get(session_id, credentials.credentials)
    async with session.lock:
        # Recheck after waiting: deletion/expiry may have happened while queued.
        store.get(session_id, credentials.credentials)
        yield session


AuthorizedSession = Annotated[Session, Depends(authorized_session)]


@router.get("/health", tags=["system"])
async def health(request: Request):
    settings = request.app.state.settings
    return {"status": "ok", "catalog_mode": settings.catalog_mode,
            "assistant_mode": settings.assistant_mode, "cart_mode": "local"}


@router.get("/products", response_model=ProductPage, tags=["catalog"])
async def products(request: Request, page: Annotated[int, Query(ge=1, le=100000)] = 1,
                   q: Annotated[str | None, Query(min_length=1, max_length=200)] = None):
    if q is not None and not q.strip():
        raise AppError(422, "invalid_query", "Поисковый запрос не может быть пустым.")
    return await request.app.state.catalog.list(page, q)


@router.get("/products/{product_id}", response_model=Product, tags=["catalog"])
async def product(request: Request, product_id: ProductId):
    return await request.app.state.catalog.detail(product_id)


@router.get("/products/{product_id}/alternatives", response_model=Alternatives, tags=["catalog"])
async def alternatives(request: Request, product_id: ProductId):
    return await request.app.state.catalog.alternatives(product_id)


@router.get("/purchase-terms", response_model=PurchaseTerms, tags=["catalog"])
async def purchase_terms(request: Request):
    return request.app.state.catalog.terms()


@router.post("/sessions", response_model=SessionCreated, status_code=201, tags=["sessions"])
async def create_session(request: Request):
    return request.app.state.sessions.create()


@router.delete("/sessions/{session_id}", status_code=204, tags=["sessions"])
async def delete_session(request: Request, session: AuthorizedSession):
    request.app.state.sessions.sessions.pop(session.id, None)
    return Response(status_code=204)


@router.post("/sessions/{session_id}/chat", response_model=ChatResponse, tags=["chat"])
async def chat(request: Request, body: ChatRequest, session: AuthorizedSession):
    return await request.app.state.assistant.chat(session, body.message)


@router.post("/sessions/{session_id}/chat/attachment", response_model=ChatResponse, tags=["chat"],
             openapi_extra={"requestBody": {"required": True, "content": {
                 media: {"schema": {"type": "string", "format": "binary"}} for media in SUPPORTED_TYPES}}})
async def attachment_chat(request: Request, session: AuthorizedSession):
    """Send raw file bytes with their Content-Type; uses previous chat context.

    Files are not persisted. This endpoint requires ASSISTANT_MODE=openai.
    """
    if request.app.state.settings.assistant_mode != "openai":
        raise AppError(503, "ai_required", "Для анализа вложений включите ASSISTANT_MODE=openai.")
    media_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    if media_type not in SUPPORTED_TYPES:
        raise AppError(415, "unsupported_attachment", "Поддерживаются PDF, JPEG, DOCX и XLSX.")
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > MAX_FILE_BYTES:
            raise AppError(413, "attachment_too_large", "Максимальный размер файла — 5 МиБ.")
        data.extend(chunk)
    block = content_block(bytes(data), media_type)
    return await request.app.state.assistant.chat(session,
        "Определи товары во вложении с учётом предыдущего запроса. Проверь данные в каталоге. "
        "Если распознавание неоднозначно, уточни артикул. Не добавляй товары без подтверждения.", block)


@router.get("/sessions/{session_id}/cart", response_model=Cart, tags=["cart"])
async def cart(request: Request, session: AuthorizedSession):
    return request.app.state.cart.view(session)


@router.post("/sessions/{session_id}/cart/proposals", response_model=PendingAction, status_code=201, tags=["cart"])
async def propose(request: Request, body: CartProposal, session: AuthorizedSession):
    return await request.app.state.cart.propose(session, body)


@router.post("/sessions/{session_id}/cart/proposals/{action_id}/confirm", response_model=Cart, tags=["cart"])
async def confirm(request: Request, action_id: str, body: ConfirmAction, session: AuthorizedSession):
    return await request.app.state.cart.confirm(session, action_id)
