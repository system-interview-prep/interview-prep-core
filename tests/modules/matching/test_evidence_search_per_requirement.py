"""One call per requirement closes the channel between them.

Together, all requirements share one prompt and one answer, so what the model
returns for one can depend on what another asked about. Measured on the frozen
corpus, injecting a requirement nobody could meet flipped a *different*
requirement's status and took it off the failed must-have list.
"""

import pytest

from src.core.config import Settings, get_settings
from src.modules.matching.application import targeted_reparse
from src.modules.matching.application.targeted_reparse import (
    EvidenceSearchResult,
    _search_requirements,
)


class RecordingSearcher:
    """Answers each requirement with its own id, and remembers every prompt."""

    def __init__(self):
        self.prompts: list[list[str]] = []

    def search(self, raw_text, requirements):
        del raw_text
        self.prompts.append([item["requirement_id"] for item in requirements])
        return EvidenceSearchResult.model_validate(
            {
                "items": [
                    {"requirement_id": item["requirement_id"], "exact_quote": "Java"}
                    for item in requirements
                ]
            }
        )


def requirements(*ids):
    return [{"requirement_id": name, "label": name} for name in ids]


@pytest.fixture
def per_requirement(monkeypatch):
    def _set(enabled: bool):
        settings = Settings()
        object.__setattr__(
            settings, "matching_llm_evidence_search_per_requirement", enabled
        )
        monkeypatch.setattr(targeted_reparse, "get_settings", lambda: settings)

    return _set


class TestTogether:
    def test_the_default_sends_one_prompt_for_everything(self, per_requirement):
        per_requirement(False)
        searcher = RecordingSearcher()
        _search_requirements(searcher, "CV text", requirements("req-a", "req-b", "req-c"))
        assert searcher.prompts == [["req-a", "req-b", "req-c"]]


class TestOneAtATime:
    def test_each_requirement_gets_its_own_prompt(self, per_requirement):
        per_requirement(True)
        searcher = RecordingSearcher()
        _search_requirements(searcher, "CV text", requirements("req-a", "req-b", "req-c"))
        assert searcher.prompts == [["req-a"], ["req-b"], ["req-c"]]

    def test_no_prompt_mentions_another_requirement(self, per_requirement):
        """The channel is closed only if nothing else is in the prompt."""
        per_requirement(True)
        searcher = RecordingSearcher()
        _search_requirements(searcher, "CV text", requirements("req-a", "req-impossible"))
        assert all(len(prompt) == 1 for prompt in searcher.prompts)

    def test_every_answer_is_kept(self, per_requirement):
        per_requirement(True)
        searcher = RecordingSearcher()
        result = _search_requirements(
            searcher, "CV text", requirements("req-a", "req-b")
        )
        assert [item.requirement_id for item in result.items] == ["req-a", "req-b"]

    def test_a_single_requirement_is_not_split_further(self, per_requirement):
        per_requirement(True)
        searcher = RecordingSearcher()
        _search_requirements(searcher, "CV text", requirements("req-a"))
        assert searcher.prompts == [["req-a"]]

    def test_removing_a_requirement_leaves_the_others_prompt_untouched(
        self, per_requirement
    ):
        """What one requirement is asked must not depend on the posting.

        The two CVs differ so that neither ask answers the other from cache;
        the point here is the shape of the prompt, not its reuse.
        """
        per_requirement(True)
        with_extra = RecordingSearcher()
        _search_requirements(
            with_extra, "CV one", requirements("req-a", "req-impossible")
        )
        without = RecordingSearcher()
        _search_requirements(without, "CV two", requirements("req-a"))
        assert without.prompts[0] == with_extra.prompts[0] == ["req-a"]

    def test_the_same_requirement_on_the_same_cv_is_paid_for_once(
        self, per_requirement
    ):
        """Asking separately is what makes each answer reusable on its own."""
        per_requirement(True)
        first = RecordingSearcher()
        _search_requirements(first, "CV text", requirements("req-a", "req-b"))
        again = RecordingSearcher()
        _search_requirements(again, "CV text", requirements("req-a", "req-b"))
        assert first.prompts == [["req-a"], ["req-b"]]
        assert again.prompts == []


class TestSettingDefault:
    def test_it_is_on_now_that_a_run_has_measured_it(self):
        """Off until 2026-11-14, when the shared prompt was measured changing a
        different requirement's verdict. On costs roughly 3.7x the calls."""
        assert get_settings().matching_llm_evidence_search_per_requirement is True
