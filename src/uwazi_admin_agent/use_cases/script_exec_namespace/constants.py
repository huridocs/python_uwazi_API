"""Sandbox namespace module split out of ``script_exec_namespace`` (behavior preserved)."""

from __future__ import annotations

import builtins as _builtins
import collections
import datetime as _datetime
import itertools
import json
import math
import random
import re
from types import SimpleNamespace
from typing import Any

from uwazi_admin_agent.domain.date_parse import find as date_find
from uwazi_admin_agent.domain.date_parse import is_date as date_is_date
from uwazi_admin_agent.domain.date_parse import parse as date_parse
from uwazi_admin_agent.domain.html_extract import html_meta, html_tables, html_text, html_title, is_html_ref
from uwazi_admin_agent.domain.text_match import Scorer as TextMatchScorer
from uwazi_admin_agent.domain.text_match import best as text_match_best
from uwazi_admin_agent.domain.text_match import partial_ratio as text_match_partial_ratio
from uwazi_admin_agent.domain.text_match import rank as text_match_rank
from uwazi_admin_agent.domain.text_match import ratio as text_match_ratio
from uwazi_admin_agent.domain.text_match import token_set_ratio as text_match_token_set_ratio
from uwazi_admin_agent.domain.text_match import token_sort_ratio as text_match_token_sort_ratio
from uwazi_admin_agent.domain.text_match import wratio as text_match_wratio

_HTMLEXTRACT: Any = SimpleNamespace(
    text=html_text,
    title=html_title,
    tables=html_tables,
    meta=html_meta,
    is_html=is_html_ref,
)


_TEXTMATCH: Any = SimpleNamespace(
    ratio=text_match_ratio,
    partial_ratio=text_match_partial_ratio,
    token_sort_ratio=text_match_token_sort_ratio,
    token_set_ratio=text_match_token_set_ratio,
    wratio=text_match_wratio,
    rank=text_match_rank,
    best=text_match_best,
    Scorer=TextMatchScorer,
)


_DATEPARSE: Any = SimpleNamespace(
    parse=date_parse,
    find=date_find,
    is_date=date_is_date,
)


_STDLIB: dict[str, Any] = {
    "json": json,
    "re": re,
    "collections": collections,
    "itertools": itertools,
    "datetime": _datetime,
    "math": math,
    "random": random,
    "htmlextract": _HTMLEXTRACT,
    "textmatch": _TEXTMATCH,
    "dateparse": _DATEPARSE,
}


_SAFE_BUILTIN_NAMES: tuple[str, ...] = (
    "None",
    "True",
    "False",
    "Ellipsis",
    "NotImplemented",
    "bool",
    "bytes",
    "bytearray",
    "complex",
    "dict",
    "float",
    "frozenset",
    "int",
    "list",
    "set",
    "str",
    "tuple",
    "range",
    "enumerate",
    "zip",
    "map",
    "filter",
    "sorted",
    "reversed",
    "iter",
    "next",
    "slice",
    "divmod",
    "pow",
    "abs",
    "round",
    "sum",
    "min",
    "max",
    "any",
    "all",
    "bin",
    "hex",
    "oct",
    "chr",
    "ord",
    "ascii",
    "format",
    "repr",
    "len",
    "hash",
    "isinstance",
    "issubclass",
    "callable",
    "print",
    "Exception",
    "BaseException",
    "StopIteration",
    "StopAsyncIteration",
    "ArithmeticError",
    "ZeroDivisionError",
    "OverflowError",
    "FloatingPointError",
    "AssertionError",
    "AttributeError",
    "BufferError",
    "EOFError",
    "ImportError",
    "ModuleNotFoundError",
    "IndexError",
    "KeyError",
    "KeyboardInterrupt",
    "LookupError",
    "MemoryError",
    "NameError",
    "NotImplementedError",
    "OSError",
    "RecursionError",
    "ReferenceError",
    "RuntimeError",
    "SystemError",
    "TypeError",
    "UnboundLocalError",
    "UnicodeError",
    "UnicodeDecodeError",
    "UnicodeEncodeError",
    "ValueError",
    "Warning",
    "DeprecationWarning",
    "UserWarning",
    "GeneratorExit",
    "SystemExit",
)


SAFE_BUILTINS: dict[str, Any] = {name: getattr(_builtins, name) for name in _SAFE_BUILTIN_NAMES}


_SEARCH_MODES: tuple[str, ...] = ("by_text", "by_filter", "by_template")


_BY_IDS_CHUNK: int = 50
