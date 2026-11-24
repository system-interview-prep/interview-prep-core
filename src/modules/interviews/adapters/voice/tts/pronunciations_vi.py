"""Cách đọc cho giọng tiếng Việt: một file dữ liệu, sửa ở đây khi nghe thấy đọc sai.

Mỗi dòng là (mẫu regex, cách đọc). Chỉ áp cho văn bản đưa vào TTS
(``language.speakable_text``), chữ trên màn hình không đổi. Chỉ chạy khi
cho cách đọc lời thoại tiếng Việt trong voice.

Danh sách bắt đầu từ những ký hiệu chắc chắn không đọc lên được. Thuật ngữ
VieNeu đọc sai thật sự (API, SQL, JWT…) chỉ thêm sau khi nghe hoặc chạy
``scripts/tts_roundtrip.py`` (cho TTS đọc rồi STT nghe lại, liệt kê từ sai):
đổi cách đọc một thuật ngữ mà giọng vốn đọc đúng sẽ làm nó sai đi.

File không import gì để voice agent (tiến trình riêng, không có ``src``) cũng nạp được.
"""

PRONUNCIATIONS_VI: tuple[tuple[str, str], ...] = (
    (r"(?<!\w)k8s(?!\w)", "Kubernetes"),
    (r"(?<!\w)e\.g\.", "ví dụ"),
    (r"(?<!\w)i\.e\.", "tức là"),
    (r"(?<!\w)etc\.?(?!\w)", "vân vân"),
    (r"(?<!\w)vs\.?(?!\w)", "so với"),
    (r"(?<!\w)N/A(?!\w)", "không có"),
    (r"\s*->\s*", " sang "),
    (r"[~≈]\s*(?=\d)", "khoảng "),
)
