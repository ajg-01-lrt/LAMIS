"""Small, deliberately constrained evaluator for the sanitized legacy formulas.

This is not a general Excel calculation engine.  The RLS workbook uses only a
tiny expression language (IF, CHAR, LOWER, XLOOKUP, cell/range references,
arithmetic, comparisons, and string concatenation).  Supporting exactly that
surface keeps config generation deterministic and independent of Excel while
making unsupported workbook changes fail closed.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Callable, Iterable, Mapping, Sequence


class FormulaError(ValueError):
    """Raised when a formula falls outside the supported safe subset."""


@dataclass(frozen=True)
class _Token:
    kind: str
    value: str
    position: int


_TOKEN_RE = re.compile(
    r"""
    (?P<SPACE>\s+)
  | (?P<STRING>"(?:""|[^"])*")
  | (?P<NUMBER>(?:\d+\.\d*|\d*\.\d+|\d+))
  | (?P<CELL>
        (?:(?:'[A-Za-z0-9_ .-]+'|[A-Za-z_][A-Za-z0-9_ .-]*)!)?
        \$?[A-Z]{1,3}\$?\d+
        (?::\$?[A-Z]{1,3}\$?\d+)?
    )
  | (?P<OP>>=|<=|<>|=|>|<|\+|-|\*|/|&)
  | (?P<LPAREN>\()
  | (?P<RPAREN>\))
  | (?P<COMMA>,)
  | (?P<IDENT>[_A-Za-z][_A-Za-z0-9.]*)
    """,
    re.VERBOSE | re.IGNORECASE,
)

_CELL_RE = re.compile(
    r"^(?:(?P<sheet>'[^']+'|[A-Za-z_][A-Za-z0-9_ .-]*)!)?"
    r"(?P<start>\$?[A-Z]{1,3}\$?\d+)"
    r"(?::(?P<end>\$?[A-Z]{1,3}\$?\d+))?$",
    re.IGNORECASE,
)
_COORD_RE = re.compile(r"^\$?(?P<col>[A-Z]{1,3})\$?(?P<row>\d+)$", re.IGNORECASE)


def _tokens(formula: str) -> list[_Token]:
    text = formula[1:] if formula.startswith("=") else formula
    result: list[_Token] = []
    pos = 0
    while pos < len(text):
        match = _TOKEN_RE.match(text, pos)
        if not match:
            snippet = text[pos : pos + 30]
            raise FormulaError(f"unsupported token at offset {pos}: {snippet!r}")
        kind = match.lastgroup or ""
        if kind != "SPACE":
            result.append(_Token(kind, match.group(kind), pos))
        pos = match.end()
    result.append(_Token("EOF", "", pos))
    return result


def _column_number(name: str) -> int:
    number = 0
    for char in name.upper():
        number = number * 26 + (ord(char) - ord("A") + 1)
    return number


def _column_name(number: int) -> str:
    chars: list[str] = []
    while number:
        number, remainder = divmod(number - 1, 26)
        chars.append(chr(ord("A") + remainder))
    return "".join(reversed(chars))


def _normalize_coordinate(value: str) -> str:
    match = _COORD_RE.match(value)
    if not match:
        raise FormulaError(f"invalid cell coordinate: {value!r}")
    return f"{match.group('col').upper()}{int(match.group('row'))}"


def _iter_range(start: str, end: str) -> Iterable[str]:
    first = _COORD_RE.match(start)
    last = _COORD_RE.match(end)
    if not first or not last:
        raise FormulaError(f"invalid range: {start}:{end}")
    c1, c2 = _column_number(first.group("col")), _column_number(last.group("col"))
    r1, r2 = int(first.group("row")), int(last.group("row"))
    if c2 < c1 or r2 < r1:
        raise FormulaError(f"reverse ranges are not supported: {start}:{end}")
    for row in range(r1, r2 + 1):
        for column in range(c1, c2 + 1):
            yield f"{_column_name(column)}{row}"


def excel_text(value: Any) -> str:
    """Render a scalar the way Excel's ``&`` operator renders common values."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return format(value, ".15g")
    return str(value)


class FormulaEvaluator:
    """Evaluate a formula using caller-provided current-sheet and lookup cells."""

    def __init__(
        self,
        current_cells: Mapping[str, Any],
        lookup_sheets: Mapping[str, Mapping[str, Any]],
    ) -> None:
        self.current_cells = {
            _normalize_coordinate(key): value for key, value in current_cells.items()
        }
        self.lookup_sheets = {
            sheet.casefold(): {
                _normalize_coordinate(key): value for key, value in cells.items()
            }
            for sheet, cells in lookup_sheets.items()
        }

    def evaluate(self, formula: str) -> Any:
        parser = _Parser(_tokens(formula), self._resolve_reference)
        value = parser.parse()
        return value

    def _resolve_reference(self, reference: str) -> Any:
        match = _CELL_RE.match(reference)
        if not match:
            raise FormulaError(f"invalid reference: {reference!r}")
        raw_sheet = match.group("sheet")
        sheet = raw_sheet.strip("'") if raw_sheet else None
        start = _normalize_coordinate(match.group("start"))
        end_raw = match.group("end")

        if sheet is None:
            cells = self.current_cells
        else:
            try:
                cells = self.lookup_sheets[sheet.casefold()]
            except KeyError as exc:
                raise FormulaError(f"unknown lookup sheet: {sheet}") from exc

        if end_raw:
            end = _normalize_coordinate(end_raw)
            return [cells.get(coord) for coord in _iter_range(start, end)]
        if start not in cells:
            raise FormulaError(f"missing value for cell {reference}")
        return cells[start]


class _Parser:
    def __init__(
        self,
        tokens: Sequence[_Token],
        resolve_reference: Callable[[str], Any],
    ) -> None:
        self.tokens = tokens
        self.index = 0
        self.resolve_reference = resolve_reference

    @property
    def current(self) -> _Token:
        return self.tokens[self.index]

    def advance(self) -> _Token:
        token = self.current
        self.index += 1
        return token

    def accept(self, kind: str, value: str | None = None) -> _Token | None:
        token = self.current
        if token.kind != kind:
            return None
        if value is not None and token.value != value:
            return None
        self.index += 1
        return token

    def expect(self, kind: str, value: str | None = None) -> _Token:
        token = self.accept(kind, value)
        if token is None:
            wanted = kind if value is None else f"{kind} {value!r}"
            raise FormulaError(
                f"expected {wanted} at offset {self.current.position}, "
                f"got {self.current.kind} {self.current.value!r}"
            )
        return token

    def parse(self) -> Any:
        value = self.comparison()
        self.expect("EOF")
        return value

    def comparison(self) -> Any:
        left = self.concatenation()
        while self.current.kind == "OP" and self.current.value in {
            "=",
            "<>",
            ">",
            "<",
            ">=",
            "<=",
        }:
            operator = self.advance().value
            right = self.concatenation()
            left = self._compare(left, operator, right)
        return left

    def concatenation(self) -> Any:
        left = self.additive()
        while self.accept("OP", "&"):
            right = self.additive()
            left = excel_text(left) + excel_text(right)
        return left

    def additive(self) -> Any:
        left = self.multiplicative()
        while self.current.kind == "OP" and self.current.value in {"+", "-"}:
            operator = self.advance().value
            right = self.multiplicative()
            left = left + right if operator == "+" else left - right
        return left

    def multiplicative(self) -> Any:
        left = self.unary()
        while self.current.kind == "OP" and self.current.value in {"*", "/"}:
            operator = self.advance().value
            right = self.unary()
            left = left * right if operator == "*" else left / right
        return left

    def unary(self) -> Any:
        if self.accept("OP", "+"):
            return +self.unary()
        if self.accept("OP", "-"):
            return -self.unary()
        return self.primary()

    def primary(self) -> Any:
        token = self.current
        if token.kind == "STRING":
            self.advance()
            return token.value[1:-1].replace('""', '"')
        if token.kind == "NUMBER":
            self.advance()
            return float(token.value) if "." in token.value else int(token.value)
        if token.kind == "CELL":
            self.advance()
            return self.resolve_reference(token.value)
        if token.kind == "IDENT":
            name = self.advance().value
            if self.accept("LPAREN"):
                args: list[Any] = []
                if not self.accept("RPAREN"):
                    while True:
                        args.append(self.comparison())
                        if self.accept("RPAREN"):
                            break
                        self.expect("COMMA")
                return self._call(name, args)
            if name.casefold() == "true":
                return True
            if name.casefold() == "false":
                return False
            raise FormulaError(f"unknown identifier: {name}")
        if self.accept("LPAREN"):
            value = self.comparison()
            self.expect("RPAREN")
            return value
        raise FormulaError(
            f"expected expression at offset {token.position}, "
            f"got {token.kind} {token.value!r}"
        )

    @staticmethod
    def _compare(left: Any, operator: str, right: Any) -> bool:
        if isinstance(left, str) and isinstance(right, str):
            left_cmp: Any = left.casefold()
            right_cmp: Any = right.casefold()
        else:
            left_cmp, right_cmp = left, right
        if operator == "=":
            return left_cmp == right_cmp
        if operator == "<>":
            return left_cmp != right_cmp
        if operator == ">":
            return left_cmp > right_cmp
        if operator == "<":
            return left_cmp < right_cmp
        if operator == ">=":
            return left_cmp >= right_cmp
        if operator == "<=":
            return left_cmp <= right_cmp
        raise FormulaError(f"unsupported comparison operator: {operator}")

    def _call(self, name: str, args: Sequence[Any]) -> Any:
        function = name.rsplit(".", 1)[-1].casefold()
        if function == "if":
            if len(args) not in (2, 3):
                raise FormulaError("IF expects two or three arguments")
            return args[1] if bool(args[0]) else (args[2] if len(args) == 3 else False)
        if function == "char":
            if len(args) != 1:
                raise FormulaError("CHAR expects one argument")
            return chr(int(args[0]))
        if function == "lower":
            if len(args) != 1:
                raise FormulaError("LOWER expects one argument")
            return excel_text(args[0]).lower()
        if function == "xlookup":
            if len(args) not in (3, 4):
                raise FormulaError("XLOOKUP expects three or four arguments")
            lookup, keys, values = args[:3]
            if not isinstance(keys, list) or not isinstance(values, list):
                raise FormulaError("XLOOKUP key and return arguments must be ranges")
            if len(keys) != len(values):
                raise FormulaError("XLOOKUP ranges have different lengths")
            for key, value in zip(keys, values):
                if self._compare(key, "=", lookup):
                    return value
            if len(args) == 4:
                return args[3]
            raise FormulaError(f"XLOOKUP value not found: {lookup!r}")
        raise FormulaError(f"unsupported function: {name}")
