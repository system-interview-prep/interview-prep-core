from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import aio_pika
from aio_pika.abc import AbstractRobustChannel, AbstractRobustConnection

from src.core.config import get_settings


class RabbitMQ:
    def __init__(self) -> None:
        self.connection: AbstractRobustConnection | None = None
        self.channel: AbstractRobustChannel | None = None

    async def connect(self) -> None:
        self.connection = await aio_pika.connect_robust(get_settings().rabbitmq_url)
        self.channel = await self.connection.channel()
        await self.channel.set_qos(prefetch_count=1)

    async def close(self) -> None:
        if self.connection:
            await self.connection.close()

    async def publish(self, queue_name: str, body: bytes) -> None:
        if not self.channel:
            raise RuntimeError("RabbitMQ is not connected")
        queue = await self.channel.declare_queue(queue_name, durable=True)
        await self.channel.default_exchange.publish(
            aio_pika.Message(body=body, delivery_mode=aio_pika.DeliveryMode.PERSISTENT),
            routing_key=queue.name,
        )


rabbitmq = RabbitMQ()


@asynccontextmanager
async def rabbitmq_lifespan() -> AsyncIterator[None]:
    await rabbitmq.connect()
    try:
        yield
    finally:
        await rabbitmq.close()
