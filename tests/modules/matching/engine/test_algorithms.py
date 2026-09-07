from src.modules.matching.engine.manager import AlgorithmManager


def test_matching_registry_contains_only_supported_algorithms() -> None:
    manager = AlgorithmManager()
    assert "bert" not in manager.algorithm_registry
    assert "distilbert" not in manager.algorithm_registry
