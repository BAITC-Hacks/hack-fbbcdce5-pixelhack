import asyncio
import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.core.errors import AppError
from app.schemas import CartItem, PendingAction, SessionCreated


def now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Session:
    id: str
    token_hash: str
    expires_at: datetime
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    history: list[dict] = field(default_factory=list)
    items: dict[str, CartItem] = field(default_factory=dict)
    pending: PendingAction | None = None
    confirmed: set[str] = field(default_factory=set)
    chat_calls: list[float] = field(default_factory=list)


class SessionStore:
    """Single-process, bounded, ephemeral storage. No user text or tokens on disk."""

    def __init__(self, ttl_seconds: int, capacity: int):
        self.ttl_seconds = ttl_seconds
        self.capacity = capacity
        self.sessions: dict[str, Session] = {}

    def create(self) -> SessionCreated:
        self.sessions = {key: s for key, s in self.sessions.items() if s.expires_at > now() or s.lock.locked()}
        if len(self.sessions) >= self.capacity:
            raise AppError(503, "session_capacity", "Лимит сессий достигнут. Попробуйте позже.")
        token = secrets.token_urlsafe(32)
        session = Session(str(uuid4()), hashlib.sha256(token.encode()).hexdigest(),
                          now() + timedelta(seconds=self.ttl_seconds))
        self.sessions[session.id] = session
        return SessionCreated(session_id=session.id, access_token=token, expires_at=session.expires_at)

    def get(self, session_id: str, token: str) -> Session:
        session = self.sessions.get(session_id)
        digest = hashlib.sha256(token.encode()).hexdigest()
        if not session or not secrets.compare_digest(session.token_hash, digest):
            raise AppError(401, "invalid_session", "Недействительная сессия или токен.")
        if session.expires_at <= now():
            if not session.lock.locked():
                self.sessions.pop(session_id, None)
            raise AppError(401, "session_expired", "Сессия истекла. Создайте новую.")
        return session
