"""Select the configured OCR adapter at the application boundary."""

from src.core.config import get_settings
from src.modules.user_cvs.parsing.infrastructure.mineru_adapter import MinerUDocumentExtractor
from src.modules.user_cvs.parsing.infrastructure.paddleocr_adapter import PaddleOCRDocumentExtractor


def build_document_extractor():
    provider = get_settings().ocr_provider.casefold().strip().replace("-", "").replace("_", "")
    if provider in {"mineru", "miuneru"}:
        return MinerUDocumentExtractor()
    if provider in {"paddleocr", "paddle"}:
        return PaddleOCRDocumentExtractor()
    raise ValueError("OCR_PROVIDER must be 'mineru' or 'paddleocr'")
