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
    # Only planned stages, in order, each once; closing promised conditionally.
    assert "dự án trong CV, kiến thức chuyên môn, một vài tình huống khó hơn và cách bạn làm việc nhóm" in text
    assert text.endswith(warmup_question(is_vi=True, is_demo=False))
    # The chat room shows the question, suggestions and report link itself.
    chat = _opening(mode="text")
    assert "Nếu còn thời gian" not in chat
    assert "báo cáo" not in chat


def test_how_to_answer_names_only_controls_the_room_has():
    # Spoken openings do not include a long intro or answer-control instructions.
    for mode in ("voice", "video"):
        spoken = _opening(mode=mode)
        assert "nói “xong”" not in spoken
        assert "bấm “Trả lời xong”" not in spoken
        greeting, _, first_question = spoken.partition(". ")
        assert len(greeting.split()) <= 3
        assert first_question == warmup_question(is_vi=True, is_demo=False)
    chat = _opening(mode="text")
    assert "gợi ý" not in chat


def test_opening_is_split_into_short_paragraphs_with_the_question_alone():
    demo_question = warmup_question(is_vi=True, is_demo=True)
    for text, question in (
        (_opening(mode="text"), warmup_question(is_vi=True, is_demo=False)),
        (_opening(mode="text", duration_minutes=3, first_question=demo_question), demo_question),
    ):
        paragraphs = text.split("\n\n")
        assert paragraphs[-1] == question
        # Each preamble paragraph becomes one transcript bubble: a few short sentences.
        for paragraph in paragraphs[:-1]:
            assert len(paragraph.split()) <= 30, paragraph
            assert paragraph.count(".") <= 3, paragraph


def test_demo_opening_stays_short():
    demo = _opening(mode="text", duration_minutes=3, first_question=warmup_question(is_vi=True, is_demo=True))
    assert "Buổi demo ba phút cho vị trí Software Engineer, Backend" in demo
    assert "Mình sẽ hỏi về" not in demo
    # A three-minute demo: about ten seconds of preliminaries at most.
    assert len(" ".join(demo.split("\n\n")[:-1]).split()) <= 40
    spoken_demo = _opening(
        mode="voice", duration_minutes=3, first_question=warmup_question(is_vi=True, is_demo=True)
    )
    assert "nói “xong”" not in spoken_demo
    assert "bấm “Trả lời xong”" not in spoken_demo


def test_warmup_does_not_repeat_the_cv_project_question():
    assert "dự án" not in warmup_question(is_vi=True, is_demo=False)


def test_spoken_sentences_stay_short():
    for sentence in _opening().replace("\n\n", " ").replace(";", ".").split(". "):
        assert len(sentence.split()) <= 32, sentence


def test_english_opening():
    text = _opening(mode="text", is_vi=False, call_name="Hien", first_question=warmup_question(is_vi=False, is_demo=False))
    assert text.startswith("Hi Hien, I'm the AI interviewer from Intervia.")
    assert "say “done”" not in text
    assert "press “Done answering”" not in text
