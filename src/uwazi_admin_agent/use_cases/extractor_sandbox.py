"""The extractor-function sandbox gate, shared by every authoring path.

A candidate ``def extract(text, ctx) -> dict | None`` reaches a generated script
only after it passes TWO checks that must never diverge:

- :func:`validate_extractor_source` — the STATIC gate: the source parses, has no
  import lines and no class statements, and ``exec``s in the script sandbox with
  a callable two-argument ``extract``;
- :func:`build_extractor_namespace` — the same ``exec``, returning the namespace so
  a caller can RUN the function (the ``author_html_extractor`` self-proof and the
  ``benchmark_extractor`` strategy comparison).

Both the authoring subagent (:mod:`...author_extractor_tool`) and the benchmark
tool (:mod:`...extraction_benchmark_tool`) use this module, so a candidate proven
in one is guaranteed to be loadable in the other — and in the emitted script's own
namespace, which binds the same :data:`~...script_exec_namespace._STDLIB` and
``SAFE_BUILTINS``.

Pure: no LLM, no ports, no I/O. Unit-tested directly.
"""

from __future__ import annotations

import ast
from typing import Any

from uwazi_admin_agent.use_cases.script_exec_namespace import _STDLIB, SAFE_BUILTINS
from uwazi_admin_agent.use_cases.script_safety import validate_script_source

#: The sandbox names an extractor runs with: the bound stdlib subset plus the
#: curated builtins (no ``__import__``/``open``/``getattr``).
EXTRACTOR_BUILTINS: dict[str, Any] = SAFE_BUILTINS


def build_extractor_namespace(source: str) -> dict[str, Any]:
    """``exec`` one extractor source in the script sandbox; return its namespace.

    The static safety gate runs here as well as in the validator: this is the
    ``exec`` choke point for the authoring/benchmark paths, so no unchecked
    source can slip through even if a caller forgets to validate first.
    """
    rejection = validate_script_source(source)
    if rejection is not None:
        raise ValueError(rejection)
    namespace: dict[str, Any] = {"__builtins__": EXTRACTOR_BUILTINS, **_STDLIB}
    exec(compile(source, "<extractor>", "exec"), namespace)  # noqa: S102 - sandboxed by SAFE_BUILTINS + _STDLIB
    return namespace


def validate_extractor_source(code: str) -> str | None:
    """Validate emitted extractor source WITHOUT mocks: parse it, reject imports
    and classes, then ``exec`` it in the script sandbox and require a callable
    two-argument ``extract``.

    Returns ``None`` when valid, otherwise a rejection message the generation
    agent can act on. Pure seam (no LLM, no I/O) — unit-tested directly.
    """
    rejection = validate_script_source(code)
    if rejection is not None:
        return rejection.replace("ScriptRejected:", "Rejected:", 1)
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return f"Rejected: extractor source does not parse ({exc.lineno}: {exc.msg})."
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            return "Rejected: extractor source must contain ZERO import lines (everything is bound)."
        if isinstance(node, ast.ClassDef):
            return "Rejected: extractor source must not define classes (class does not work in the sandbox; use def)."
    try:
        namespace = build_extractor_namespace(code)
    except Exception as exc:  # noqa: BLE001 - any exec failure is a rejection
        return f"Rejected: extractor source failed to exec in the sandbox ({type(exc).__name__}: {exc})."
    extract = namespace.get("extract")
    if not callable(extract):
        return "Rejected: extractor source must define a callable named `extract`."
    code_obj = getattr(extract, "__code__", None)
    if (
        code_obj is None
        or code_obj.co_argcount != 2
        or code_obj.co_flags & 0x04  # CO_VARARGS
        or code_obj.co_flags & 0x08  # CO_VARKEYWORDS
    ):
        argcount = code_obj.co_argcount if code_obj is not None else -1
        return f"Rejected: `extract` must take exactly two arguments (html, ctx), got {argcount}."
    return None


__all__ = ["EXTRACTOR_BUILTINS", "build_extractor_namespace", "validate_extractor_source"]
