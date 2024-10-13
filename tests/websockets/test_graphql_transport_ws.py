from __future__ import annotations

import asyncio
import contextlib
import json
import time
from datetime import timedelta
from typing import TYPE_CHECKING, Any, AsyncGenerator, Dict, Optional, Type
from unittest.mock import AsyncMock, Mock, patch

import pytest
import pytest_asyncio
from pytest_mock import MockerFixture

from strawberry.subscriptions import GRAPHQL_TRANSPORT_WS_PROTOCOL
from strawberry.subscriptions.protocols.graphql_transport_ws.handlers import (
    BaseGraphQLTransportWSHandler,
)
from strawberry.subscriptions.protocols.graphql_transport_ws.types import (
    CompleteMessage,
    ConnectionAckMessage,
    ConnectionInitMessage,
    ErrorMessage,
    NextMessage,
    PingMessage,
    PongMessage,
    SubscribeMessage,
    SubscribeMessagePayload,
)
from tests.http.clients.base import DebuggableGraphQLTransportWSHandler
from tests.views.schema import MyExtension, Schema

from ..http.clients.base import WebSocketClient

try:
    from ..http.clients.fastapi import FastAPIHttpClient
except ImportError:  # pragma: no cover
    FastAPIHttpClient = None
try:
    from ..http.clients.starlite import StarliteHttpClient
except ImportError:  # pragma: no cover
    StarliteHttpClient = None
try:
    from ..http.clients.litestar import LitestarHttpClient
except ImportError:  # pragma: no cover
    LitestarHttpClient = None

if TYPE_CHECKING:
    from ..http.clients.base import HttpClient


@pytest_asyncio.fixture
async def ws_raw(http_client: HttpClient) -> AsyncGenerator[WebSocketClient, None]:
    async with http_client.ws_connect(
        "/graphql", protocols=[GRAPHQL_TRANSPORT_WS_PROTOCOL]
    ) as ws:
        yield ws
        await ws.close()
        assert ws.closed


@pytest_asyncio.fixture
async def ws(ws_raw: WebSocketClient) -> WebSocketClient:
    await ws_raw.send_json(ConnectionInitMessage().as_dict())
    response = await ws_raw.receive_json()
    assert response == ConnectionAckMessage().as_dict()
    return ws_raw


def assert_next(
    response: dict[str, Any],
    id: str,
    data: Dict[str, Any],
    extensions: Optional[Dict[str, Any]] = None,
):
    """
    Assert that the NextMessage payload contains the provided data.
    If extensions is provided, it will also assert that the
    extensions are present
    """
    assert response["type"] == "next"
    assert response["id"] == id
    assert set(response["payload"].keys()) <= {"data", "errors", "extensions"}
    assert response["payload"]["data"] == data
    if extensions is not None:
        assert response["payload"]["extensions"] == extensions


async def test_unknown_message_type(ws_raw: WebSocketClient):
    ws = ws_raw

    await ws.send_json({"type": "NOT_A_MESSAGE_TYPE"})

    data = await ws.receive(timeout=2)
    assert ws.closed
    assert ws.close_code == 4400
    assert ws.close_reason == "Unknown message type: NOT_A_MESSAGE_TYPE"


async def test_missing_message_type(ws_raw: WebSocketClient):
    ws = ws_raw

    await ws.send_json({"notType": None})

    data = await ws.receive(timeout=2)
    assert ws.closed
    assert ws.close_code == 4400
    assert ws.close_reason == "Failed to parse message"


async def test_parsing_an_invalid_message(ws_raw: WebSocketClient):
    ws = ws_raw

    await ws.send_json({"type": "subscribe", "notPayload": None})

    data = await ws.receive(timeout=2)
    assert ws.closed
    assert ws.close_code == 4400
    assert ws.close_reason == "Failed to parse message"


async def test_parsing_an_invalid_payload(ws_raw: WebSocketClient):
    ws = ws_raw

    await ws.send_json({"type": "subscribe", "payload": {"unexpectedField": 42}})

    data = await ws.receive(timeout=2)
    assert ws.closed
    assert ws.close_code == 4400
    assert ws.close_reason == "Failed to parse message"


async def test_ws_messages_must_be_text(ws_raw: WebSocketClient):
    ws = ws_raw

    await ws.send_bytes(json.dumps(ConnectionInitMessage().as_dict()).encode())

    await ws.receive(timeout=2)
    assert ws.closed
    assert ws.close_code == 4400
    assert ws.close_reason == "WebSocket message type must be text"


async def test_ws_messages_must_be_json(ws_raw: WebSocketClient):
    ws = ws_raw

    await ws.send_text("not valid json")

    await ws.receive(timeout=2)
    assert ws.closed
    assert ws.close_code == 4400
    assert ws.close_reason == "WebSocket message type must be text"


async def test_ws_message_frame_types_cannot_be_mixed(ws_raw: WebSocketClient):
    ws = ws_raw

    await ws.send_json(ConnectionInitMessage().as_dict())

    response = await ws.receive_json()
    assert response == ConnectionAckMessage().as_dict()

    await ws.send_bytes(
        json.dumps(
            SubscribeMessage(
                id="sub1",
                payload=SubscribeMessagePayload(
                    query="subscription { debug { isConnectionInitTimeoutTaskDone } }"
                ),
            ).as_dict()
        ).encode()
    )

    await ws.receive(timeout=2)
    assert ws.closed
    assert ws.close_code == 4400
    assert ws.close_reason == "WebSocket message type must be text"


async def test_connection_init_timeout(
    request: Any, http_client_class: Type[HttpClient]
):
    with contextlib.suppress(ImportError):
        from tests.http.clients.aiohttp import AioHttpClient

        if http_client_class == AioHttpClient:
            pytest.skip(
                "Closing a AIOHTTP WebSocket from a "
                "task currently doesn't work as expected"
            )

    test_client = http_client_class()
    test_client.create_app(connection_init_wait_timeout=timedelta(seconds=0))

    async with test_client.ws_connect(
        "/graphql", protocols=[GRAPHQL_TRANSPORT_WS_PROTOCOL]
    ) as ws:
        data = await ws.receive(timeout=2)
        assert ws.closed
        assert ws.close_code == 4408
        assert ws.close_reason == "Connection initialisation timeout"


@pytest.mark.flaky
async def test_connection_init_timeout_cancellation(
    ws_raw: WebSocketClient,
):
    # Verify that the timeout task is cancelled after the connection Init
    # message is received
    ws = ws_raw
    await ws.send_json(ConnectionInitMessage().as_dict())

    response = await ws.receive_json()
    assert response == ConnectionAckMessage().as_dict()

    await ws.send_json(
        SubscribeMessage(
            id="sub1",
            payload=SubscribeMessagePayload(
                query="subscription { debug { isConnectionInitTimeoutTaskDone } }"
            ),
        ).as_dict()
    )

    response = await ws.receive_json()
    assert_next(response, "sub1", {"debug": {"isConnectionInitTimeoutTaskDone": True}})


@pytest.mark.xfail(reason="This test is flaky")
async def test_close_twice(
    mocker: MockerFixture, request: Any, http_client_class: Type[HttpClient]
):
    test_client = http_client_class()
    test_client.create_app(connection_init_wait_timeout=timedelta(seconds=0.25))

    async with test_client.ws_connect(
        "/graphql", protocols=[GRAPHQL_TRANSPORT_WS_PROTOCOL]
    ) as ws:
        transport_close = mocker.patch.object(ws, "close")

        # We set payload is set to "invalid value" to force a invalid payload error
        # which will close the connection
        await ws.send_json(
            ConnectionInitMessage(payload="invalid value").as_dict(),  # type: ignore
        )
        # Yield control so that ._close can be called
        await asyncio.sleep(0)

        for t in asyncio.all_tasks():
            if (
                t.get_coro().__qualname__
                == "BaseGraphQLTransportWSHandler.handle_connection_init_timeout"
            ):
                # The init timeout task should be cancelled
                with pytest.raises(asyncio.CancelledError):
                    await t

        await ws.receive(timeout=0.5)
        assert ws.closed
        assert ws.close_code == 4400
        assert ws.close_reason == "Invalid connection init payload"
        transport_close.assert_not_called()


async def test_too_many_initialisation_requests(ws: WebSocketClient):
    await ws.send_json(ConnectionInitMessage().as_dict())
    data = await ws.receive(timeout=2)
    assert ws.closed
    assert ws.close_code == 4429
    assert ws.close_reason == "Too many initialisation requests"


async def test_ping_pong(ws: WebSocketClient):
    await ws.send_json(PingMessage().as_dict())
    response = await ws.receive_json()
    assert response == PongMessage().as_dict()


async def test_can_send_payload_with_additional_things(ws_raw: WebSocketClient):
    ws = ws_raw

    # send  init

    await ws.send_json(ConnectionInitMessage().as_dict())

    await ws.receive(timeout=2)

    await ws.send_json(
        {
            "type": "subscribe",
            "payload": {
                "query": 'subscription { echo(message: "Hi") }',
                "some": "other thing",
            },
            "id": "1",
        }
    )

    data = await ws.receive(timeout=2)

    assert json.loads(data.data) == {
        "type": "next",
        "id": "1",
        "payload": {"data": {"echo": "Hi"}, "extensions": {"example": "example"}},
    }


async def test_server_sent_ping(ws: WebSocketClient):
    await ws.send_json(
        SubscribeMessage(
            id="sub1",
            payload=SubscribeMessagePayload(query="subscription { requestPing }"),
        ).as_dict()
    )

    response = await ws.receive_json()
    assert response == PingMessage().as_dict()

    await ws.send_json(PongMessage().as_dict())

    response = await ws.receive_json()
    assert_next(response, "sub1", {"requestPing": True})
    response = await ws.receive_json()
    assert response == CompleteMessage(id="sub1").as_dict()


async def test_unauthorized_subscriptions(ws_raw: WebSocketClient):
    ws = ws_raw
    await ws.send_json(
        SubscribeMessage(
            id="sub1",
            payload=SubscribeMessagePayload(
                query='subscription { echo(message: "Hi") }'
            ),
        ).as_dict()
    )

    data = await ws.receive(timeout=2)
    assert ws.closed
    assert ws.close_code == 4401
    assert ws.close_reason == "Unauthorized"


async def test_duplicated_operation_ids(ws: WebSocketClient):
    await ws.send_json(
        SubscribeMessage(
            id="sub1",
            payload=SubscribeMessagePayload(
                query='subscription { echo(message: "Hi", delay: 5) }'
            ),
        ).as_dict()
    )

    await ws.send_json(
        SubscribeMessage(
            id="sub1",
            payload=SubscribeMessagePayload(
                query='subscription { echo(message: "Hi", delay: 5) }'
            ),
        ).as_dict()
    )

    data = await ws.receive(timeout=2)
    assert ws.closed
    assert ws.close_code == 4409
    assert ws.close_reason == "Subscriber for sub1 already exists"


async def test_reused_operation_ids(ws: WebSocketClient):
    """Test that an operation id can be re-used after it has been
    previously used for a completed operation.
    """
    # Use sub1 as an id for an operation
    await ws.send_json(
        SubscribeMessage(
            id="sub1",
            payload=SubscribeMessagePayload(
                query='subscription { echo(message: "Hi") }'
            ),
        ).as_dict()
    )

    response = await ws.receive_json()
    assert_next(response, "sub1", {"echo": "Hi"})

    response = await ws.receive_json()
    assert response == CompleteMessage(id="sub1").as_dict()

    # operation is now complete.  Create a new operation using
    # the same ID
    await ws.send_json(
        SubscribeMessage(
            id="sub1",
            payload=SubscribeMessagePayload(
                query='subscription { echo(message: "Hi") }'
            ),
        ).as_dict()
    )

    response = await ws.receive_json()
    assert_next(response, "sub1", {"echo": "Hi"})


async def test_simple_subscription(ws: WebSocketClient):
    await ws.send_json(
        SubscribeMessage(
            id="sub1",
            payload=SubscribeMessagePayload(
                query='subscription { echo(message: "Hi") }'
            ),
        ).as_dict()
    )

    response = await ws.receive_json()
    assert_next(response, "sub1", {"echo": "Hi"})
    await ws.send_json(CompleteMessage(id="sub1").as_dict())


async def test_subscription_syntax_error(ws: WebSocketClient):
    await ws.send_json(
        SubscribeMessage(
            id="sub1",
            payload=SubscribeMessagePayload(query="subscription { INVALID_SYNTAX "),
        ).as_dict()
    )

    data = await ws.receive(timeout=2)
    assert ws.closed
    assert ws.close_code == 4400
    assert ws.close_reason == "Syntax Error: Expected Name, found <EOF>."


async def test_subscription_field_errors(ws: WebSocketClient):
    process_errors = Mock()
    with patch.object(Schema, "process_errors", process_errors):
        await ws.send_json(
            SubscribeMessage(
                id="sub1",
                payload=SubscribeMessagePayload(
                    query="subscription { notASubscriptionField }",
                ),
            ).as_dict()
        )

        response = await ws.receive_json()
        assert response["type"] == ErrorMessage.type
        assert response["id"] == "sub1"
        assert len(response["payload"]) == 1
        assert response["payload"][0].get("path") is None
        assert response["payload"][0]["locations"] == [{"line": 1, "column": 16}]
        assert (
            response["payload"][0]["message"]
            == "Cannot query field 'notASubscriptionField' on type 'Subscription'."
        )
        process_errors.assert_called_once()


async def test_query_field_errors(ws: WebSocketClient):
    await ws.send_json(
        SubscribeMessage(
            id="sub1",
            payload=SubscribeMessagePayload(
                query="query { notASubscriptionField }",
            ),
        ).as_dict()
    )

    response = await ws.receive_json()
    assert response["type"] == ErrorMessage.type
    assert response["id"] == "sub1"
    assert len(response["payload"]) == 1
    assert response["payload"][0].get("path") is None
    assert response["payload"][0]["locations"] == [{"line": 1, "column": 9}]
    assert (
        response["payload"][0]["message"]
        == "Cannot query field 'notASubscriptionField' on type 'Query'."
    )


async def test_subscription_cancellation(ws: WebSocketClient):
    await ws.send_json(
        SubscribeMessage(
            id="sub1",
            payload=SubscribeMessagePayload(
                query='subscription { echo(message: "Hi", delay: 99) }'
            ),
        ).as_dict()
    )

    await ws.send_json(
        SubscribeMessage(
            id="sub2",
            payload=SubscribeMessagePayload(
                query="subscription { debug { numActiveResultHandlers } }",
            ),
        ).as_dict()
    )

    response = await ws.receive_json()
    assert_next(response, "sub2", {"debug": {"numActiveResultHandlers": 2}})
    response = await ws.receive_json()
    assert response == CompleteMessage(id="sub2").as_dict()

    await ws.send_json(CompleteMessage(id="sub1").as_dict())

    await ws.send_json(
        SubscribeMessage(
            id="sub3",
            payload=SubscribeMessagePayload(
                query="subscription { debug { numActiveResultHandlers } }",
            ),
        ).as_dict()
    )

    response = await ws.receive_json()
    assert_next(response, "sub3", {"debug": {"numActiveResultHandlers": 1}})

    response = await ws.receive_json()
    assert response == CompleteMessage(id="sub3").as_dict()


async def test_subscription_errors(ws: WebSocketClient):
    await ws.send_json(
        SubscribeMessage(
            id="sub1",
            payload=SubscribeMessagePayload(
                query='subscription { error(message: "TEST ERR") }',
            ),
        ).as_dict()
    )

    response = await ws.receive_json()
    assert response["type"] == NextMessage.type
    assert response["id"] == "sub1"
    assert len(response["payload"]["errors"]) == 1
    assert response["payload"]["errors"][0]["path"] == ["error"]
    assert response["payload"]["errors"][0]["message"] == "TEST ERR"


async def test_operation_error_no_complete(ws: WebSocketClient):
    """Test that an "error" message is not followed by "complete"."""
    # Since we don't include the operation variables,
    # the subscription will fail immediately.
    # see https://github.com/enisdenjo/graphql-ws/blob/master/PROTOCOL.md#error
    await ws.send_json(
        SubscribeMessage(
            id="sub1",
            payload=SubscribeMessagePayload(
                query="subscription Foo($bar: String!){ exception(message: $bar) }",
            ),
        ).as_dict()
    )

    response = await ws.receive_json()
    assert response["type"] == ErrorMessage.type
    assert response["id"] == "sub1"

    # after an "error" message, there should be nothing more
    # sent regarding "sub1", not even a "complete".
    await ws.send_json(PingMessage().as_dict())
    data = await ws.receive_json(timeout=1)
    assert data == PongMessage().as_dict()


async def test_subscription_exceptions(ws: WebSocketClient):
    process_errors = Mock()
    with patch.object(Schema, "process_errors", process_errors):
        await ws.send_json(
            SubscribeMessage(
                id="sub1",
                payload=SubscribeMessagePayload(
                    query='subscription { exception(message: "TEST EXC") }',
                ),
            ).as_dict()
        )

        response = await ws.receive_json()
        assert response["type"] == NextMessage.type
        assert response["id"] == "sub1"
        assert response["payload"]["errors"] == [{"message": "TEST EXC"}]
        process_errors.assert_called_once()


async def test_single_result_query_operation(ws: WebSocketClient):
    await ws.send_json(
        SubscribeMessage(
            id="sub1",
            payload=SubscribeMessagePayload(query="query { hello }"),
        ).as_dict()
    )

    response = await ws.receive_json()
    assert_next(response, "sub1", {"hello": "Hello world"})
    response = await ws.receive_json()
    assert response == CompleteMessage(id="sub1").as_dict()


async def test_single_result_query_operation_async(ws: WebSocketClient):
    """Test a single result query operation on an
    `async` method in the schema, including an artificial
    async delay.
    """
    await ws.send_json(
        SubscribeMessage(
            id="sub1",
            payload=SubscribeMessagePayload(
                query='query { asyncHello(name: "Dolly", delay:0.01)}'
            ),
        ).as_dict()
    )

    response = await ws.receive_json()
    assert_next(response, "sub1", {"asyncHello": "Hello Dolly"})

    response = await ws.receive_json()
    assert response == CompleteMessage(id="sub1").as_dict()


async def test_single_result_query_operation_overlapped(ws: WebSocketClient):
    """Test that two single result queries can be in flight at the same time,
    just like regular queries.  Start two queries with separate ids. The
    first query has a delay, so we expect the response to the second
    query to be delivered first.
    """
    # first query
    await ws.send_json(
        SubscribeMessage(
            id="sub1",
            payload=SubscribeMessagePayload(
                query='query { asyncHello(name: "Dolly", delay:1)}'
            ),
        ).as_dict()
    )
    # second query
    await ws.send_json(
        SubscribeMessage(
            id="sub2",
            payload=SubscribeMessagePayload(
                query='query { asyncHello(name: "Dolly", delay:0)}'
            ),
        ).as_dict()
    )

    # we expect the response to the second query to arrive first
    response = await ws.receive_json()
    assert_next(response, "sub2", {"asyncHello": "Hello Dolly"})
    response = await ws.receive_json()
    assert response == CompleteMessage(id="sub2").as_dict()


async def test_single_result_mutation_operation(ws: WebSocketClient):
    await ws.send_json(
        SubscribeMessage(
            id="sub1",
            payload=SubscribeMessagePayload(query="mutation { hello }"),
        ).as_dict()
    )

    response = await ws.receive_json()
    assert_next(response, "sub1", {"hello": "strawberry"})

    response = await ws.receive_json()
    assert response == CompleteMessage(id="sub1").as_dict()


async def test_single_result_operation_selection(ws: WebSocketClient):
    query = """
        query Query1 {
            hello
        }
        query Query2 {
            hello(name: "Strawberry")
        }
    """

    await ws.send_json(
        SubscribeMessage(
            id="sub1",
            payload=SubscribeMessagePayload(query=query, operationName="Query2"),
        ).as_dict()
    )

    response = await ws.receive_json()
    assert_next(response, "sub1", {"hello": "Hello Strawberry"})
    response = await ws.receive_json()
    assert response == CompleteMessage(id="sub1").as_dict()


async def test_single_result_invalid_operation_selection(ws: WebSocketClient):
    query = """
        query Query1 {
            hello
        }
    """

    await ws.send_json(
        SubscribeMessage(
            id="sub1",
            payload=SubscribeMessagePayload(query=query, operationName="Query2"),
        ).as_dict()
    )

    data = await ws.receive(timeout=2)
    assert ws.closed
    assert ws.close_code == 4400
    assert ws.close_reason == "Can't get GraphQL operation type"


async def test_single_result_execution_error(ws: WebSocketClient):
    process_errors = Mock()
    with patch.object(Schema, "process_errors", process_errors):
        await ws.send_json(
            SubscribeMessage(
                id="sub1",
                payload=SubscribeMessagePayload(
                    query="query { alwaysFail }",
                ),
            ).as_dict()
        )

        response = await ws.receive_json()
        assert response["type"] == NextMessage.type
        assert response["id"] == "sub1"
        errs = response["payload"]["errors"]
        assert len(errs) == 1
        assert errs[0]["path"] == ["alwaysFail"]
        assert errs[0]["message"] == "You are not authorized"

        process_errors.assert_called_once()


async def test_single_result_pre_execution_error(ws: WebSocketClient):
    """Test that single-result-operations which raise exceptions
    behave in the same way as streaming operations.
    """
    process_errors = Mock()
    with patch.object(Schema, "process_errors", process_errors):
        await ws.send_json(
            SubscribeMessage(
                id="sub1",
                payload=SubscribeMessagePayload(
                    query="query { IDontExist }",
                ),
            ).as_dict()
        )

        response = await ws.receive_json()
        assert response["type"] == ErrorMessage.type
        assert response["id"] == "sub1"
        assert len(response["payload"]) == 1
        assert (
            response["payload"][0]["message"]
            == "Cannot query field 'IDontExist' on type 'Query'."
        )
        process_errors.assert_called_once()


async def test_single_result_duplicate_ids_sub(ws: WebSocketClient):
    """Test that single-result-operations and streaming operations
    share the same ID namespace. Start a regular subscription,
    then issue a single-result operation with same ID and expect an
    error due to already existing ID
    """
    # regular subscription
    await ws.send_json(
        SubscribeMessage(
            id="sub1",
            payload=SubscribeMessagePayload(
                query='subscription { echo(message: "Hi", delay: 5) }'
            ),
        ).as_dict()
    )
    # single result subscription with duplicate id
    await ws.send_json(
        SubscribeMessage(
            id="sub1",
            payload=SubscribeMessagePayload(
                query="query { hello }",
            ),
        ).as_dict()
    )

    data = await ws.receive(timeout=2)
    assert ws.closed
    assert ws.close_code == 4409
    assert ws.close_reason == "Subscriber for sub1 already exists"


async def test_single_result_duplicate_ids_query(ws: WebSocketClient):
    """Test that single-result-operations don't allow duplicate
    IDs for two asynchronous queries. Issue one async query
    with delay, then another with same id. Expect error.
    """
    # single result subscription 1
    await ws.send_json(
        SubscribeMessage(
            id="sub1",
            payload=SubscribeMessagePayload(
                query='query { asyncHello(name: "Hi", delay: 5) }'
            ),
        ).as_dict()
    )
    # single result subscription with duplicate id
    await ws.send_json(
        SubscribeMessage(
            id="sub1",
            payload=SubscribeMessagePayload(
                query="query { hello }",
            ),
        ).as_dict()
    )

    # We expect the remote to close the socket due to duplicate ID in use
    data = await ws.receive(timeout=2)
    assert ws.closed
    assert ws.close_code == 4409
    assert ws.close_reason == "Subscriber for sub1 already exists"


async def test_injects_connection_params(ws_raw: WebSocketClient):
    ws = ws_raw
    await ws.send_json(ConnectionInitMessage(payload={"strawberry": "rocks"}).as_dict())

    response = await ws.receive_json()
    assert response == ConnectionAckMessage().as_dict()

    await ws.send_json(
        SubscribeMessage(
            id="sub1",
            payload=SubscribeMessagePayload(query="subscription { connectionParams }"),
        ).as_dict()
    )

    response = await ws.receive_json()
    assert_next(response, "sub1", {"connectionParams": "rocks"})

    await ws.send_json(CompleteMessage(id="sub1").as_dict())


async def test_rejects_connection_params_not_dict(ws_raw: WebSocketClient):
    ws = ws_raw
    await ws.send_json(ConnectionInitMessage(payload="gonna fail").as_dict())

    data = await ws.receive(timeout=2)
    assert ws.closed
    assert ws.close_code == 4400
    assert ws.close_reason == "Invalid connection init payload"


@pytest.mark.parametrize(
    "payload",
    [[], "invalid value", 1],
)
async def test_rejects_connection_params_with_wrong_type(
    payload: Any, ws_raw: WebSocketClient
):
    ws = ws_raw
    await ws.send_json(ConnectionInitMessage(payload=payload).as_dict())

    data = await ws.receive(timeout=2)
    assert ws.closed
    assert ws.close_code == 4400
    assert ws.close_reason == "Invalid connection init payload"


# timings can sometimes fail currently.  Until this test is rewritten when
# generator based subscriptions are implemented, mark it as flaky
@pytest.mark.xfail(reason="This test is flaky, see comment above")
async def test_subsciption_cancel_finalization_delay(ws: WebSocketClient):
    # Test that when we cancel a subscription, the websocket isn't blocked
    # while some complex finalization takes place.
    delay = 0.1

    await ws.send_json(
        SubscribeMessage(
            id="sub1",
            payload=SubscribeMessagePayload(
                query=f"subscription {{ longFinalizer(delay: {delay}) }}"
            ),
        ).as_dict()
    )

    response = await ws.receive_json()
    assert_next(response, "sub1", {"longFinalizer": "hello"})

    # now cancel the stubscription and send a new query.  We expect the response
    # to the new query to arrive immediately, without waiting for the finalizer
    start = time.time()
    await ws.send_json(CompleteMessage(id="sub1").as_dict())
    await ws.send_json(
        SubscribeMessage(
            id="sub2",
            payload=SubscribeMessagePayload(query="query { hello }"),
        ).as_dict()
    )
    while True:
        response = await ws.receive_json()
        assert response["type"] in ("next", "complete")
        if response["id"] == "sub2":
            break
    end = time.time()
    elapsed = end - start
    assert elapsed < delay


async def test_error_handler_for_timeout(http_client: HttpClient):
    """Test that the error handler is called when the timeout
    task encounters an error.
    """
    with contextlib.suppress(ImportError):
        from tests.http.clients.channels import ChannelsHttpClient

        if isinstance(http_client, ChannelsHttpClient):
            pytest.skip("Can't patch on_init for this client")

    ws = ws_raw
    handler = None
    errorhandler = AsyncMock()

    def on_init(_handler):
        nonlocal handler
        if handler:
            return
        handler = _handler
        # patch the object
        handler.handle_task_exception = errorhandler
        # cause an attribute error in the timeout task
        handler.connection_init_wait_timeout = None

    with patch.object(DebuggableGraphQLTransportWSHandler, "on_init", on_init):
        async with http_client.ws_connect(
            "/graphql", protocols=[GRAPHQL_TRANSPORT_WS_PROTOCOL]
        ) as ws:
            await asyncio.sleep(0.01)  # wait for the timeout task to start
            await ws.send_json(ConnectionInitMessage().as_dict())
            response = await ws.receive_json()
            assert response == ConnectionAckMessage().as_dict()
            await ws.close()

    # the error hander should have been called
    assert handler
    errorhandler.assert_called_once()
    args = errorhandler.call_args
    assert isinstance(args[0][0], AttributeError)
    assert "total_seconds" in str(args[0][0])


async def test_subscription_errors_continue(ws: WebSocketClient):
    """Verify that an ExecutionResult with errors during subscription does not terminate
    the subscription.
    """
    process_errors = Mock()
    with patch.object(Schema, "process_errors", process_errors):
        await ws.send_json(
            SubscribeMessage(
                id="sub1",
                payload=SubscribeMessagePayload(
                    query="subscription { flavorsInvalid }",
                ),
            ).as_dict()
        )

        response = await ws.receive_json()
        assert response["type"] == NextMessage.type
        assert response["id"] == "sub1"
        assert response["payload"]["data"] == {"flavorsInvalid": "VANILLA"}

        response = await ws.receive_json()
        assert response["type"] == NextMessage.type
        assert response["id"] == "sub1"
        assert response["payload"]["data"] is None
        errors = response["payload"]["errors"]
        assert "cannot represent value" in str(errors)
        process_errors.assert_called_once()

        response = await ws.receive_json()
        assert response["type"] == NextMessage.type
        assert response["id"] == "sub1"
        assert response["payload"]["data"] == {"flavorsInvalid": "CHOCOLATE"}

        response = await ws.receive_json()
        assert response["type"] == CompleteMessage.type
        assert response["id"] == "sub1"


@patch.object(MyExtension, MyExtension.get_results.__name__, return_value={})
async def test_no_extensions_results_wont_send_extensions_in_payload(
    mock: Mock, ws: WebSocketClient
):
    await ws.send_json(
        SubscribeMessage(
            id="sub1",
            payload=SubscribeMessagePayload(
                query='subscription { echo(message: "Hi") }'
            ),
        ).as_dict()
    )

    response = await ws.receive_json()
    mock.assert_called_once()
    assert_next(response, "sub1", {"echo": "Hi"})
    assert "extensions" not in response["payload"]


async def test_validation_query(ws: WebSocketClient):
    """
    Test validation for query
    """
    await ws.send_json(
        SubscribeMessage(
            id="sub1",
            payload=SubscribeMessagePayload(
                query="query { conditionalFail(fail:true) }"
            ),
        ).as_dict()
    )

    # We expect an error message directly
    response = await ws.receive_json()
    assert response["type"] == ErrorMessage.type
    assert response["id"] == "sub1"
    assert len(response["payload"]) == 1
    assert response["payload"][0].get("path") == ["conditionalFail"]
    assert response["payload"][0]["message"] == "failed after sleep None"


async def test_validation_subscription(ws: WebSocketClient):
    """
    Test validation for subscription
    """
    await ws.send_json(
        SubscribeMessage(
            id="sub1",
            payload=SubscribeMessagePayload(
                query="subscription { conditionalFail(fail:true) }"
            ),
        ).as_dict()
    )

    # We expect an error message directly
    response = await ws.receive_json()
    assert response["type"] == ErrorMessage.type
    assert response["id"] == "sub1"
    assert len(response["payload"]) == 1
    assert response["payload"][0].get("path") == ["conditionalFail"]
    assert response["payload"][0]["message"] == "failed after sleep None"


async def test_long_validation_concurrent_query(ws: WebSocketClient):
    """
    Test that the websocket is not blocked while validating a
    single-result-operation
    """
    await ws.send_json(
        SubscribeMessage(
            id="sub1",
            payload=SubscribeMessagePayload(
                query="query { conditionalFail(sleep:0.1) }"
            ),
        ).as_dict()
    )
    await ws.send_json(
        SubscribeMessage(
            id="sub2",
            payload=SubscribeMessagePayload(
                query="query { conditionalFail(fail:false) }"
            ),
        ).as_dict()
    )

    # we expect the second query to arrive first, because the
    # first query is stuck in validation
    response = await ws.receive_json()
    assert_next(response, "sub2", {"conditionalFail": "Hey"})


async def test_long_validation_concurrent_subscription(ws: WebSocketClient):
    """
    Test that the websocket is not blocked while validating a
    subscription
    """
    await ws.send_json(
        SubscribeMessage(
            id="sub1",
            payload=SubscribeMessagePayload(
                query="subscription { conditionalFail(sleep:0.1) }"
            ),
        ).as_dict()
    )
    await ws.send_json(
        SubscribeMessage(
            id="sub2",
            payload=SubscribeMessagePayload(
                query="query { conditionalFail(fail:false) }"
            ),
        ).as_dict()
    )

    # we expect the second query to arrive first, because the
    # first operation is stuck in validation
    response = await ws.receive_json()
    assert_next(response, "sub2", {"conditionalFail": "Hey"})


async def test_task_error_handler(ws: WebSocketClient):
    """
    Test that error handling works
    """
    # can't use a simple Event here, because the handler may run
    # on a different thread
    wakeup = False

    # a replacement method which causes an error in th eTask
    async def op(*args: Any, **kwargs: Any):
        nonlocal wakeup
        wakeup = True
        raise ZeroDivisionError("test")

    with patch.object(BaseGraphQLTransportWSHandler, "task_logger") as logger:
        with patch.object(BaseGraphQLTransportWSHandler, "handle_operation", op):
            # send any old subscription request.  It will raise an error
            await ws.send_json(
                SubscribeMessage(
                    id="sub1",
                    payload=SubscribeMessagePayload(
                        query="subscription { conditionalFail(sleep:0) }"
                    ),
                ).as_dict()
            )

            # wait for the error to be logged.  Must use timed loop and not event.
            while not wakeup:  # noqa: ASYNC110
                await asyncio.sleep(0.01)
            # and another little bit, for the thread to finish
            await asyncio.sleep(0.01)
            assert logger.exception.called
