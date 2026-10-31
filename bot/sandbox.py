"""Validation + restricted loading for strategy code (human- or AI-written).

Defense in depth, not a perfect sandbox:
  1. AST allowlist: only `math`/`statistics` imports, no dunder access, no dangerous builtins.
  2. Execution with a stripped-down __builtins__.
  3. Backtests of candidates run in a separate process with a hard timeout (see evolve.py).
"""
from __future__ import annotations

import ast
import builtins as _b

ALLOWED_IMPORTS = {"math", "statistics"}
FORBIDDEN_NAMES = {
    "eval", "exec", "compile", "open", "__import__", "globals", "locals", "vars", "getattr",
    "setattr", "delattr", "input", "breakpoint", "memoryview", "help", "exit", "quit", "type",
    "object", "classmethod", "staticmethod", "property", "super", "dir", "id", "hash",
}
SAFE_BUILTINS = {
    n: getattr(_b, n) for n in (
        "abs", "all", "any", "bool", "dict", "enumerate", "filter", "float", "int", "isinstance",
        "len", "list", "map", "max", "min", "pow", "range", "reversed", "round", "set", "sorted",
        "str", "sum", "tuple", "zip", "ValueError", "ZeroDivisionError", "KeyError", "IndexError",
        "Exception", "True", "False", "None", "print",
    ) if hasattr(_b, n)
}
MAX_SOURCE_BYTES = 40_000


class StrategyRejected(Exception):
    pass


def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name not in ALLOWED_IMPORTS or level != 0:
        raise ImportError(f"import of {name!r} not allowed in strategies")
    return __import__(name, globals, locals, fromlist, level)


def validate_source(src: str) -> ast.Module:
    if len(src.encode()) > MAX_SOURCE_BYTES:
        raise StrategyRejected("source too large")
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        raise StrategyRejected(f"syntax error: {e}") from e
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] not in ALLOWED_IMPORTS:
                    raise StrategyRejected(f"import {a.name} not allowed (allowed: {sorted(ALLOWED_IMPORTS)})")
        elif isinstance(node, ast.ImportFrom):
            if node.level or (node.module or "").split(".")[0] not in ALLOWED_IMPORTS:
                raise StrategyRejected(f"from-import {node.module} not allowed")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            raise StrategyRejected(f"use of {node.id!r} not allowed")
        elif isinstance(node, ast.Name) and node.id.startswith("__"):
            raise StrategyRejected(f"dunder name {node.id!r} not allowed")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise StrategyRejected(f"private/dunder attribute .{node.attr} not allowed")
        elif isinstance(node, (ast.Global, ast.Nonlocal, ast.AsyncFunctionDef, ast.Await, ast.ClassDef)):
            raise StrategyRejected(f"{type(node).__name__} not allowed")
    names = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    if "generate_signals" not in names:
        raise StrategyRejected("strategy must define generate_signals(history, positions, params)")
    return tree


def load_strategy(src: str, filename: str = "<strategy>") -> dict:
    """Validate and execute strategy source; returns its namespace (generate_signals, PARAMS, ...)."""
    tree = validate_source(src)
    code = compile(tree, filename, "exec")
    ns: dict = {"__builtins__": {**SAFE_BUILTINS, "__import__": _guarded_import}, "__name__": "strategy"}
    exec(code, ns)  # noqa: S102 - validated above
    if not callable(ns.get("generate_signals")):
        raise StrategyRejected("generate_signals is not callable")
    ns.setdefault("PARAMS", {})
    if not isinstance(ns["PARAMS"], dict):
        raise StrategyRejected("PARAMS must be a dict")
    return ns
