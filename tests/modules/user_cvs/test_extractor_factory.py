from types import SimpleNamespace

from src.modules.user_cvs.parsing.infrastructure import extractor_factory
from src.modules.user_cvs.parsing.infrastructure.mineru_adapter import MinerUDocumentExtractor
from src.modules.user_cvs.parsing.infrastructure.paddleocr_adapter import PaddleOCRDocumentExtractor


def test_factory_keeps_mineru_as_a_selectable_provider(monkeypatch):
    monkeypatch.setattr(extractor_factory, "get_settings", lambda: SimpleNamespace(ocr_provider="mineru"))

    assert isinstance(extractor_factory.build_document_extractor(), MinerUDocumentExtractor)


def test_factory_selects_paddleocr_from_environment_setting(monkeypatch):
    monkeypatch.setattr(extractor_factory, "get_settings", lambda: SimpleNamespace(ocr_provider="paddleocr"))

    assert isinstance(extractor_factory.build_document_extractor(), PaddleOCRDocumentExtractor)


def test_factory_accepts_existing_miuneru_typo_as_mineru_alias(monkeypatch):
    monkeypatch.setattr(extractor_factory, "get_settings", lambda: SimpleNamespace(ocr_provider="miunerU"))

    assert isinstance(extractor_factory.build_document_extractor(), MinerUDocumentExtractor)
