from src.modules.matching.engine.manager import AlgorithmManager


def test_legacy_manager_has_no_local_transformer_algorithms() -> None:
    registry = AlgorithmManager().algorithm_registry

    assert {"bert", "distilbert", "sbert", "cross_encoder"}.isdisjoint(registry)
