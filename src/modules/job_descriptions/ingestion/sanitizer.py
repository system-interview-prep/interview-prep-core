"""HTML sanitization and URL validation for external job descriptions."""

import html
import re
from html.parser import HTMLParser
from urllib.parse import urlparse


DANGEROUS_TAGS = {
    "script",
    "style",
    "iframe",
    "embed",
    "object",
    "noscript",
    "svg",
    "head",
    "meta",
    "link",
}

BLOCK_TAGS = {
    "p",
    "div",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "ul",
    "ol",
    "li",
    "tr",
    "blockquote",
    "section",
    "article",
    "header",
    "footer",
}


class _PlainTextExtractingParser(HTMLParser):
    """Parses HTML into readable plain text with structural line breaks while stripping dangerous tags."""

    def __init__(self) -> None:
        super().__init__()
        self._pieces: list[str] = []
        self._ignore_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if t in DANGEROUS_TAGS:
            self._ignore_depth += 1
            return
        if self._ignore_depth > 0:
            return

        if t in BLOCK_TAGS:
            self._pieces.append("\n")
        elif t == "br":
            self._pieces.append("\n")

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in DANGEROUS_TAGS:
            if self._ignore_depth > 0:
                self._ignore_depth -= 1
            return
        if self._ignore_depth > 0:
            return

        if t in BLOCK_TAGS:
            self._pieces.append("\n")

    def handle_data(self, data: str) -> None:
        if self._ignore_depth == 0:
            self._pieces.append(data)

    def get_text(self) -> str:
        raw = "".join(self._pieces)
        decoded = html.unescape(raw)
        # Normalize carriage returns and tabs
        cleaned = decoded.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
        # Collapse multiple empty lines
        lines = [line.strip() for line in cleaned.split("\n")]
        # Group non-empty lines while preserving paragraph separations
        result: list[str] = []
        prev_empty = True
        for line in lines:
            if line:
                result.append(line)
                prev_empty = False
            elif not prev_empty:
                result.append("")
                prev_empty = True
        return "\n".join(result).strip()


class HtmlSanitizer:
    """Security sanitization and text normalization utility for external JD content."""

    @staticmethod
    def to_plain_text(raw_html: str | None) -> str:
        """Converts raw HTML into clean, readable plain text for parser consumption."""
        if not raw_html or not raw_html.strip():
            return ""

        parser = _PlainTextExtractingParser()
        parser.feed(raw_html)
        parser.close()
        return parser.get_text()

    @staticmethod
    def sanitize_html(raw_html: str | None) -> str:
        """Sanitizes raw HTML description by removing dangerous elements and event handlers."""
        if not raw_html or not raw_html.strip():
            return ""

        content = raw_html

        # 1. Remove dangerous blocks completely (<script>...</script>, <style>...</style>, etc.)
        for tag in DANGEROUS_TAGS:
            pattern = re.compile(rf"<{tag}\b[^>]*>.*?</{tag}>", re.IGNORECASE | re.DOTALL)
            content = pattern.sub("", content)
            # Also self-closing or unclosed single tags
            single_pattern = re.compile(rf"<{tag}\b[^>]*>", re.IGNORECASE)
            content = single_pattern.sub("", content)

        # 2. Strip inline JavaScript event handlers (e.g. onload=, onclick=, onerror=)
        event_handler_pattern = re.compile(r'\s+on[a-z]+\s*=\s*(?:"[^"]*"|\'[^\']*\'|[^\s>]+)', re.IGNORECASE)
        content = event_handler_pattern.sub("", content)

        # 3. Strip dangerous URL schemes from href/src (e.g. javascript:, data:, vbscript:)
        scheme_pattern = re.compile(
            r'((?:href|src)\s*=\s*["\'])\s*(?:javascript|data|vbscript):[^"\']*["\']',
            re.IGNORECASE,
        )
        content = scheme_pattern.sub(r'\1#"', content)

        return content.strip()

    @staticmethod
    def validate_url(url: str | None) -> str | None:
        """Validates that a URL is well-formed with strict http or https scheme."""
        if not url:
            return None
        trimmed = url.strip()
        if not trimmed:
            return None
        try:
            parsed = urlparse(trimmed)
            if parsed.scheme.lower() in ("http", "https") and parsed.netloc:
                return trimmed
            return None
        except Exception:
            return None
