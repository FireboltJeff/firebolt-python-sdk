from __future__ import annotations

import logging
import re
import time
from enum import Enum
from functools import wraps
from inspect import cleandoc
from types import TracebackType
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Sequence,
    Tuple,
)

from aiorwlock import RWLock
from httpx import Response, codes

from firebolt.async_db._types import (
    ColType,
    Column,
    ParameterType,
    RawColType,
    parse_type,
    parse_value,
    split_format_sql,
)
from firebolt.async_db.util import is_db_available, is_engine_running
from firebolt.client import AsyncClient
from firebolt.common.exception import (
    CursorClosedError,
    DataError,
    EngineNotRunningError,
    FireboltDatabaseError,
    OperationalError,
    ProgrammingError,
    QueryNotRunError,
)

if TYPE_CHECKING:
    from firebolt.async_db.connection import Connection

logger = logging.getLogger(__name__)


JSON_OUTPUT_FORMAT = "JSONCompact"


class CursorState(Enum):
    NONE = 1
    ERROR = 2
    DONE = 3
    CLOSED = 4


def check_not_closed(func: Callable) -> Callable:
    """(Decorator) ensure cursor is not closed before calling method."""

    @wraps(func)
    def inner(self: Cursor, *args: Any, **kwargs: Any) -> Any:
        if self.closed:
            raise CursorClosedError(method_name=func.__name__)
        return func(self, *args, **kwargs)

    return inner


def check_query_executed(func: Callable) -> Callable:
    cleandoc(
        """
        (Decorator) ensure that some query has been executed before
        calling cursor method.
        """
    )

    @wraps(func)
    def inner(self: Cursor, *args: Any, **kwargs: Any) -> Any:
        if self._state == CursorState.NONE:
            raise QueryNotRunError(method_name=func.__name__)
        return func(self, *args, **kwargs)

    return inner


class BaseCursor:
    __slots__ = (
        "connection",
        "_arraysize",
        "_client",
        "_state",
        "_descriptions",
        "_rowcount",
        "_rows",
        "_idx",
        "_idx_lock",
        "_row_sets",
        "_next_set_idx",
    )

    default_arraysize = 1

    def __init__(self, client: AsyncClient, connection: Connection):
        self.connection = connection
        self._client = client
        self._arraysize = self.default_arraysize
        # These fields initialized here for type annotations purpose
        self._rows: Optional[List[List[RawColType]]] = None
        self._descriptions: Optional[List[Column]] = None
        self._row_sets: List[
            Tuple[int, Optional[List[Column]], Optional[List[List[RawColType]]]]
        ] = []
        self._rowcount = -1
        self._idx = 0
        self._next_set_idx = 0
        self._reset()

    def __del__(self) -> None:
        self.close()

    @property  # type: ignore
    @check_not_closed
    def description(self) -> Optional[List[Column]]:
        """
        Provides information about a single result row of a query

        Attributes:
            * ``name``
            * ``type_code``
            * ``display_size``
            * ``internal_size``
            * ``precision``
            * ``scale``
            * ``null_ok``
        """
        return self._descriptions

    @property  # type: ignore
    @check_not_closed
    def rowcount(self) -> int:
        """The number of rows produced by last query."""
        return self._rowcount

    @property
    def arraysize(self) -> int:
        """Default number of rows returned by fetchmany."""
        return self._arraysize

    @arraysize.setter
    def arraysize(self, value: int) -> None:
        if not isinstance(value, int):
            raise TypeError(
                "Invalid arraysize value type, expected int,"
                f" got {type(value).__name__}"
            )
        self._arraysize = value

    @property
    def closed(self) -> bool:
        """True if connection is closed, False otherwise."""

        return self._state == CursorState.CLOSED

    def close(self) -> None:
        """Terminate an ongoing query (if any) and mark connection as closed."""

        self._state = CursorState.CLOSED
        # remove typecheck skip  after connection is implemented
        self.connection._remove_cursor(self)  # type: ignore

    def _append_query_data(self, response: Response) -> None:
        """Store information about executed query from httpx response."""

        row_set: Tuple[
            int, Optional[List[Column]], Optional[List[List[RawColType]]]
        ] = (-1, None, None)

        # Empty response is returned for insert query
        if response.headers.get("content-length", "") != "0":
            try:
                query_data = response.json()
                rowcount = int(query_data["rows"])
                descriptions = [
                    Column(
                        d["name"], parse_type(d["type"]), None, None, None, None, None
                    )
                    for d in query_data["meta"]
                ]

                # Parse data during fetch
                rows = query_data["data"]
                row_set = (rowcount, descriptions, rows)
            except (KeyError, ValueError) as err:
                raise DataError(f"Invalid query data format: {str(err)}")

        self._row_sets.append(row_set)
        if self._next_set_idx == 0:
            # Populate values for first set
            self._pop_next_set()

    @check_not_closed
    @check_query_executed
    def nextset(self) -> Optional[bool]:
        """
        Skip to the next available set, discarding any remaining rows
        from the current set.
        Returns True if operation was successful,
        None if there are no more sets to retrive
        """
        return self._pop_next_set()

    def _pop_next_set(self) -> Optional[bool]:
        """
        Same functionality as .nextset, but doesn't check that query has been executed.
        """
        if self._next_set_idx >= len(self._row_sets):
            return None
        self._rowcount, self._descriptions, self._rows = self._row_sets[
            self._next_set_idx
        ]
        self._idx = 0
        self._next_set_idx += 1
        return True

    async def _raise_if_error(self, resp: Response) -> None:
        """Raise a proper error if any"""
        if resp.status_code == codes.INTERNAL_SERVER_ERROR:
            raise OperationalError(
                f"Error executing query:\n{resp.read().decode('utf-8')}"
            )
        if resp.status_code == codes.FORBIDDEN:
            if not await is_db_available(self.connection, self.connection.database):
                raise FireboltDatabaseError(
                    f"Database {self.connection.database} does not exist"
                )
            raise ProgrammingError(resp.read().decode("utf-8"))
        if (
            resp.status_code == codes.SERVICE_UNAVAILABLE
            or resp.status_code == codes.NOT_FOUND
        ):
            if not await is_engine_running(self.connection, self.connection.engine_url):
                raise EngineNotRunningError(
                    f"Firebolt engine {self.connection.engine_url} "
                    "needs to be running to run queries against it."
                )
        resp.raise_for_status()

    def _reset(self) -> None:
        """Clear all data stored from previous query."""
        self._state = CursorState.NONE
        self._rows = None
        self._descriptions = None
        self._rowcount = -1
        self._idx = 0
        self._row_sets = []
        self._next_set_idx = 0

    async def _do_execute_request(
        self,
        query: str,
        parameters: Sequence[Sequence[ParameterType]],
        set_parameters: Optional[Dict] = None,
    ) -> None:
        self._reset()
        try:

            queries = split_format_sql(query, parameters)

            for query in queries:

                start_time = time.time()
                # our CREATE EXTERNAL TABLE queries currently require credentials,
                # so we will skip logging those queries.
                # https://docs.firebolt.io/sql-reference/commands/ddl-commands#create-external-table
                if not re.search("aws_key_id|credentials", query, flags=re.IGNORECASE):
                    logger.debug(f"Running query: {query}")

                resp = await self._client.request(
                    url="/",
                    method="POST",
                    params={
                        "database": self.connection.database,
                        "output_format": JSON_OUTPUT_FORMAT,
                        **(set_parameters or dict()),
                    },
                    content=query,
                )

                await self._raise_if_error(resp)
                self._append_query_data(resp)
                logger.info(
                    f"Query fetched {self.rowcount} rows in"
                    f" {time.time() - start_time} seconds"
                )

            self._state = CursorState.DONE

        except Exception:
            self._state = CursorState.ERROR
            raise

    @check_not_closed
    async def execute(
        self,
        query: str,
        parameters: Optional[Sequence[ParameterType]] = None,
        set_parameters: Optional[Dict] = None,
    ) -> int:
        """Prepare and execute a database query. Return row count."""

        params_list = [parameters] if parameters else []
        await self._do_execute_request(query, params_list, set_parameters)
        return self.rowcount

    @check_not_closed
    async def executemany(
        self, query: str, parameters_seq: Sequence[Sequence[ParameterType]]
    ) -> int:
        """
        Prepare and execute a database query against all parameter
        sequences provided. Return last query row count.
        """
        await self._do_execute_request(query, parameters_seq)
        return self.rowcount

    def _parse_row(self, row: List[RawColType]) -> List[ColType]:
        """Parse a single data row based on query column types"""
        assert len(row) == len(self.description)
        return [
            parse_value(col, self.description[i].type_code) for i, col in enumerate(row)
        ]

    def _get_next_range(self, size: int) -> Tuple[int, int]:
        cleandoc(
            """
            Return range of next rows of size (if possible),
            and update _idx to point to the end of this range
            """
        )

        if self._rows is None:
            # No elements to take
            raise DataError("no rows to fetch")

        left = self._idx
        right = min(self._idx + size, len(self._rows))
        self._idx = right
        return left, right

    @check_not_closed
    @check_query_executed
    def fetchone(self) -> Optional[List[ColType]]:
        """Fetch the next row of a query result set."""
        left, right = self._get_next_range(1)
        if left == right:
            # We are out of elements
            return None
        assert self._rows is not None
        return self._parse_row(self._rows[left])

    @check_not_closed
    @check_query_executed
    def fetchmany(self, size: Optional[int] = None) -> List[List[ColType]]:
        """
        Fetch the next set of rows of a query result,
        cursor.arraysize is default size.
        """
        size = size if size is not None else self.arraysize
        left, right = self._get_next_range(size)
        assert self._rows is not None
        rows = self._rows[left:right]
        return [self._parse_row(row) for row in rows]

    @check_not_closed
    @check_query_executed
    def fetchall(self) -> List[List[ColType]]:
        """Fetch all remaining rows of a query result."""
        left, right = self._get_next_range(self.rowcount)
        assert self._rows is not None
        rows = self._rows[left:right]
        return [self._parse_row(row) for row in rows]

    @check_not_closed
    def setinputsizes(self, sizes: List[int]) -> None:
        """Predefine memory areas for query parameters (does nothing)."""

    @check_not_closed
    def setoutputsize(self, size: int, column: Optional[int] = None) -> None:
        """Set a column buffer size for fetches of large columns (does nothing)."""

    # Context manager support
    @check_not_closed
    def __enter__(self) -> BaseCursor:
        return self

    def __exit__(
        self, exc_type: type, exc_val: Exception, exc_tb: TracebackType
    ) -> None:
        self.close()


class Cursor(BaseCursor):
    """
    Class, responsible for executing asyncio queries to Firebolt Database.
    Should not be created directly,
    use :py:func:`connection.cursor <firebolt.async_db.connection.Connection>`

    Args:
        description: information about a single result row
        rowcount: the number of rows produced by last query
        closed: True if connection is closed, False otherwise
        arraysize: Read/Write, specifies the number of rows to fetch at a time
            with the :py:func:`fetchmany` method

    """

    __slots__ = BaseCursor.__slots__ + ("_async_query_lock",)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._async_query_lock = RWLock()
        super().__init__(*args, **kwargs)

    @wraps(BaseCursor.execute)
    async def execute(
        self,
        query: str,
        parameters: Optional[Sequence[ParameterType]] = None,
        set_parameters: Optional[Dict] = None,
    ) -> int:
        async with self._async_query_lock.writer:
            return await super().execute(query, parameters, set_parameters)
        """Prepare and execute a database query"""

    @wraps(BaseCursor.executemany)
    async def executemany(
        self, query: str, parameters_seq: Sequence[Sequence[ParameterType]]
    ) -> int:
        async with self._async_query_lock.writer:
            return await super().executemany(query, parameters_seq)
        """
            Prepare and execute a database query against all parameter
            sequences provided
        """

    @wraps(BaseCursor.fetchone)
    async def fetchone(self) -> Optional[List[ColType]]:
        async with self._async_query_lock.reader:
            return super().fetchone()
        """Fetch the next row of a query result set"""

    @wraps(BaseCursor.fetchmany)
    async def fetchmany(self, size: Optional[int] = None) -> List[List[ColType]]:
        async with self._async_query_lock.reader:
            return super().fetchmany(size)
        """fetch the next set of rows of a query result,
          size is cursor.arraysize by default"""

    @wraps(BaseCursor.fetchall)
    async def fetchall(self) -> List[List[ColType]]:
        async with self._async_query_lock.reader:
            return super().fetchall()
        """Fetch all remaining rows of a query result"""

    @wraps(BaseCursor.nextset)
    async def nextset(self) -> None:
        async with self._async_query_lock.reader:
            return super().nextset()

    # Iteration support
    @check_not_closed
    @check_query_executed
    def __aiter__(self) -> Cursor:
        return self

    @check_not_closed
    @check_query_executed
    async def __anext__(self) -> List[ColType]:
        row = await self.fetchone()
        if row is None:
            raise StopAsyncIteration
        return row
