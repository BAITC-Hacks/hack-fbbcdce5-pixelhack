import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.api.routes import router
from app.core.config import Settings
from app.core.errors import AppError
from app.integrations.ekt import EktClient
from app.services.assistant import AssistantService
from app.services.cart import CartService
from app.services.catalog import CatalogService
from app.services.sessions import SessionStore

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with httpx.AsyncClient(
            base_url=settings.ekt_base_url,
            auth=httpx.BasicAuth(settings.ekt_username, settings.ekt_password.get_secret_value()),
            timeout=settings.request_timeout_seconds,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            follow_redirects=False,
        ) as http:
            openai_client = None
            if settings.assistant_mode == "openai":
                from openai import AsyncOpenAI
                openai_client = AsyncOpenAI(api_key=settings.openai_api_key.get_secret_value(),
                                            timeout=settings.chat_timeout_seconds, max_retries=0)
            app.state.settings = settings
            app.state.catalog = CatalogService(EktClient(http) if settings.catalog_mode == "live" else None,
                                               settings.ekt_search_pages)
            app.state.sessions = SessionStore(settings.session_ttl_seconds, settings.max_sessions)
            app.state.cart = CartService(app.state.catalog)
            app.state.assistant = AssistantService(settings, app.state.catalog, app.state.cart, openai_client)
            try:
                yield
            finally:
                app.state.sessions.sessions.clear()
                if openai_client:
                    await openai_client.close()

    app = FastAPI(title="EKT AI Assistant API", version="0.1.0", lifespan=lifespan,
                  description="API прототипа HackAlem AI. Синтетический каталог по умолчанию; корзина локальная.")
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins,
                       allow_methods=["GET", "POST", "DELETE"],
                       allow_headers=["Authorization", "Content-Type"])

    @app.middleware("http")
    async def no_store(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def error(status: int, code: str, message: str):
        return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}},
                            headers={"Cache-Control": "no-store"})

    @app.exception_handler(AppError)
    async def app_error(request: Request, exc: AppError):
        return error(exc.status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        # Never echo user input (potential secrets or payment data) in error responses.
        fields = ", ".join(".".join(str(x) for x in e["loc"]) for e in exc.errors())
        return error(422, "validation_error", f"Проверьте поля запроса: {fields}.")

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return error(exc.status_code, "http_error", str(exc.detail))

    @app.exception_handler(TimeoutError)
    async def timeout_error(request: Request, exc: TimeoutError):
        return error(504, "assistant_timeout", "Превышено время ожидания ответа ассистента.")

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        logger.error("Request failed: %s", type(exc).__name__)
        return error(502, "service_unavailable", "Сервис временно недоступен. Попробуйте позже.")

    app.include_router(router)
    return app


app = create_app()
