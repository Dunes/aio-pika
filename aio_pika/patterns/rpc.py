import asyncio
import json
import logging
import pickle
import time
import uuid
from enum import Enum
from functools import partial
from typing import Any, Callable, Dict, Optional, Tuple, TypeVar

from aiormq.abc import ExceptionType
from aiormq.tools import awaitable

from aio_pika.abc import (
    AbstractChannel, AbstractExchange, AbstractIncomingMessage, AbstractQueue,
    ConsumerTag, DeliveryMode,
)
from aio_pika.channel import Channel
from aio_pika.exceptions import MessageProcessError
from aio_pika.exchange import ExchangeType
from aio_pika.message import IncomingMessage, Message, ReturnedMessage
from aio_pika.tools import shield

from .base import Base, Proxy


log = logging.getLogger(__name__)

T = TypeVar("T")
CallbackType = Callable[..., T]


class RPCMessageType(str, Enum):
    ERROR = "error"
    RESULT = "result"
    CALL = "call"


# This needed only for migration from 6.x to 7.x
# TODO: Remove this in 8.x release
RPCMessageTypes = RPCMessageType    # noqa


class RPC(Base):
    __slots__ = (
        "channel",
        "loop",
        "proxy",
        "result_queue",
        "result_consumer_tag",
        "routes",
        "consumer_tags",
        "dlx_exchange",
    )

    DLX_NAME = "rpc.dlx"
    DELIVERY_MODE = DeliveryMode.NOT_PERSISTENT

    __doc__ = """
    Remote Procedure Call helper.

    Create an instance ::

        rpc = await RPC.create(channel)

    Registering python function ::

        # RPC instance passes only keyword arguments
        def multiply(*, x, y):
            return x * y

        await rpc.register("multiply", multiply)

    Call function through proxy ::

        assert await rpc.proxy.multiply(x=2, y=3) == 6

    Call function explicit ::

        assert await rpc.call('multiply', dict(x=2, y=3)) == 6

    """

    def __init__(self, channel: Channel):
        self.result_queue: AbstractQueue
        self.result_consumer_tag: ConsumerTag
        self.dlx_exchange: AbstractExchange
        self.channel = channel
        self.loop = self.channel.loop
        self.proxy = Proxy(self.call)
        self.futures: Dict[str, asyncio.Future] = {}
        self.routes: Dict[str, Callable[..., Any]] = {}
        self.queues: Dict[Callable[..., Any], AbstractQueue] = {}
        self.consumer_tags: Dict[Callable[..., Any], ConsumerTag] = {}

    def __remove_future(self, future: asyncio.Future) -> None:
        log.debug("Remove done future %r", future)
        self.futures.pop(str(id(future)), None)

    def create_future(self) -> Tuple[asyncio.Future, str]:
        future = self.loop.create_future()
        log.debug("Create future for RPC call")
        correlation_id = str(uuid.uuid4())
        self.futures[correlation_id] = future
        future.add_done_callback(self.__remove_future)
        return future, correlation_id

    @shield
    async def close(self) -> None:
        if not hasattr(self, "result_queue"):
            log.warning("RPC already closed")
            return

        log.debug("Cancelling listening %r", self.result_queue)
        await self.result_queue.cancel(self.result_consumer_tag)
        del self.result_consumer_tag

        log.debug("Unbinding %r", self.result_queue)
        await self.result_queue.unbind(
            self.dlx_exchange, "",
            arguments={"From": self.result_queue.name, "x-match": "any"},
        )

        log.debug("Cancelling undone futures %r", self.futures)
        for future in self.futures.values():
            if future.done():
                continue

            future.set_exception(asyncio.CancelledError)

        log.debug("Deleting %r", self.result_queue)
        await self.result_queue.delete()
        del self.result_queue
        del self.dlx_exchange

    @shield
    async def initialize(
        self, auto_delete: bool = True,
        durable: bool = False, **kwargs: Any
    ) -> None:
        if hasattr(self, "result_queue"):
            return

        self.result_queue = await self.channel.declare_queue(
            None, auto_delete=auto_delete, durable=durable, **kwargs
        )

        self.dlx_exchange = await self.channel.declare_exchange(
            self.DLX_NAME, type=ExchangeType.HEADERS, auto_delete=True,
        )

        await self.result_queue.bind(
            self.dlx_exchange,
            "",
            arguments={"From": self.result_queue.name, "x-match": "any"},
        )

        self.result_consumer_tag = await self.result_queue.consume(
            self.on_result_message, exclusive=True, no_ack=True,
        )

        self.channel.close_callbacks.add(self.on_close)
        self.channel.return_callbacks.add(self.on_message_returned)

    def on_close(
        self, channel: AbstractChannel,
        exc: Optional[ExceptionType] = None,
    ) -> None:
        log.debug("Closing RPC futures because %r", exc)
        for future in self.futures.values():
            if future.done():
                continue

            future.set_exception(exc or Exception)

    @classmethod
    async def create(cls, channel: Channel, **kwargs: Any) -> "RPC":
        """ Creates a new instance of :class:`aio_pika.patterns.RPC`.
        You should use this method instead of :func:`__init__`,
        because :func:`create` returns coroutine and makes async initialize

        :param channel: initialized instance of :class:`aio_pika.Channel`
        :returns: :class:`RPC`

        """
        rpc = cls(channel)
        await rpc.initialize(**kwargs)
        return rpc

    def on_message_returned(
        self, channel: AbstractChannel, message: ReturnedMessage,
    ) -> None:
        if message.correlation_id is None:
            log.warning(
                "Message without correlation_id was returned: %r", message,
            )
            return

        future = self.futures.pop(message.correlation_id, None)

        if not future or future.done():
            log.warning("Unknown message was returned: %r", message)
            return

        future.set_exception(
            MessageProcessError("Message has been returned", message),
        )

    async def on_result_message(self, message: AbstractIncomingMessage) -> None:
        if message.correlation_id is None:
            log.warning(
                "Message without correlation_id was received: %r", message,
            )
            return

        future = self.futures.pop(message.correlation_id, None)

        if future is None:
            log.warning("Unknown message: %r", message)
            return

        try:
            payload = self.deserialize(message.body)
        except Exception as e:
            log.error("Failed to deserialize response on message: %r", message)
            future.set_exception(e)
            return

        if message.type == RPCMessageType.RESULT.value:
            future.set_result(payload)
        elif message.type == RPCMessageType.ERROR.value:
            future.set_exception(payload)
        elif message.type == RPCMessageType.CALL.value:
            future.set_exception(
                asyncio.TimeoutError("Message timed-out", message),
            )
        else:
            future.set_exception(
                RuntimeError("Unknown message type %r" % message.type),
            )

    async def on_call_message(
        self, method_name: str, message: IncomingMessage,
    ) -> None:
        if method_name not in self.routes:
            log.warning("Method %r not registered in %r", method_name, self)
            return

        try:
            payload = self.deserialize(message.body)
            func = self.routes[method_name]

            result = self.serialize(await self.execute(func, payload))
            message_type = RPCMessageType.RESULT.value
        except Exception as e:
            result = self.serialize_exception(e)
            message_type = RPCMessageType.ERROR.value

        if not message.reply_to:
            log.info(
                'RPC message without "reply_to" header %r call result '
                "will be lost",
                message,
            )
            await message.ack()
            return

        result_message = Message(
            result,
            content_type=self.CONTENT_TYPE,
            correlation_id=message.correlation_id,
            delivery_mode=message.delivery_mode,
            timestamp=time.time(),
            type=message_type,
        )

        try:
            await self.channel.default_exchange.publish(
                result_message, message.reply_to, mandatory=False,
            )
        except Exception:
            log.exception("Failed to send reply %r", result_message)
            await message.reject(requeue=False)
            return

        if message_type == RPCMessageType.ERROR.value:
            await message.ack()
            return

        await message.ack()

    def serialize(self, data: Any) -> bytes:
        """ Serialize data to the bytes.
        Uses `pickle` by default.
        You should overlap this method when you want to change serializer

        :param data: Data which will be serialized
        :returns: bytes
        """
        return super().serialize(data)

    def deserialize(self, data: bytes) -> Any:
        """ Deserialize data from bytes.
        Uses `pickle` by default.
        You should overlap this method when you want to change serializer

        :param data: Data which will be deserialized
        :returns: :class:`Any`
        """
        return super().deserialize(data)

    def serialize_exception(self, exception: Exception) -> bytes:
        """ Serialize python exception to bytes

        :param exception: :class:`Exception`
        :return: bytes
        """
        return pickle.dumps(exception)

    async def execute(self, func: CallbackType, payload: Dict[str, Any]) -> T:
        """ Executes rpc call. Might be overlapped. """
        return await func(**payload)

    async def call(
        self,
        method_name: str,
        kwargs: Optional[Dict[str, Any]] = None,
        *,
        expiration: Optional[int] = None,
        priority: int = 5,
        delivery_mode: DeliveryMode = DELIVERY_MODE
    ) -> Any:
        """ Call remote method and awaiting result.

        :param method_name: Name of method
        :param kwargs: Methos kwargs
        :param expiration:
            If not `None` messages which staying in queue longer
            will be returned and :class:`asyncio.TimeoutError` will be raised.
        :param priority: Message priority
        :param delivery_mode: Call message delivery mode
        :raises asyncio.TimeoutError: when message expired
        :raises CancelledError: when called :func:`RPC.cancel`
        :raises RuntimeError: internal error
        """

        future, correlation_id = self.create_future()

        message = Message(
            body=self.serialize(kwargs or {}),
            type=RPCMessageType.CALL.value,
            timestamp=time.time(),
            priority=priority,
            correlation_id=correlation_id,
            delivery_mode=delivery_mode,
            reply_to=self.result_queue.name,
            headers={"From": self.result_queue.name},
        )

        if expiration is not None:
            message.expiration = expiration

        log.debug("Publishing calls for %s(%r)", method_name, kwargs)
        await self.channel.default_exchange.publish(
            message, routing_key=method_name, mandatory=True,
        )

        log.debug("Waiting RPC result for %s(%r)", method_name, kwargs)
        return await future

    async def register(
        self, method_name: str, func: CallbackType, **kwargs: Any
    ) -> Any:
        """ Method creates a queue with name which equal of
        `method_name` argument. Then subscribes this queue.

        :param method_name: Method name
        :param func:
            target function. Function **MUST** accept only keyword arguments.
        :param kwargs: arguments which will be passed to `queue_declare`
        :raises RuntimeError:
            Function already registered in this :class:`RPC` instance
            or method_name already used.
        """
        arguments = kwargs.pop("arguments", {})
        arguments.update({"x-dead-letter-exchange": self.DLX_NAME})

        kwargs["arguments"] = arguments

        queue = await self.channel.declare_queue(method_name, **kwargs)

        if func in self.consumer_tags:
            raise RuntimeError("Function already registered")

        if method_name in self.routes:
            raise RuntimeError(
                "Method name already used for %r" % self.routes[method_name],
            )

        self.consumer_tags[func] = await queue.consume(
            partial(self.on_call_message, method_name),
        )

        self.routes[method_name] = awaitable(func)
        self.queues[func] = queue

    async def unregister(self, func: CallbackType) -> None:
        """ Cancels subscription to the method-queue.

        :param func: Function
        """
        if func not in self.consumer_tags:
            return

        consumer_tag = self.consumer_tags.pop(func)
        queue = self.queues.pop(func)

        await queue.cancel(consumer_tag)

        self.routes.pop(queue.name)


class JsonRPC(RPC):
    SERIALIZER = json
    CONTENT_TYPE = "application/json"

    def serialize(self, data: Any) -> bytes:
        return self.SERIALIZER.dumps(
            data, ensure_ascii=False, default=repr,
        ).encode()

    def serialize_exception(self, exception: Exception) -> bytes:
        return self.serialize(
            {
                "error": {
                    "type": exception.__class__.__name__,
                    "message": repr(exception),
                    "args": exception.args,
                },
            },
        )


__all__ = (
    "CallbackType",
    "JsonRPC",
    "RPC",
    "RPCMessageType",
)
