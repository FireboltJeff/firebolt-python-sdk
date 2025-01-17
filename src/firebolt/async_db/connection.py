from __future__ import annotations

import socket
from json import JSONDecodeError
from types import TracebackType
from typing import Any, Callable, List, Optional, Type

from httpcore.backends.auto import AutoBackend
from httpcore.backends.base import AsyncNetworkStream
from httpx import AsyncHTTPTransport, HTTPStatusError, RequestError, Timeout

from firebolt.async_db.cursor import BaseCursor, Cursor
from firebolt.client import DEFAULT_API_URL, AsyncClient
from firebolt.common.exception import (
    ConnectionClosedError,
    FireboltEngineError,
    InterfaceError,
)
from firebolt.common.urls import ACCOUNT_ENGINE_BY_NAME_URL, ACCOUNT_ENGINE_URL
from firebolt.common.util import fix_url_schema

DEFAULT_TIMEOUT_SECONDS: int = 5
KEEPALIVE_FLAG: int = 1
KEEPIDLE_RATE: int = 60  # seconds


async def _resolve_engine_url(
    engine_name: str,
    username: str,
    password: str,
    api_endpoint: str,
    account_name: Optional[str] = None,
) -> str:
    async with AsyncClient(
        auth=(username, password),
        base_url=api_endpoint,
        account_name=account_name,
        api_endpoint=api_endpoint,
    ) as client:
        try:
            account_id = await client.account_id
            response = await client.get(
                url=ACCOUNT_ENGINE_BY_NAME_URL.format(account_id=account_id),
                params={"engine_name": engine_name},
            )
            response.raise_for_status()
            engine_id = response.json()["engine_id"]["engine_id"]
            response = await client.get(
                url=ACCOUNT_ENGINE_URL.format(
                    account_id=account_id, engine_id=engine_id
                ),
            )
            response.raise_for_status()
            return response.json()["engine"]["endpoint"]
        except HTTPStatusError as e:
            # Engine error would be 404.
            if e.response.status_code != 404:
                raise InterfaceError(f"Unable to retrieve engine endpoint: {e}.")
            # Once this is point is reached we've already authenticated with
            # the backend so it's safe to assume the cause of the error is
            # missing engine.
            raise FireboltEngineError(f"Firebolt engine {engine_name} does not exist.")
        except (JSONDecodeError, RequestError, RuntimeError, HTTPStatusError) as e:
            raise InterfaceError(f"Unable to retrieve engine endpoint: {e}.")


def async_connect_factory(connection_class: Type) -> Callable:
    async def connect_inner(
        database: str = None,
        username: str = None,
        password: str = None,
        engine_name: Optional[str] = None,
        engine_url: Optional[str] = None,
        account_name: Optional[str] = None,
        api_endpoint: str = DEFAULT_API_URL,
    ) -> Connection:
        """
        Connect to Firebolt database.

        Args:
            database: name of the database to connect
            username: user name to use for authentication
            password: password to use for authentication
            engine_name: Optional The name of the engine to connect to
            engine_url: Optional. The engine endpoint to use
            account_name: For customers with multiple accounts; if None uses default.
            api_endpoint(optional): Firebolt API endpoint. Used for authentication.

        Note:
            Either `engine_name` or `engine_url` should be provided, but not both.

        """

        if engine_name and engine_url:
            raise InterfaceError(
                "Both engine_name and engine_url are provided. "
                "Provide only one to connect."
            )
        if not engine_name and not engine_url:
            raise InterfaceError(
                "Neither engine_name nor engine_url is provided. "
                "Provide one to connect."
            )

        api_endpoint = fix_url_schema(api_endpoint)
        # These parameters are optional in function signature
        # but are required to connect.
        # PEP 249 recommends making them kwargs.
        for param, name in (
            (database, "database"),
            (username, "username"),
            (password, "password"),
        ):
            if not param:
                raise InterfaceError(f"{name} is required to connect.")

        # Mypy checks, this should never happen
        assert database is not None
        assert username is not None
        assert password is not None

        if engine_name:
            engine_url = await _resolve_engine_url(
                engine_name=engine_name,
                username=username,
                password=password,
                account_name=account_name,
                api_endpoint=api_endpoint,
            )

        assert engine_url is not None

        engine_url = fix_url_schema(engine_url)
        return connection_class(engine_url, database, username, password, api_endpoint)

    return connect_inner


class OverriddenHttpBackend(AutoBackend):
    """
    This class is a short-term solution for TCP keep-alive issue:
    https://docs.aws.amazon.com/elasticloadbalancing/latest/network/network-load-balancers.html#connection-idle-timeout
    Since httpx creates a connection right before executing a request
    backend has to be overridden in order to set the socket KEEPALIVE
    and KEEPIDLE settings.
    """

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: Optional[float] = None,
        local_address: Optional[str] = None,
    ) -> AsyncNetworkStream:
        stream = await super().connect_tcp(
            host, port, timeout=timeout, local_address=local_address
        )
        # Enable keepalive
        stream.get_extra_info("socket").setsockopt(
            socket.SOL_SOCKET, socket.SO_KEEPALIVE, KEEPALIVE_FLAG
        )
        # MacOS does not have TCP_KEEPIDLE
        if hasattr(socket, "TCP_KEEPIDLE"):
            keepidle = socket.TCP_KEEPIDLE
        else:
            keepidle = 0x10  # TCP_KEEPALIVE on mac

        # Set keepalive to 60 seconds
        stream.get_extra_info("socket").setsockopt(
            socket.IPPROTO_TCP, keepidle, KEEPIDLE_RATE
        )
        return stream


class BaseConnection:
    client_class: type
    cursor_class: type
    __slots__ = (
        "_client",
        "_cursors",
        "database",
        "engine_url",
        "api_endpoint",
        "_is_closed",
    )

    def __init__(
        self,
        engine_url: str,
        database: str,  # TODO: Get by engine name
        username: str,
        password: str,
        api_endpoint: str = DEFAULT_API_URL,
    ):
        transport = AsyncHTTPTransport()
        transport._pool._network_backend = OverriddenHttpBackend()
        self._client = AsyncClient(
            auth=(username, password),
            base_url=engine_url,
            api_endpoint=api_endpoint,
            timeout=Timeout(DEFAULT_TIMEOUT_SECONDS, read=None),
            transport=transport,
        )
        self.api_endpoint = api_endpoint
        self.engine_url = engine_url
        self.database = database
        self._cursors: List[BaseCursor] = []
        self._is_closed = False

    def _cursor(self, **kwargs: Any) -> BaseCursor:
        """
        Create new cursor object.
        """

        if self.closed:
            raise ConnectionClosedError("Unable to create cursor: connection closed")

        c = self.cursor_class(self._client, self, **kwargs)
        self._cursors.append(c)
        return c

    async def _aclose(self) -> None:
        """Close connection and all underlying cursors."""
        if self.closed:
            return

        # self._cursors is going to be changed during closing cursors
        # after this point no cursors would be added to _cursors, only removed since
        # closing lock is held, and later connection will be marked as closed
        cursors = self._cursors[:]
        for c in cursors:
            # Here c can already be closed by another thread,
            # but it shouldn't raise an error in this case
            c.close()
        await self._client.aclose()
        self._is_closed = True

    @property
    def closed(self) -> bool:
        """True if connection is closed, False otherwise."""
        return self._is_closed

    def _remove_cursor(self, cursor: Cursor) -> None:
        # This way it's atomic
        try:
            self._cursors.remove(cursor)
        except ValueError:
            pass

    def commit(self) -> None:
        """Does nothing since Firebolt doesn't have transactions"""

        if self.closed:
            raise ConnectionClosedError("Unable to commit: connection closed")


class Connection(BaseConnection):
    """
    Firebolt asyncronous database connection class. Implements `PEP 249`_.

    Args:
        engine_url: Firebolt database engine REST API url
        database: Firebolt database name
        username: Firebolt account username
        password: Firebolt account password
        api_endpoint: Optional. Firebolt API endpoint. Used for authentication.

    Note:
        Firebolt currenly doesn't support transactions
        so commit and rollback methods are not implemented.

    .. _PEP 249:
        https://www.python.org/dev/peps/pep-0249/

    """

    cursor_class = Cursor

    aclose = BaseConnection._aclose

    def cursor(self) -> Cursor:
        c = super()._cursor()
        assert isinstance(c, Cursor)  # typecheck
        return c

    # Context manager support
    async def __aenter__(self) -> Connection:
        if self.closed:
            raise ConnectionClosedError("Connection is already closed")
        return self

    async def __aexit__(
        self, exc_type: type, exc_val: Exception, exc_tb: TracebackType
    ) -> None:
        await self._aclose()


connect = async_connect_factory(Connection)
