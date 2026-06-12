import time
from dataclasses import dataclass, field
from typing import Optional

from loguru import logger


def _format_timestamp(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts))


@dataclass
class ChatMessage:
    role: str
    content: str
    timestamp: float = field(default_factory=time.time)


@dataclass
class ChatSession:
    job_id: str
    messages: list[ChatMessage] = field(default_factory=list)
    status: str = "pending"
    result: Optional[str] = None
    progress: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    last_accessed_at: float = field(default_factory=time.time)

    def touch(self) -> None:
        self.last_accessed_at = time.time()

    def add_message(self, role: str, content: str) -> None:
        self.messages.append(ChatMessage(role=role, content=content, timestamp=time.time()))

    def get_transcript(self, max_messages: int = 20) -> str:
        recent = self.messages[-max_messages:]
        return "\n".join(f"[{_format_timestamp(msg.timestamp)}] {msg.role}: {msg.content}" for msg in recent)


class InMemoryChatStorage:
    def __init__(self, ttl_seconds: float = 86400.0) -> None:
        self._sessions: dict[str, ChatSession] = {}
        self._ttl_seconds = ttl_seconds

    def _is_expired(self, session: ChatSession, now: Optional[float] = None) -> bool:
        current = now if now is not None else time.time()
        return (current - session.last_accessed_at) > self._ttl_seconds

    def evict_expired(self) -> list[str]:
        now = time.time()
        evicted: list[str] = []
        for job_id, session in list(self._sessions.items()):
            if self._is_expired(session, now=now):
                self._sessions.pop(job_id, None)
                evicted.append(job_id)
        if evicted:
            logger.info("Evicted expired chat sessions: {}", evicted)
        return evicted

    def create_session(self, job_id: str) -> ChatSession:
        session = ChatSession(job_id=job_id)
        session.touch()
        self._sessions[job_id] = session
        return session

    def get_session(self, job_id: str) -> Optional[ChatSession]:
        self.evict_expired()
        session = self._sessions.get(job_id)
        if session is not None:
            session.touch()
        return session

    def get_or_create_session(self, job_id: str) -> ChatSession:
        session = self.get_session(job_id)
        if session is not None:
            return session
        return self.create_session(job_id)

    def delete_session(self, job_id: str) -> None:
        self._sessions.pop(job_id, None)

    def list_sessions(self) -> list[str]:
        self.evict_expired()
        return list(self._sessions.keys())
