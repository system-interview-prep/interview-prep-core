"""Select the configured OCR adapter at the application boundary."""

from src.core.config import get_settings
from src.modules.user_cvs.parsing.infrastructure.docx_text_adapter import DocxTextExtractor
from src.modules.user_cvs.parsing.infrastructure.fallback_extractor import FallbackDocumentExtractor
from src.modules.user_cvs.parsing.infrastructure.mineru_adapter import MinerUDocumentExtractor
from src.modules.user_cvs.parsing.infrastructure.paddleocr_adapter import PaddleOCRDocumentExtractor
from src.modules.user_cvs.parsing.infrastructure.pdf_text_adapter import PdfTextLayerExtractor

_PROVIDERS = {"mineru": MinerUDocumentExtractor, "paddleocr": PaddleOCRDocumentExtractor}
_ALIASES = {"mineru": "mineru", "miuneru": "mineru", "paddleocr": "paddleocr", "paddle": "paddleocr"}


def _provider_key(value: str) -> str | None:
    return _ALIASES.get(value.casefold().strip().replace("-", "").replace("_", ""))


def build_document_extractor():
    settings = get_settings()
    primary = _provider_key(settings.ocr_provider)
    if primary is None:
        raise ValueError("OCR_PROVIDER must be 'mineru' or 'paddleocr'")

    fallback_setting = str(getattr(settings, "ocr_fallback_provider", "none") or "none").casefold().strip()
    if fallback_setting == "auto":
        fallback = next(key for key in _PROVIDERS if key != primary)
    elif fallback_setting in {"none", "off", "false", ""}:
        fallback = None
    else:
        fallback = _provider_key(fallback_setting)
        if fallback is None:
            raise ValueError("OCR_FALLBACK_PROVIDER must be 'auto', 'none', 'mineru' or 'paddleocr'")

    chain = [_PROVIDERS[primary]()]
    if fallback is not None and fallback != primary:
        chain.append(_PROVIDERS[fallback]())
    if getattr(settings, "pdf_text_layer_enabled", False):
        # No upload at all for PDFs and Word files that already carry their text.
        chain[:0] = [PdfTextLayerExtractor(), DocxTextExtractor()]
    return chain[0] if len(chain) == 1 else FallbackDocumentExtractor(chain)
