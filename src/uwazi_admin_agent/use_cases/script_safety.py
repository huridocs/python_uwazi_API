"""Static gate for LLM-authored script source (defense in depth).

The exec namespace (``SAFE_BUILTINS`` + the bound helper surface) is the primary
capability boundary, but a hand-crafted ``__globals__`` / ``().__class__`` walk
can reach the host interpreter through object introspection. This module rejects
that shape BEFORE ``exec`` so the cheap escapes never run.

This is deliberately a gate, not a sandbox: a determined adversary can find
paths a static AST walk misses. The real containment boundary is the process
the script runs in (no credentials, no network, no filesystem); this gate raises
the bar for the accidental and prompt-injected cases that dominate a trusted
operator deployment.

Pure: ``ast`` only, no I/O, unit-testable offline.
"""

from __future__ import annotations

import ast

#: Calls that reach the host even though a few of them are not in SAFE_BUILTINS
#: today. Listing them keeps the gate correct if the builtins list changes and
#: gives the generator a clear rejection instead of a NameError later.
_FORBIDDEN_CALLS: frozenset[str] = frozenset(
    {
        "__import__",
        "breakpoint",
        "compile",
        "delattr",
        "eval",
        "exec",
        "format",
        "getattr",
        "globals",
        "input",
        "locals",
        "memoryview",
        "open",
        "setattr",
        "vars",
    }
)


def _is_dunder(name: str) -> bool:
    """True for a CPython protocol name (``__class__``, ``__globals__``, ...)."""
    return name.startswith("__") or name.endswith("__")


def validate_script_source(code: str) -> str | None:
    """Reject source that can reach the host through introspection.

    Returns ``None`` when the source is acceptable, otherwise a one-line
    ``ScriptRejected: ...`` message. Rejected constructs:

    - ``import`` / ``from ... import`` and ``class`` definitions;
    - any ``__dunder__`` name or attribute (``__globals__``, ``__class__``,
      ``__subclasses__``, ``__builtins__``, ...);
    - calls to ``getattr``/``eval``/``exec``/``open``/``vars``/``globals``/...;
    - ``str.format`` and the ``format`` builtin: ``"{0.__class__}"`` hides an
      attribute walk inside a string literal the AST never sees as an
      ``Attribute`` node, so it must be refused wholesale.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return f"ScriptRejected: script does not parse ({exc.lineno}: {exc.msg})."
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            return "ScriptRejected: import statements are not allowed."
        if isinstance(node, ast.ClassDef):
            return "ScriptRejected: class definitions are not allowed."
        if isinstance(node, ast.Name) and _is_dunder(node.id):
            return f"ScriptRejected: dunder name {node.id!r} is not allowed."
        if isinstance(node, ast.Attribute) and _is_dunder(node.attr):
            return f"ScriptRejected: dunder attribute {node.attr!r} is not allowed."
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FORBIDDEN_CALLS:
            return f"ScriptRejected: call to {node.func.id!r} is not allowed."
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
            return "ScriptRejected: str.format is not allowed (format-field traversal)."
    return None


__all__ = ["validate_script_source"]
