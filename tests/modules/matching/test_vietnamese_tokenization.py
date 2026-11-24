"""The query half must leave this process in the form the index stores."""

from src.modules.matching.retrieval.bm25 import tokenize_text
from src.modules.matching.retrieval.bm25_provider import ParadeDbBm25Provider


class TestVietnameseWordsSurviveTokenization:
    def test_d_with_stroke_no_longer_truncates_the_word(self):
        """"đã" used to tokenize to "a" and "đại học" to "ai hoc"."""
        assert tokenize_text("Đã triển khai", remove_stopwords=False) == [
            "da",
            "trien",
            "khai",
        ]

    def test_the_most_common_vietnamese_cv_words_keep_their_letters(self):
        tokens = tokenize_text(
            "Tốt nghiệp Đại học, được đào tạo về điện toán", remove_stopwords=False
        )
        assert tokens == [
            "tot",
            "nghiep",
            "dai",
            "hoc",
            "duoc",
            "dao",
            "tao",
            "ve",
            "dien",
            "toan",
        ]

    def test_every_token_is_ascii_so_the_index_can_match_it(self):
        tokens = tokenize_text(
            "Phát triển hệ thống đặt hàng điện tử", remove_stopwords=False
        )
        assert tokens and all(token.isascii() for token in tokens)

    def test_marked_and_unmarked_spellings_agree(self):
        """A CV typed without tone marks and one typed with them must meet."""
        assert tokenize_text("kinh nghiệm phát triển") == tokenize_text(
            "kinh nghiem phat trien"
        )

    def test_technology_names_are_still_preserved(self):
        assert tokenize_text("C++, C#, Node.js và .NET", remove_stopwords=False) == [
            "c++",
            "c#",
            "node.js",
            "va",
            ".net",
        ]

    def test_aliases_still_canonicalize(self):
        assert tokenize_text("k8s và postgres", remove_stopwords=False) == [
            "kubernetes",
            "va",
            "postgresql",
        ]


class TestSanitizedQueryReachesTheIndex:
    def test_the_query_string_carries_no_diacritics(self):
        query = ParadeDbBm25Provider.sanitize_query("Yêu cầu kinh nghiệm Đại học")
        assert query.isascii()

    def test_the_query_keeps_the_words_themselves(self):
        query = ParadeDbBm25Provider.sanitize_query("kinh nghiệm đại học")
        assert set(query.split()) == {"kinh", "nghiem", "dai", "hoc"}
