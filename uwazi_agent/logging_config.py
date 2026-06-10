import json
import os
import socket
import sys
from typing import Any

from loguru import logger

_LOG_CONTEXT: dict[str, Any] = {}


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

    graylog_host = os.environ.get("GRAYLOG_HOST", "")
    graylog_port = int(os.environ.get("GRAYLOG_PORT", "12201"))

    logger.remove()

    fmt = (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
        "url={extra[uwazi_url]} user={extra[uwazi_user]} | "
        "<level>{message}</level>"
    )

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
            filter=lambda record: all(key in record["extra"] for key in ("uwazi_url", "uwazi_user")),
        )
    else:
        logger.add(
            sys.stderr,
            level="DEBUG",
            format=fmt,
            filter=lambda record: all(key in record["extra"] for key in ("uwazi_url", "uwazi_user")),
        )
        logger.add(
            sys.stderr,
            level="DEBUG",
            format=(
                "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
                "<level>{level: <8}</level> | "
                "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
                "<level>{message}</level>"
            ),
            filter=lambda record: not all(key in record["extra"] for key in ("uwazi_url", "uwazi_user")),
        )

    logger.configure(extra={"uwazi_url": url, "uwazi_user": user})


def bind_context(**kwargs: Any) -> None:
    global _LOG_CONTEXT
    _LOG_CONTEXT.update(kwargs)
    logger.configure(extra={**{k: v for k, v in _LOG_CONTEXT.items()}, **kwargs})
