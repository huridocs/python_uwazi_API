import json
import os
import socket
import sys
from pathlib import Path
from typing import Any

from loguru import logger

_LOG_CONTEXT: dict[str, Any] = {}


def _patch_record(record: dict[str, Any]) -> None:
    """Inject a ``file_name`` field (basename of the source file, no extension)
    so loguru's format string can show e.g. ``agent_factory`` instead of the
    full module path ``uwazi_agent.use_cases.agent_factory``.
    """
    record["extra"]["file_name"] = Path(record["file"].path).stem


def _gelf_message(record: dict[str, Any]) -> dict[str, Any]:
    extra = record.get("extra", {})
    msg: dict[str, Any] = {
        "version": "1.1",
        "host": "ai-assistant",
        "short_message": record["message"],
        "timestamp": record["time"].timestamp(),
        "level": record["level"].no,
        "_logger": record["name"],
        "_file": record["file"].name,
        "_line": record["line"],
        "_function": record["function"],
    }
    for key, value in _LOG_CONTEXT.items():
        msg[f"_{key}"] = value
    for key, value in extra.items():
        msg[f"_{key}"] = value
    return msg


def _make_graylog_sink(host: str, port: int) -> Any:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def sink(message: Any) -> None:
        try:
            gelf = _gelf_message(message.record)
            data = json.dumps(gelf).encode("utf-8")
            sock.sendto(data, (host, port))
        except Exception:
            pass

    return sink


def setup_logging(url: str = "", user: str = "") -> None:
    global _LOG_CONTEXT
    _LOG_CONTEXT = {
        "uwazi_url": url,
        "uwazi_user": user,
        "source": "ai-assistant",
    }

    graylog_host = os.environ.get("GRAYLOG_IP", "")
    graylog_port = int(os.environ.get("GRAYLOG_PORT", "12201"))

    logger.remove()

    fmt = (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{extra[file_name]}</cyan> | "
        "url={extra[uwazi_url]} user={extra[uwazi_user]} | "
        "<level>{message}</level>"
    )

    def _has_creds(record: dict[str, Any]) -> bool:
        return all(key in record["extra"] for key in ("uwazi_url", "uwazi_user"))

    if graylog_host:
        logger.add(
            _make_graylog_sink(graylog_host, graylog_port),
            level="DEBUG",
            format="{message}",
        )
        logger.add(
            sys.stderr,
            level="INFO",
            format=fmt,
            filter=_has_creds,
        )
        logger.add(
            sys.stderr,
            level="DEBUG",
            format=(
                "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
                "<level>{level: <8}</level> | "
                "<cyan>{extra[file_name]}</cyan> | "
                "<level>{message}</level>"
            ),
            filter=lambda record: not _has_creds(record),
        )
    else:
        logger.add(
            sys.stderr,
            level="DEBUG",
            format=fmt,
            filter=_has_creds,
        )
        logger.add(
            sys.stderr,
            level="DEBUG",
            format=(
                "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
                "<level>{level: <8}</level> | "
                "<cyan>{extra[file_name]}</cyan> | "
                "<level>{message}</level>"
            ),
            filter=lambda record: not _has_creds(record),
        )

    logger.configure(extra={"uwazi_url": url, "uwazi_user": user})
    logger.configure(patcher=_patch_record)


def bind_context(**kwargs: Any) -> None:
    global _LOG_CONTEXT
    _LOG_CONTEXT.update(kwargs)
    logger.configure(extra={**{k: v for k, v in _LOG_CONTEXT.items()}, **kwargs})


def _count_newlines(text: str) -> int:
    """Count newline characters in ``text`` without including a trailing one."""
    if not text:
        return 0
    count = text.count("\n")
    if text.endswith("\n"):
        count -= 1
    return count


def truncate_log_message(message: str, max_lines: int = 5) -> str:
    """Keep ``message`` compact: at most ``max_lines`` lines.

    Long multi-line messages (e.g. page scripts, entity JSON dumps) blow up
    Docker console output and make logs hard to scan. This helper collapses
    anything that exceeds ``max_lines`` by returning the first
    ``max_lines - 1`` lines, then a final line that states how many extra
    lines were dropped.

    Args:
        message: The original log message.
        max_lines: Maximum number of lines to emit. Defaults to 5.

    Returns:
        A possibly-truncated version of ``message`` with at most ``max_lines``
        lines.
    """
    if not message or max_lines <= 0:
        return message

    lines = message.splitlines()
    if len(lines) <= max_lines:
        return message

    kept = lines[: max_lines - 1]
    dropped = len(lines) - (max_lines - 1)
    kept.append(f"... ({dropped} more line{'s' if dropped != 1 else ''})")
    return "\n".join(kept)
