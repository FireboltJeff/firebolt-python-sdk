from time import time

from pytest import mark, raises

from firebolt.common.exception import AuthenticationError
from firebolt.db import Connection


@mark.skip(reason="flaky, token not updated each time")
def test_refresh_token(connection: Connection) -> None:
    """Auth refreshes token on expiration/invalidation"""
    with connection.cursor() as c:
        # Works fine
        c.execute("show tables")

        # Invalidate the token
        c._client.auth._token += "_"

        # Still works fine
        c.execute("show tables")

        old = c._client.auth.token
        c._client.auth._expires = int(time()) - 1

        # Still works fine
        c.execute("show tables")

        assert c._client.auth.token != old, "Auth didn't update token on expiration"


def test_credentials_invalidation(connection: Connection) -> None:
    """Auth raises Authentication Error on credentials invalidation"""
    with connection.cursor() as c:
        # Works fine
        c.execute("show tables")

        # Invalidate the token
        c._client.auth._token += "_"
        c._client.auth.username += "_"
        c._client.auth.password += "_"

        with raises(AuthenticationError) as exc_info:
            c.execute("show tables")

        assert str(exc_info.value).startswith(
            "Failed to authenticate"
        ), "Invalid authentication error message"
