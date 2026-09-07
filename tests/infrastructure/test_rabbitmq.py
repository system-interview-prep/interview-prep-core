from src.infrastructure import rabbitmq


def test_rabbitmq_exposes_lifespan_and_channel_dependency() -> None:
    assert callable(rabbitmq.rabbitmq_lifespan)
    assert callable(rabbitmq.rabbitmq.publish)
