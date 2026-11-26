"""Candidate-selected interviewer tone instructions."""


def candidate_tone_instruction(tone: str | None, *, is_vi: bool) -> str:
    normalized = (tone or "standard").strip().lower()
    if normalized == "supportive":
        instruction = (
            "Use a warm, encouraging and respectful interviewer voice. Acknowledge effort briefly, "
            "but do not overpraise, give away answers, or soften factual evaluation."
            if not is_vi
            else "Dùng giọng phỏng vấn ấm áp, khích lệ và tôn trọng. Ghi nhận nỗ lực ngắn gọn, "
            "nhưng không khen quá mức, không tiết lộ đáp án và không làm nhẹ nhận xét chuyên môn."
        )
    elif normalized == "strict":
        instruction = (
            "Use a direct, rigorous and concise interviewer voice. Ask for precise evidence and "
            "challenge vague claims while remaining respectful; never shame or intimidate the candidate."
            if not is_vi
            else "Dùng giọng phỏng vấn thẳng thắn, nghiêm túc và súc tích. Yêu cầu dẫn chứng cụ thể, "
            "hỏi sâu khi câu trả lời còn chung chung nhưng luôn tôn trọng; không chê bai hay gây áp lực."
        )
    else:
        return ""
    return (
        f"\n\nCandidate-selected interviewer tone: {normalized}\n"
        "Apply only to candidate-facing wording; do not change assessment criteria, sufficiency "
        "decisions, evidence requirements, or scores.\n"
        + instruction
    )
