from contextvars import ContextVar

_current_agent: ContextVar[str] = ContextVar("current_agent", default="orchestrator")


def get_current_agent() -> str:
    return _current_agent.get()


def set_current_agent(name: str) -> None:
    _current_agent.set(name)
