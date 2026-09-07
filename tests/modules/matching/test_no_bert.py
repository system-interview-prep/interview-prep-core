from src.modules.matching.engine.manager import AlgorithmManager
from src.modules.matching.legacy_config import MatchingConfig


def test_legacy_manager_has_no_local_transformer_algorithms() -> None:
    registry = AlgorithmManager().algorithm_registry

    assert {"bert", "distilbert", "sbert", "cross_encoder"}.isdisjoint(registry)


def test_default_legacy_algorithms_do_not_request_sbert() -> None:
    assert "sbert" not in MatchingConfig.DEFAULT_ALGORITHMS
