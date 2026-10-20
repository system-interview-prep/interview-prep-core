from src.modules.interviews.core.opening_script import build_opening, warmup_question

_STAGES = ["WARM_UP", "VALIDATE", "DEEP_DIVE", "DEEP_DIVE", "CHALLENGE", "BEHAVIORAL"]


def _opening(**overrides):
    kwargs = dict(
        is_vi=True,
        mode="voice",
        duration_minutes=25,
        job_title="Software Engineer, Backend",
        call_name="Hiền",
        planned_stages=_STAGES,
        first_question=warmup_question(is_vi=True, is_demo=False),
    )
    kwargs.update(overrides)
    return build_opening(**kwargs)


def test_opening_greets_by_call_name_and_states_purpose_length_and_agenda():
    text = _opening(mode="text")
    assert text.startswith("Chào Hiền, mình là người phỏng vấn AI của Intervia.")
    assert "vị trí Software Engineer, Backend, khoảng 25 phút" in text
    # Only planned stages, in order, each once.
    assert "Mình sẽ hỏi về dự án trong CV, chuyên môn, vài tình huống và cách bạn làm việc nhóm." in text
    assert text.endswith(warmup_question(is_vi=True, is_demo=False))
    # The room shows the report link itself.
    assert "báo cáo" not in text


def test_every_room_hears_the_same_opening():
    chat = _opening(mode="text")
    assert _opening(mode="voice") == chat
    assert _opening(mode="video") == chat
    assert "hãy" not in chat.casefold()
    assert "nói “xong”" not in chat
    assert "bấm “Trả lời xong”" not in chat


def test_opening_is_one_intro_paragraph_then_the_question():
    demo_question = warmup_question(is_vi=True, is_demo=True)
    for text, question in (
        (_opening(mode="text"), warmup_question(is_vi=True, is_demo=False)),
        (_opening(mode="text", duration_minutes=3, first_question=demo_question), demo_question),
    ):
        paragraphs = text.split("\n\n")
        assert len(paragraphs) == 2
        assert paragraphs[-1] == question
        # Spoken in about twenty seconds.
        assert len(paragraphs[0].split()) <= 50, paragraphs[0]


def test_demo_opening_stays_short():
    demo = _opening(mode="text", duration_minutes=3, first_question=warmup_question(is_vi=True, is_demo=True))
    assert "Buổi demo ba phút, mình hỏi nhanh vài câu." in demo
    assert "Mình sẽ hỏi về" not in demo
    assert len(demo.split("\n\n")[0].split()) <= 25


def test_warmup_does_not_repeat_the_cv_project_question():
    assert "dự án" not in warmup_question(is_vi=True, is_demo=False)


def test_spoken_sentences_stay_short():
    for sentence in _opening().replace("\n\n", " ").replace(";", ".").split(". "):
        assert len(sentence.split()) <= 25, sentence


def test_warmup_refers_to_the_candidate_without_mình():
    for is_vi, is_demo in ((True, False), (True, True)):
        assert "về mình" not in warmup_question(is_vi=is_vi, is_demo=is_demo)


def test_english_opening():
    text = _opening(mode="text", is_vi=False, call_name="Hien", first_question=warmup_question(is_vi=False, is_demo=False))
    assert text.startswith("Hi Hien, I'm the AI interviewer from Intervia.")
    assert "say “done”" not in text
    assert "press “Done answering”" not in text
