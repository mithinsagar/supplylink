"""A restricted expression evaluator for contract business rules.

Contracts express cross-field rules as short Python-like expressions::

    rules:
      - name: stocked_items_must_be_priced
        expr: "quantity_available == 0 or unit_price > 0"

Evaluating those with :func:`eval` would mean any contract file — a supplier
onboarding artefact, plausibly edited by someone outside the core team — could
execute arbitrary code inside the pipeline. Instead the expression is parsed to
an AST once, structurally validated against an allow-list, and then interpreted
node by node. Anything not on the list (attribute access, calls to unknown
names, subscripts, comprehensions, imports, lambdas, walrus) is rejected at
contract-load time, not at record-evaluation time — so a malformed rule fails the
run before a single record is touched.

The allow-list is deliberately small. It covers comparison, boolean and
arithmetic operators plus a handful of pure helper functions, which is enough for
every business rule these feeds need. Growing it is a code change with a code
review, which is the point.
"""

from __future__ import annotations

import ast
import operator
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

from supplylink.errors import ContractError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

_BINARY_OPERATORS: Final[dict[type[ast.operator], Callable[[Any, Any], Any]]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.FloorDiv: operator.floordiv,
}

_COMPARISONS: Final[dict[type[ast.cmpop], Callable[[Any, Any], Any]]] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}

_UNARY_OPERATORS: Final[dict[type[ast.unaryop], Callable[[Any], Any]]] = {
    ast.Not: operator.not_,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _length(value: Any) -> int:
    return len(value) if value is not None else 0


#: Pure helpers a rule may call. No I/O, no attribute access, no reflection.
_FUNCTIONS: Final[dict[str, Callable[..., Any]]] = {
    "abs": abs,
    "len": _length,
    "min": min,
    "max": max,
    "round": round,
    "lower": lambda value: str(value).lower() if value is not None else None,
    "upper": lambda value: str(value).upper() if value is not None else None,
    "startswith": lambda value, prefix: (
        str(value).startswith(prefix) if value is not None else False
    ),
    "is_null": lambda value: value is None,
    "not_null": lambda value: value is not None,
}

#: Node types the interpreter understands. Everything else is a contract error.
_ALLOWED_NODES: Final[tuple[type[ast.AST], ...]] = (
    ast.Expression,
    ast.BoolOp,
    ast.BinOp,
    ast.UnaryOp,
    ast.Compare,
    ast.IfExp,
    ast.Call,
    ast.Constant,
    ast.Name,
    ast.Load,
    ast.And,
    ast.Or,
    ast.Tuple,
    ast.List,
    *_BINARY_OPERATORS,
    *_COMPARISONS,
    *_UNARY_OPERATORS,
)


class Expression:
    """A parsed, validated business-rule expression.

    Parsing and structural validation happen once at contract load. Evaluation is
    a pure function of the record, so the same expression object is safely reused
    across every record in a batch.
    """

    __slots__ = ("_tree", "identifiers", "source")

    def __init__(self, source: str) -> None:
        self.source = source
        try:
            tree = ast.parse(source, mode="eval")
        except SyntaxError as exc:
            raise ContractError(
                f"rule expression is not valid syntax: {source!r} ({exc.msg})"
            ) from exc

        self.identifiers = _validate(tree, source)
        self._tree = tree

    def evaluate(self, record: Mapping[str, Any]) -> bool:
        """Evaluate against one record. The result is coerced to ``bool``."""
        return bool(_eval_node(self._tree.body, record))

    def __repr__(self) -> str:
        return f"Expression({self.source!r})"


def _validate(tree: ast.Expression, source: str) -> frozenset[str]:
    """Reject disallowed constructs and return the free identifiers used."""
    identifiers: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ContractError(
                f"rule expression uses unsupported construct {type(node).__name__!r}: {source!r}"
            )
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCTIONS:
                allowed = ", ".join(sorted(_FUNCTIONS))
                raise ContractError(
                    f"rule expression calls an unavailable function: {source!r}. "
                    f"Available: {allowed}"
                )
            if node.keywords:
                raise ContractError(f"rule expression uses keyword arguments: {source!r}")
        elif isinstance(node, ast.Name):
            identifiers.add(node.id)
    return frozenset(identifiers - set(_FUNCTIONS))


def _eval_node(node: ast.AST, record: Mapping[str, Any]) -> Any:  # noqa: PLR0911
    if isinstance(node, ast.Constant):
        return node.value

    if isinstance(node, ast.Name):
        # An unmapped field reads as NULL rather than raising, so a rule can be
        # written once for feeds that differ in which optional fields they carry.
        return record.get(node.id)

    if isinstance(node, ast.BoolOp):
        values = (_eval_node(value, record) for value in node.values)
        if isinstance(node.op, ast.And):
            return all(values)
        return any(values)

    if isinstance(node, ast.UnaryOp):
        return _UNARY_OPERATORS[type(node.op)](_eval_node(node.operand, record))

    if isinstance(node, ast.BinOp):
        left = _coerce(_eval_node(node.left, record))
        right = _coerce(_eval_node(node.right, record))
        return _BINARY_OPERATORS[type(node.op)](left, right)

    if isinstance(node, ast.Compare):
        return _eval_compare(node, record)

    if isinstance(node, ast.IfExp):
        branch = node.body if _eval_node(node.test, record) else node.orelse
        return _eval_node(branch, record)

    if isinstance(node, ast.Call):
        arguments = [_eval_node(argument, record) for argument in node.args]
        return _FUNCTIONS[node.func.id](*arguments)  # type: ignore[attr-defined]

    if isinstance(node, ast.Tuple | ast.List):
        return [_eval_node(element, record) for element in node.elts]

    raise ContractError(f"unsupported expression node {type(node).__name__!r}")


def _eval_compare(node: ast.Compare, record: Mapping[str, Any]) -> bool:
    """Evaluate a (possibly chained) comparison with SQL-like NULL semantics.

    A comparison involving NULL is false rather than an error. Nullability is the
    ``required`` rule's job; a business rule should not also have to restate it,
    and should not blow up a record because an optional field was absent.
    """
    left = _eval_node(node.left, record)
    for op, comparator_node in zip(node.ops, node.comparators, strict=True):
        right = _eval_node(comparator_node, record)
        membership = isinstance(op, ast.In | ast.NotIn)
        if not membership and (left is None or right is None):
            return False
        if not _COMPARISONS[type(op)](_coerce(left), _coerce(right) if not membership else right):
            return False
        left = right
    return True


def _coerce(value: Any) -> Any:
    """Make mixed numeric types comparable.

    Prices are ``Decimal`` and quantities are ``int``; Python compares those
    happily, but ``Decimal`` and ``float`` together raise. Floats only enter from
    JSON literals inside a rule, so promoting them is safe and keeps rule authors
    from having to think about it.
    """
    return Decimal(str(value)) if isinstance(value, float) else value
