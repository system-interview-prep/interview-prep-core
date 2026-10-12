from src.modules.interviews.adapters.voice.tts_cache import SpeechAudioCache, SpeechClip

_CLIP = SpeechClip(pcm=b"\x01\x00" * 4800, sample_rate=24000, channels=1)  # 200 ms


def test_replayed_question_comes_from_memory_and_counts_saved_characters():
    cache = SpeechAudioCache(voice_signature="eleven|v1", directory=None)
    question = "Bạn đã dùng Redis để cache những dữ liệu nào?"
    assert cache.get(question) is None

    cache.put(question, _CLIP, persist=False)

    assert cache.get(question) == _CLIP
    assert (cache.hits, cache.saved_characters) == (1, len(question))


def test_fixed_lines_are_shared_on_disk_across_sessions(tmp_path):
    line = "Mình đang xem câu trả lời, chờ mình một chút nhé."
    SpeechAudioCache(voice_signature="eleven|v1", directory=tmp_path).put(line, _CLIP, persist=True)

    next_session = SpeechAudioCache(voice_signature="eleven|v1", directory=tmp_path)
    assert next_session.get(line) == _CLIP
    # Audio of one voice is never replayed for another.
    assert SpeechAudioCache(voice_signature="cartesia|x", directory=tmp_path).get(line) is None
    assert not list(tmp_path.glob("*.part"))


def test_session_speech_is_not_written_to_disk(tmp_path):
    cache = SpeechAudioCache(voice_signature="eleven|v1", directory=tmp_path)
    cache.put("Chào Lan, mình là người phỏng vấn.", _CLIP, persist=False)
    assert not list(tmp_path.iterdir())


def test_memory_is_bounded_and_clips_split_into_20ms_frames():
    cache = SpeechAudioCache(voice_signature="v", directory=None, memory_size=2)
    for index in range(3):
        cache.put(f"câu {index}", _CLIP, persist=False)
    assert cache.get("câu 0") is None
    assert cache.get("câu 2") is not None

    pieces = _CLIP.chunks()
    assert len(pieces) == 10 and all(len(piece) == 960 for piece in pieces)
