from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ChatMessage:
    role: str
    content: str


@dataclass
class ChatSession:
    job_id: str
    messages: list[ChatMessage] = field(default_factory=list)
    status: str = "pending"
    result: Optional[str] = None

    def add_message(self, role: str, content: str) -> None:
        self.messages.append(ChatMessage(role=role, content=content))

    def get_context(self, max_messages: int = 10) -> str:
        recent = self.messages[-max_messages:] if len(self.messages) > max_messages else self.messages
        context_parts = []
        for msg in recent:
            if msg.role == "user":
                context_parts.append(f"Previous task: {msg.content}")
            elif msg.role == "assistant":
                context_parts.append(f"Previous answer: {msg.content}")
        return "\n\n".join(context_parts)


class InMemoryChatStorage:
    def __init__(self) -> None:
        self._sessions: dict[str, ChatSession] = {}

    def create_session(self, job_id: str) -> ChatSession:
        session = ChatSession(job_id=job_id)
        self._sessions[job_id] = session
        return session

    def get_session(self, job_id: str) -> Optional[ChatSession]:
        return self._sessions.get(job_id)

    def delete_session(self, job_id: str) -> None:
        self._sessions.pop(job_id, None)

    def list_sessions(self) -> list[str]:
        return list(self._sessions.keys())
