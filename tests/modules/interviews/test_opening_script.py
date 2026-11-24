from src.modules.interviews.core.dialogue.opening import build_opening, warmup_question

_STAGES = ["WARM_UP", "VALIDATE", "DEEP_DIVE", "DEEP_DIVE", "CHALLENGE", "BEHAVIORAL"]


def _opening(**overrides):
    kwargs = dict(
        is_vi=True,
        mode="voice",
        duration_minutes=25,
        job_title="Software Engineer, Backend",
        call_name="Hiền",
        planned_stages=_STAGES,
    )
    kwargs.update(overrides)
    return build_opening(**kwargs)


def test_opening_greets_by_call_name_and_states_purpose_length_and_agenda():
    text = _opening(mode="text")
    assert text.startswith("Chào Hiền, mình là người phỏng vấn AI của Intervia.")
    assert "vị trí Software Engineer, Backend, khoảng 25 phút" in text
    # Only planned stages, in order, each once.
    assert "Mình sẽ hỏi về dự án trong CV, chuyên môn, vài tình huống và cách bạn làm việc nhóm." in text
    assert warmup_question(is_vi=True, is_demo=False) not in text
    # The room shows the report link itself.
    assert "báo cáo" not in text


def test_every_room_hears_the_same_opening():
    chat = _opening(mode="text")
    assert _opening(mode="voice") == chat
    assert _opening(mode="video") == chat
    assert "hãy" not in chat.casefold()
    assert "nói “xong”" not in chat
    assert "bấm “Trả lời xong”" not in chat


def test_opening_script_is_independent_of_the_first_question():
    text = _opening(mode="text")
    assert "\n\n" not in text
    assert warmup_question(is_vi=True, is_demo=False) not in text
    # Spoken in about twenty seconds.
    assert len(text.split()) <= 50, text


def test_demo_opening_stays_short():
    demo = _opening(mode="text", duration_minutes=3)
    assert "Buổi demo ba phút, mình hỏi nhanh vài câu." in demo
    assert "Mình sẽ hỏi về" not in demo
    assert len(demo.split()) <= 25


def test_warmup_does_not_repeat_the_cv_project_question():
    assert "dự án" not in warmup_question(is_vi=True, is_demo=False)


def test_spoken_sentences_stay_short():
    for sentence in _opening().replace("\n\n", " ").replace(";", ".").split(". "):
        assert len(sentence.split()) <= 25, sentence


def test_warmup_refers_to_the_candidate_without_mình():
    for is_vi, is_demo in ((True, False), (True, True)):
        assert "về mình" not in warmup_question(is_vi=is_vi, is_demo=is_demo)


def test_english_opening():
    text = _opening(mode="text", is_vi=False, call_name="Hien")
    assert text.startswith("Hi Hien, I'm the AI interviewer from Intervia.")
    assert "say “done”" not in text
    assert "press “Done answering”" not in text
