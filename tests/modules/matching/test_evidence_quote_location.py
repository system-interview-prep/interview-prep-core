"""One CV in two spellings must buy one answer and keep the same evidence."""

from src.modules.matching.application.targeted_reparse import (
    _cache_key_text,
    locate_quote,
)


class TestCacheKeyFolding:
    def test_capitals_do_not_buy_a_second_call(self):
        text = "Built a payment service in Java"
        assert _cache_key_text(text) == _cache_key_text(text.upper())

    def test_collapsed_whitespace_does_not_buy_a_second_call(self):
        text = "Built  a\tpayment service\n\n\n\nin Java"
        collapsed = "Built a payment service\n\nin Java"
        assert _cache_key_text(text) == _cache_key_text(collapsed)

    def test_different_wording_still_buys_its_own_call(self):
        assert _cache_key_text("Java at ACME") != _cache_key_text("Python at ACME")

    def test_vietnamese_diacritics_are_not_folded_away(self):
        """Diacritics carry meaning, so they must keep their own entry."""
        assert _cache_key_text("kinh nghiệm") != _cache_key_text("kinh nghiem")


class TestLocateQuote:
    def test_an_exact_quote_is_found(self):
        raw = "Backend developer. Built a payment service in Java."
        start, end = locate_quote(raw, "Built a payment service")
        assert raw[start:end] == "Built a payment service"

    def test_a_quote_in_the_other_case_still_resolves(self):
        raw = "Backend developer. Built a payment service in Java."
        start, end = locate_quote(raw, "BUILT A PAYMENT SERVICE")
        assert raw[start:end] == "Built a payment service"

    def test_a_quote_with_collapsed_spacing_still_resolves(self):
        raw = "Built  a   payment\tservice in Java."
        start, end = locate_quote(raw, "Built a payment service")
        assert raw[start:end] == "Built  a   payment\tservice"

    def test_a_quote_spanning_a_line_break_resolves(self):
        raw = "Built a payment\nservice in Java."
        start, end = locate_quote(raw, "Built a payment service")
        assert raw[start:end] == "Built a payment\nservice"

    def test_surrounding_whitespace_in_the_quote_is_ignored(self):
        raw = "Built a payment service in Java."
        start, end = locate_quote(raw, "  Built a payment service \n")
        assert raw[start:end] == "Built a payment service"

    def test_an_invented_quote_is_rejected(self):
        raw = "Backend developer."
        assert locate_quote(raw, "Led a team of twelve") is None

    def test_an_empty_quote_is_rejected(self):
        assert locate_quote("Backend developer.", "   ") is None

    def test_diacritics_are_not_ignored(self):
        """Lowercasing is allowed; dropping tone marks changes the word."""
        raw = "Có kinh nghiệm backend."
        assert locate_quote(raw, "kinh nghiem") is None
        start, end = locate_quote(raw, "KINH NGHIỆM")
        assert raw[start:end] == "kinh nghiệm"

    def test_offsets_stay_aligned_after_a_multi_character_lowering(self):
        """"İ" lowercases to two characters; offsets must not drift."""
        raw = "İstanbul office. Built a payment service in Java."
        start, end = locate_quote(raw, "Built a payment service")
        assert raw[start:end] == "Built a payment service"
