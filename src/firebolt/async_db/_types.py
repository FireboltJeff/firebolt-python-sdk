from __future__ import annotations

from collections import namedtuple
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import List, Sequence, Union

from sqlparse import parse as parse_sql  # type: ignore
from sqlparse.sql import Statement, Token, TokenList  # type: ignore
from sqlparse.tokens import Token as TokenType  # type: ignore

try:
    from ciso8601 import parse_datetime  # type: ignore
except ImportError:
    parse_datetime = datetime.fromisoformat  # type: ignore


from firebolt.common.exception import DataError, NotSupportedError
from firebolt.common.util import cached_property

_NoneType = type(None)
_col_types = (int, float, str, datetime, date, bool, list, _NoneType)
# duplicating this since 3.7 can't unpack Union
ColType = Union[int, float, str, datetime, date, bool, list, _NoneType]
RawColType = Union[int, float, str, bool, list, _NoneType]
ParameterType = Union[int, float, str, datetime, date, bool, Sequence]

# These definitions are required by PEP-249
Date = date


def DateFromTicks(t: int) -> date:
    """Convert ticks to date for firebolt db."""
    return datetime.fromtimestamp(t).date()


def Time(hour: int, minute: int, second: int) -> None:
    """Unsupported: construct time for firebolt db."""
    raise NotSupportedError("time is not supported by Firebolt")


def TimeFromTicks(t: int) -> None:
    """Unsupported: convert ticks to time for firebolt db."""
    raise NotSupportedError("time is not supported by Firebolt")


Timestamp = datetime
TimestampFromTicks = datetime.fromtimestamp


def Binary(value: str) -> str:
    """Convert string to binary for firebolt db, does nothing."""
    return value


STRING = BINARY = str
NUMBER = int
DATETIME = datetime
ROWID = int

Column = namedtuple(
    "Column",
    (
        "name",
        "type_code",
        "display_size",
        "internal_size",
        "precision",
        "scale",
        "null_ok",
    ),
)


class ARRAY:
    """Class for holding information about array column type in firebolt db."""

    _prefix = "Array("

    def __init__(self, subtype: Union[type, ARRAY]):
        assert (subtype in _col_types and subtype is not list) or isinstance(
            subtype, ARRAY
        ), f"Invalid array subtype: {str(subtype)}"
        self.subtype = subtype

    def __str__(self) -> str:
        return f"Array({str(self.subtype)})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ARRAY):
            return NotImplemented
        return other.subtype == self.subtype


NULLABLE_PREFIX = "Nullable("


class _InternalType(Enum):
    """Enum of all internal firebolt types except for array."""

    # INT, INTEGER
    Int8 = "Int8"
    UInt8 = "UInt8"
    Int16 = "Int16"
    UInt16 = "UInt16"
    Int32 = "Int32"
    UInt32 = "UInt32"

    # BIGINT, LONG
    Int64 = "Int64"
    UInt64 = "UInt64"

    # FLOAT
    Float32 = "Float32"

    # DOUBLE, DOUBLE PRECISION
    Float64 = "Float64"

    # VARCHAR, TEXT, STRING
    String = "String"

    # DATE
    Date = "Date"

    # DATETIME, TIMESTAMP
    DateTime = "DateTime"

    # Nullable(Nothing)
    Nothing = "Nothing"

    @cached_property
    def python_type(self) -> type:
        """Convert internal type to python type."""
        types = {
            _InternalType.Int8: int,
            _InternalType.UInt8: int,
            _InternalType.Int16: int,
            _InternalType.UInt16: int,
            _InternalType.Int32: int,
            _InternalType.UInt32: int,
            _InternalType.Int64: int,
            _InternalType.UInt64: int,
            _InternalType.Float32: float,
            _InternalType.Float64: float,
            _InternalType.String: str,
            _InternalType.Date: date,
            _InternalType.DateTime: datetime,
            # For simplicity, this could happen only during 'select null' query
            _InternalType.Nothing: str,
        }
        return types[self]


def parse_type(raw_type: str) -> Union[type, ARRAY]:
    """Parse typename, provided by query metadata into python type."""
    if not isinstance(raw_type, str):
        raise DataError(f"Invalid typename {str(raw_type)}: str expected")
    # Handle arrays
    if raw_type.startswith(ARRAY._prefix) and raw_type.endswith(")"):
        return ARRAY(parse_type(raw_type[len(ARRAY._prefix) : -1]))
    # Handle nullable
    if raw_type.startswith(NULLABLE_PREFIX) and raw_type.endswith(")"):
        return parse_type(raw_type[len(NULLABLE_PREFIX) : -1])

    try:
        return _InternalType(raw_type).python_type
    except ValueError:
        # Treat unknown types as strings. Better that error since user still has
        # a way to work with it
        return str


def parse_value(
    value: RawColType,
    ctype: Union[type, ARRAY],
) -> ColType:
    """Provided raw value and python type, parses first into python value."""
    if value is None:
        return None
    if ctype in (int, str, float):
        assert isinstance(ctype, type)
        return ctype(value)
    if ctype is date:
        if not isinstance(value, str):
            raise DataError(f"Invalid date value {value}: str expected")
        assert isinstance(value, str)
        return parse_datetime(value).date()
    if ctype is datetime:
        if not isinstance(value, str):
            raise DataError(f"Invalid datetime value {value}: str expected")
        return parse_datetime(value)
    if isinstance(ctype, ARRAY):
        assert isinstance(value, list)
        return [parse_value(it, ctype.subtype) for it in value]
    raise DataError(f"Unsupported data type returned: {ctype.__name__}")


escape_chars = {
    "\0": "\\0",
    "\\": "\\\\",
    "'": "\\'",
}


def format_value(value: ParameterType) -> str:
    """For python value to be used in a SQL query"""
    if isinstance(value, bool):
        return str(int(value))
    if isinstance(value, (int, float, Decimal)):
        return str(value)
    elif isinstance(value, str):
        return f"'{''.join(escape_chars.get(c, c) for c in value)}'"
    elif isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc)
        return f"'{value.strftime('%Y-%m-%d %H:%M:%S')}'"
    elif isinstance(value, date):
        return f"'{value.isoformat()}'"
    if value is None:
        return "NULL"
    elif isinstance(value, Sequence):
        return f"[{', '.join(format_value(it) for it in value)}]"

    raise DataError(f"unsupported parameter type {type(value)}")


def format_statement(statement: Statement, parameters: Sequence[ParameterType]) -> str:
    """
    Substitute placeholders in a sqlparse statement with provided values.
    """
    idx = 0

    def process_token(token: Token) -> Token:
        nonlocal idx
        if token.ttype == TokenType.Name.Placeholder:
            # Replace placeholder with formatted parameter
            if idx >= len(parameters):
                raise DataError(
                    "not enough parameters provided for substitution: given "
                    f"{len(parameters)}, found one more"
                )
            formatted = format_value(parameters[idx])
            idx += 1
            return Token(TokenType.Text, formatted)
        if isinstance(token, TokenList):
            # Process all children tokens

            return TokenList([process_token(t) for t in token.tokens])
        return token

    formatted_sql = str(process_token(statement)).rstrip(";")

    if idx < len(parameters):
        raise DataError(
            f"too many parameters provided for substitution: given {len(parameters)}, "
            f"used only {idx}"
        )

    return formatted_sql


def split_format_sql(
    query: str, parameters: Sequence[Sequence[ParameterType]]
) -> List[str]:
    """
    Split a query into separate statement, and format it with parameters
    if it's a single statement
    Trying to format a multi-statement query would result in NotSupportedError
    """
    statements = parse_sql(query)
    if not statements:
        return [query]

    if parameters:
        if len(statements) > 1:
            raise NotSupportedError(
                "formatting multistatement queries is not supported"
            )
        return [format_statement(statements[0], paramset) for paramset in parameters]
    return [str(st).strip().rstrip(";") for st in statements]
