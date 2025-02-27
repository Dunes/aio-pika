import asyncio
import contextlib
from functools import partial
from logging import getLogger
from types import TracebackType
from typing import Any, Callable, Generator, Optional, Type

import aiormq
from aiormq.abc import DeliveredMessage
from pamqp.common import Arguments

# This needed only for migration from 6.x to 7.x
# TODO: Remove this in 8.x release
from .abc import DeclarationResult  # noqa
from .abc import (
    AbstractChannel, AbstractIncomingMessage, AbstractQueue,
    AbstractQueueIterator, ConsumerTag, TimeoutType,
)
from .exceptions import QueueEmpty
from .exchange import Exchange, ExchangeParamType
from .message import IncomingMessage
from .tools import create_task, shield, task


log = getLogger(__name__)


async def consumer(
    callback: Callable[[AbstractIncomingMessage], Any],
    msg: DeliveredMessage, *,
    no_ack: bool,
    loop: asyncio.AbstractEventLoop
) -> Any:
    message = IncomingMessage(msg, no_ack=no_ack)
    return await create_task(callback, message, loop=loop)


class Queue(AbstractQueue):
    """ AMQP queue abstraction """

    def __init__(
        self,
        channel: AbstractChannel,
        name: Optional[str],
        durable: bool,
        exclusive: bool,
        auto_delete: bool,
        arguments: Arguments,
        passive: bool = False,
    ):
        self.declaration_result: aiormq.spec.Queue.DeclareOk
        self.loop = channel.loop
        self.channel = channel
        self.connection = channel.connection
        self.name = name or ""
        self.durable = durable
        self.exclusive = exclusive
        self.auto_delete = auto_delete
        self.arguments = arguments
        self.passive = passive
        self._get_lock = asyncio.Lock()

    @property
    def __channel(self) -> aiormq.abc.AbstractChannel:
        if self.channel is None or self.channel.is_closed:
            raise RuntimeError("Channel not opened")
        return self.channel.channel

    def __str__(self) -> str:
        return "%s" % self.name

    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__name__}({self}): "
            f"auto_delete={self.auto_delete}, "
            f"durable={self.durable}, "
            f"exclusive={self.exclusive}, "
            f"arguments={self.arguments!r}"
        )

    async def declare(
        self, timeout: TimeoutType = None,
    ) -> aiormq.spec.Queue.DeclareOk:
        """ Declare queue.

        :param timeout: execution timeout
        :param passive: Only check to see if the queue exists.
        :return: :class:`None`
        """

        log.debug("Declaring queue: %r", self)
        self.declaration_result = await self.__channel.queue_declare(
            queue=self.name,
            durable=self.durable,
            exclusive=self.exclusive,
            auto_delete=self.auto_delete,
            arguments=self.arguments,
            passive=self.passive,
            timeout=timeout,
        )

        if self.declaration_result.queue is not None:
            self.name = self.declaration_result.queue
        else:
            self.name = "<UNNAMED>"

        return self.declaration_result

    async def bind(
        self,
        exchange: ExchangeParamType,
        routing_key: str = None,
        *,
        arguments: Arguments = None,
        timeout: TimeoutType = None
    ) -> aiormq.spec.Queue.BindOk:

        """ A binding is a relationship between an exchange and a queue.
        This can be simply read as: the queue is interested in messages
        from this exchange.

        Bindings can take an extra routing_key parameter. To avoid
        the confusion with a basic_publish parameter we're going to
        call it a binding key.

        :param exchange: :class:`aio_pika.exchange.Exchange` instance
        :param routing_key: routing key
        :param arguments: additional arguments
        :param timeout: execution timeout
        :raises asyncio.TimeoutError:
            when the binding timeout period has elapsed.
        :return: :class:`None`
        """

        if routing_key is None:
            routing_key = self.name

        log.debug(
            "Binding queue %r: exchange=%r, routing_key=%r, arguments=%r",
            self,
            exchange,
            routing_key,
            arguments,
        )

        return await self.__channel.queue_bind(
            self.name,
            exchange=Exchange._get_exchange_name(exchange),
            routing_key=routing_key,
            arguments=arguments,
            timeout=timeout,
        )

    async def unbind(
        self,
        exchange: ExchangeParamType,
        routing_key: str = None,
        arguments: Arguments = None,
        timeout: TimeoutType = None,
    ) -> aiormq.spec.Queue.UnbindOk:

        """ Remove binding from exchange for this :class:`Queue` instance

        :param exchange: :class:`aio_pika.exchange.Exchange` instance
        :param routing_key: routing key
        :param arguments: additional arguments
        :param timeout: execution timeout
        :raises asyncio.TimeoutError:
            when the unbinding timeout period has elapsed.
        :return: :class:`None`
        """

        if routing_key is None:
            routing_key = self.name

        log.debug(
            "Unbinding queue %r: exchange=%r, routing_key=%r, arguments=%r",
            self,
            exchange,
            routing_key,
            arguments,
        )

        return await self.__channel.queue_unbind(
            queue=self.name,
            exchange=Exchange._get_exchange_name(exchange),
            routing_key=routing_key,
            arguments=arguments,
            timeout=timeout,
        )

    async def consume(
        self,
        callback: Callable[[AbstractIncomingMessage], Any],
        no_ack: bool = False,
        exclusive: bool = False,
        arguments: Arguments = None,
        consumer_tag: ConsumerTag = None,
        timeout: TimeoutType = None,
    ) -> ConsumerTag:

        """ Start to consuming the :class:`Queue`.

        :param timeout: :class:`asyncio.TimeoutError` will be raises when the
                        Future was not finished after this time.
        :param callback: Consuming callback. Could be a coroutine.
        :param no_ack:
            if :class:`True` you don't need to call
            :func:`aio_pika.message.IncomingMessage.ack`
        :param exclusive:
            Makes this queue exclusive. Exclusive queues may only
            be accessed by the current connection, and are deleted
            when that connection closes. Passive declaration of an
            exclusive queue by other connections are not allowed.
        :param arguments: additional arguments
        :param consumer_tag: optional consumer tag

        :raises asyncio.TimeoutError:
            when the consuming timeout period has elapsed.
        :return str: consumer tag :class:`str`

        """

        log.debug("Start to consuming queue: %r", self)

        consume_result = await self.__channel.basic_consume(
            queue=self.name,
            consumer_callback=partial(
                consumer,
                callback,
                no_ack=no_ack,
                loop=self.loop,
            ),
            exclusive=exclusive,
            no_ack=no_ack,
            arguments=arguments,
            consumer_tag=consumer_tag,
            timeout=timeout,
        )

        # consumer_tag property is Optional[str] in practice this check
        # should never take place, however, it protects against the case
        # if the `None` comes from pamqp
        if consume_result.consumer_tag is None:
            raise RuntimeError("Consumer tag is None")

        return consume_result.consumer_tag

    async def cancel(
        self, consumer_tag: ConsumerTag,
        timeout: TimeoutType = None,
        nowait: bool = False,
    ) -> aiormq.spec.Basic.CancelOk:
        """ This method cancels a consumer. This does not affect already
        delivered messages, but it does mean the server will not send any more
        messages for that consumer. The client may receive an arbitrary number
        of messages in between sending the cancel method and receiving the
        cancel-ok reply. It may also be sent from the server to the client in
        the event of the consumer being unexpectedly cancelled (i.e. cancelled
        for any reason other than the server receiving the corresponding
        basic.cancel from the client). This allows clients to be notified of
        the loss of consumers due to events such as queue deletion.

        :param consumer_tag:
            consumer tag returned by :func:`~aio_pika.Queue.consume`
        :param timeout: execution timeout
        :param bool nowait: Do not expect a Basic.CancelOk response
        :return: Basic.CancelOk when operation completed successfully
        """

        return await self.__channel.basic_cancel(
            consumer_tag=consumer_tag, nowait=nowait, timeout=timeout,
        )

    async def get(
        self, *, no_ack: bool = False,
        fail: bool = True, timeout: TimeoutType = 5
    ) -> Optional[IncomingMessage]:

        """ Get message from the queue.

        :param no_ack: if :class:`True` you don't need to call
                       :func:`aio_pika.message.IncomingMessage.ack`
        :param timeout: execution timeout
        :param fail: Should return :class:`None` instead of raise an
                     exception :class:`aio_pika.exceptions.QueueEmpty`.
        :return: :class:`aio_pika.message.IncomingMessage`
        """

        msg: DeliveredMessage = await self.__channel.basic_get(
            self.name, no_ack=no_ack, timeout=timeout,
        )

        if isinstance(msg.delivery, aiormq.spec.Basic.GetEmpty):
            if fail:
                raise QueueEmpty
            return None

        return IncomingMessage(msg, no_ack=no_ack)

    async def purge(
        self, no_wait: bool = False, timeout: TimeoutType = None,
    ) -> aiormq.spec.Queue.PurgeOk:
        """ Purge all messages from the queue.

        :param no_wait: no wait response
        :param timeout: execution timeout
        :return: :class:`None`
        """

        log.info("Purging queue: %r", self)

        return await self.__channel.queue_purge(
            self.name, nowait=no_wait, timeout=timeout,
        )

    async def delete(
        self, *, if_unused: bool = True,
        if_empty: bool = True, timeout: TimeoutType = None
    ) -> aiormq.spec.Queue.DeleteOk:

        """ Delete the queue.

        :param if_unused: Perform delete only when unused
        :param if_empty: Perform delete only when empty
        :param timeout: execution timeout
        :return: :class:`None`
        """

        log.info("Deleting %r", self)

        return await self.__channel.queue_delete(
            self.name,
            if_unused=if_unused,
            if_empty=if_empty,
            timeout=timeout,
        )

    def __aiter__(self) -> "AbstractQueueIterator":
        return self.iterator()

    def iterator(self, **kwargs: Any) -> "AbstractQueueIterator":
        """ Returns an iterator for async for expression.

        Full example:

        .. code-block:: python

            import aio_pika

            async def main():
                connection = await aio_pika.connect()

                async with connection:
                    channel = await connection.channel()

                    queue = await channel.declare_queue('test')

                    async with queue.iterator() as q:
                        async for message in q:
                            print(message.body)

        When your program runs with run_forever the iterator will be closed
        in background. In this case the context processor for iterator might
        be skipped and the queue might be used in the "async for"
        expression directly.

        .. code-block:: python

            import aio_pika

            async def main():
                connection = await aio_pika.connect()

                async with connection:
                    channel = await connection.channel()

                    queue = await channel.declare_queue('test')

                    async for message in queue:
                        print(message.body)

        :return: QueueIterator
        """

        return QueueIterator(self, **kwargs)


class QueueIterator(AbstractQueueIterator):
    @task
    async def close(self, *_: Any) -> Any:
        log.debug("Cancelling queue iterator %r", self)

        if not hasattr(self, "_consumer_tag"):
            log.debug("Queue iterator %r already cancelled", self)
            return

        if self._amqp_queue.channel.is_closed:
            log.debug("Queue iterator %r channel closed", self)
            return

        log.debug("Basic.cancel for %r", self._consumer_tag)
        consumer_tag = self._consumer_tag
        del self._consumer_tag

        await self._amqp_queue.cancel(consumer_tag)
        self._amqp_queue.channel.close_callbacks.remove(self.close)

        log.debug("Queue iterator %r closed", self)

        def queue_tail(
            channel: aiormq.abc.AbstractChannel,
        ) -> Generator[Any, AbstractIncomingMessage, None]:
            while not channel.is_closed:
                with contextlib.suppress(asyncio.QueueEmpty):
                    yield self._queue.get_nowait()
                return None

        # Reject all messages
        msg: IncomingMessage
        for msg in queue_tail(self._amqp_queue.channel.channel):
            await msg.reject(requeue=True)

    def __str__(self) -> str:
        return f"queue[{self._amqp_queue}](...)"

    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__name__}: "
            f"queue={self._amqp_queue.name!r} "
            f"ctag={self._consumer_tag!r}>"
        )

    def __init__(self, queue: Queue, **kwargs: Any):
        self._consumer_tag: ConsumerTag
        self.loop = queue.loop
        self._amqp_queue: AbstractQueue = queue
        self._queue = asyncio.Queue()
        self._consume_kwargs = kwargs

        self._amqp_queue.channel.close_callbacks.add(self.close)

    async def on_message(self, message: AbstractIncomingMessage) -> None:
        await self._queue.put(message)

    async def consume(self) -> None:
        self._consumer_tag = await self._amqp_queue.consume(
            self.on_message, **self._consume_kwargs
        )

    def __aiter__(self) -> "AbstractQueueIterator":
        return self

    @shield
    async def __aenter__(self) -> "AbstractQueueIterator":
        if not hasattr(self, "_consumer_tag"):
            await self.consume()
        return self

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> None:
        await self.close()

    async def __anext__(self) -> IncomingMessage:
        if not hasattr(self, "_consumer_tag"):
            await self.consume()
        try:
            return await asyncio.wait_for(
                self._queue.get(),
                timeout=self._consume_kwargs.get("timeout"),
            )
        except asyncio.CancelledError:
            await self.close()
            raise


__all__ = ("Queue", "QueueIterator", "ConsumerTag")
