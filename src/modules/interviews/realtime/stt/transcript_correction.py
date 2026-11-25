"""Conservative, synchronous ASR repairs with auditable decisions.

An interviewer mentioning a technology only authorizes a candidate spelling.
The candidate's own clause must independently support using it as a term.
Unresolvable homophones remain unchanged; this module never completes answers.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class TermRule:
    canonical: str
    aliases: tuple[str, ...]
    context_names: tuple[str, ...] = ()
    disambiguators: tuple[str, ...] = ()
    local_evidence: tuple[str, ...] = ()


_NEXT_CUES = (
    "app router",
    "AppRouter",
    "pages router",
    "PagesRouter",
    "SSR",
    "server side rendering",
    "server-side rendering",
)

RULES = (
    TermRule("VIN University", ("virginie",)),
    TermRule("backend", ("bắt em", "bắc em", "bắt en")),
    TermRule("useState", ("diu xét", "you state", "use state")),
    TermRule("useEffect", ("diu effect", "you effect", "use effect")),
    TermRule("useEffect", ("vue effect",), local_evidence=("React",)),
    TermRule("useRef", ("use review", "use rếp")),
    TermRule("useReducer", ("use reduce",)),
    TermRule("Redux", ("rì đắc", "ri đắc")),
    TermRule("React", ("rì ác",), ("React", "React.js", "ReactJS")),
    TermRule("React.js", ("react js",), ("React", "React.js", "ReactJS")),
    TermRule("Vue.js", ("víu chây ét", "view js", "vue js"), ("Vue", "Vue.js", "VueJS")),
    TermRule("Node.js", ("nốt chây ét", "node js"), ("Node.js", "NodeJS")),
    TermRule("SQL", ("éc quy eo",)),
    TermRule("ORM", ("ô a em",)),
    TermRule("API", ("a pi ai",)),
    TermRule("Redis", ("rê đít",)),
    TermRule("Docker", ("đô cơ",)),
    TermRule("Kubernetes", ("cu bơ nét",)),
    TermRule("async/await", ("a sin a quây", "async await"), ("async/await", "async / await")),
    TermRule("Dependency Injection", ("đi pen đen xi in chéc sần",)),
    TermRule("TypeScript", ("type script",)),
    TermRule("JavaScript", ("java script",)),
    TermRule("MongoDB", ("mongo db",)),
    TermRule("MySQL", ("my sql",)),
    TermRule("PostgreSQL", ("postgre sql", "post gre sql", "post gre s q l", "pốt gre sql")),
    TermRule("Spring Boot", ("SpringBoot", "spring bút"), ("Spring Boot", "SpringBoot")),
    TermRule("type guard", ("type gard", "type gua"), local_evidence=("kiểu", "dữ liệu", "thuộc tính")),
    TermRule(
        "API boundary",
        ("api bao đờ ri", "a pi ai bao đờ ri"),
        local_evidence=("kiểm tra", "dữ liệu", "schema"),
    ),
    TermRule("unknown", ("in out",)),
    TermRule(
        "unknown", ("inl",), local_evidence=("kiểu", "thuộc tính", "schema", "narrow", "dữ liệu", "API")
    ),
    TermRule(
        "NestJS",
        ("nét chây ét", "netJS", "net.js", "net js"),
        ("NestJS", "Nest.js"),
        ("controller", "dependency injection", "decorator", "injectable"),
    ),
    TermRule(
        "Next.js",
        ("nét chây ét", "netJS", "net.js", "net js"),
        ("Next.js", "NextJS"),
        (*_NEXT_CUES, "React"),
        _NEXT_CUES,
    ),
    # This ASR output does not reliably distinguish any from unknown, even if
    # both appear in the prompt. Always retain it for clarification.
    TermRule("any", ("in in out",)),
    TermRule("unknown", ("in in out",)),
)


@dataclass(frozen=True)
class CorrectionDecision:
    start: int
    end: int
    original: str
    candidates: tuple[str, ...]
    replacement: str | None
    reason: str


@dataclass(frozen=True)
class CorrectionResult:
    text: str
    decisions: tuple[CorrectionDecision, ...]

    @property
    def edits(self) -> tuple[CorrectionDecision, ...]:
        return tuple(item for item in self.decisions if item.replacement is not None)


def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    return re.compile(r"(?<!\w)" + r"[^\S\r\n]+".join(map(re.escape, phrase.split())) + r"(?!\w)", re.I)


_ALIASES: dict[str, list[TermRule]] = {}
for _rule in RULES:
    for _alias in _rule.aliases:
        _ALIASES.setdefault(_alias, []).append(_rule)
_MATCHERS = tuple((_phrase_pattern(alias), rules) for alias, rules in _ALIASES.items())
_CANONICALS = tuple(_phrase_pattern(rule.canonical) for rule in RULES)
_CONTEXT = {
    rule: tuple(_phrase_pattern(name) for name in rule.context_names or (rule.canonical,)) for rule in RULES
}
_DISAMBIGUATORS = {rule: tuple(_phrase_pattern(cue) for cue in rule.disambiguators) for rule in RULES}
_LOCAL_EVIDENCE = {rule: tuple(_phrase_pattern(cue) for cue in rule.local_evidence) for rule in RULES}
_PROTECTED = re.compile(
    r"```[\s\S]*?```|`[^`\n]*`|\"[^\"\n]*\"|“[^”\n]*”|‘[^’\n]*’"
    r"|(?<!\w)'[^'\n]+'(?!\w)|https?://\S+|www\.\S+|\S+@\S+"
    r"|(?:[A-Za-z]:\\|/)[^\s,;]+|\b[\w.-]+\.(?:py|js|ts|tsx|jsx|json|sql|md)\b"
    r"|\b[A-Za-z_]\w*_[\w]+\b"
)
_CLAUSE_BREAK = re.compile(r"[.!?;:\n]|(?:\s+nhưng\s+)", re.I)
_USAGE_BEFORE = re.compile(
    r"\b(?:dùng|sử dụng|triển khai|tích hợp|cài đặt|cấu hình|gọi|chạy|học|chọn|áp dụng|"
    r"làm với|biết|viết bằng|chuyển sang|thay vì|khác với|so với|kiểu|response là|hook|"
    r"framework|thư viện|công nghệ|cơ sở dữ liệu|công cụ|là)\s+(?:cả\s+|thêm\s+)?$",
    re.I,
)
_VI_CLAUSE = re.compile(r"\b(?:em|mình|tôi|dùng|sử dụng|không|chưa|kiểu|thuộc tính|hàm|và|để)\b", re.I)
_TERM_AFTER = re.compile(
    r"^\s+(?:để|giúp|cho phép|không cho phép|là|dùng|có thể|thì|giữ|quản lý|xử lý|"
    r"lưu|kiểm tra|hoạt động|khác)\b",
    re.I,
)
_NATURAL_CONTINUATION = re.compile(
    r"^\s+(?:(?:developer|engineer)\s+)?"
    r"(?:phải|nộp|đi|chờ|mang|gửi|học|làm|viết|trả|đóng|xin|ký|chạy|nghỉ)\b",
    re.I,
)
_LITERAL_CUE = re.compile(
    r"\b(?:nguyên văn|chuỗi|tên biến|tên hàm|tên file|tên tệp|tên người|biệt danh|"
    r"cụm từ|tiếng anh|ghi đúng là|tên khách hàng|ngữ pháp|trích dẫn|phiên âm|"
    r"tên gói|tên package|identifier)\b",
    re.I,
)
_CODE_CUE = re.compile(r"\b(?:const|let|var|def|function|class)\b[^.!?;\n]*(?:=|\(|\{)", re.I)
_PERSON_BEFORE = re.compile(r"\b(?:anh|chị|ông|bà|bác|cô|chú|bạn|gặp)\s+$", re.I)
_NATURAL_MEANING_AFTER = re.compile(
    r"^\s+là\s+(?:bạn|người|anh|chị|đồng nghiệp|sinh viên|học sinh|mã|địa chỉ|chuỗi|tên)\b", re.I
)
_IO = re.compile(r"\b(?:input|output|đầu vào|đầu ra|stdin|stdout|i/o|in/out)\b", re.I)
_TYPE = re.compile(
    r"\b(?:kiểu|type|thuộc tính|narrow|schema|type guard|response|"
    r"dữ liệu (?:chưa|không) (?:được )?(?:xác minh|xác định|rõ))\b",
    re.I,
)
_ENGLISH_PHRASE_OPENING = re.compile(
    r"^\s+(?:the|a|an|to|for|with|query|requirements|commands|comments|machines?)\b", re.I
)


def _normalized_source(text: str) -> tuple[str, list[tuple[int, int]]]:
    """Match NFC Vietnamese while retaining offsets into the original spelling."""
    chars: list[str] = []
    offsets: list[tuple[int, int]] = []
    for match in re.finditer(r"[\s\S][\u0300-\u036f]*", text):
        cluster = unicodedata.normalize("NFC", match.group())
        chars.append(cluster)
        offsets.extend([(match.start(), match.end())] * len(cluster))
    return "".join(chars), offsets


def _clause(text: str, start: int, end: int) -> tuple[str, str, str]:
    left, right = 0, len(text)
    for boundary in _CLAUSE_BREAK.finditer(text):
        if boundary.end() <= start:
            left = boundary.end()
        elif boundary.start() >= end:
            right = boundary.start()
            break
    return text[left:right], text[left:start], text[end:right]


def _term_list_before(before: str) -> bool:
    usage = re.search(r"\b(?:dùng|sử dụng|gồm|bao gồm)\s+(.+)$", before, re.I)
    if not usage:
        return False
    tail = usage.group(1)
    patterns = [pattern for pattern, _ in _MATCHERS] + list(_CANONICALS)
    for pattern in sorted(patterns, key=lambda item: len(item.pattern), reverse=True):
        tail = pattern.sub("", tail)
    return bool(re.fullmatch(r"(?:\s|,|\bvà\b|\bhay\b|\bhoặc\b|\bcả\b|\bthêm\b)+", tail, re.I))


def _positive_cue(pattern: re.Pattern[str], clause: str) -> bool:
    """A rejected framework is not positive evidence for its homophone."""
    for match in pattern.finditer(clause):
        prefix = clause[max(0, match.start() - 70) : match.start()].rsplit(",", 1)[-1]
        if not re.search(r"\b(?:không|chưa|chẳng|tránh|without)\b(?:\s+\w+){0,5}\s*$", prefix, re.I):
            return True
    return False


def _support(rule: TermRule, clause: str, before: str, after: str) -> str:
    if _LITERAL_CUE.search(clause):
        return "literal_or_name"
    if _CODE_CUE.search(clause):
        return "code_or_identifier"
    if _PERSON_BEFORE.search(before) or _NATURAL_MEANING_AFTER.match(after):
        return "ordinary_entity_meaning"
    if rule.local_evidence:
        supported = any(
            _positive_cue(pattern, clause)
            if rule.canonical in {"useEffect", "Next.js"}
            else pattern.search(clause)
            for pattern in _LOCAL_EVIDENCE[rule]
        )
        if not supported:
            return "insufficient_local_evidence"
    local = before.rsplit(",", 1)[-1] + after.split(",", 1)[0]
    local_has_vi_marks = any(
        unicodedata.combining(char) or char == "đ" for char in unicodedata.normalize("NFD", local.casefold())
    )
    english_collision = rule.canonical in {
        "useState",
        "useEffect",
        "useRef",
        "useReducer",
        "MySQL",
        "TypeScript",
        "JavaScript",
    }
    suffix = after.split(",", 1)[0]
    opening = _ENGLISH_PHRASE_OPENING.match(suffix)
    if (
        english_collision
        and opening
        and re.match(r"\s+[A-Za-z][A-Za-z0-9/-]*(?!\w)", suffix[opening.end() :])
    ):
        return "possible_english_sentence"
    if (
        len(re.findall(r"\b\w+\b", after.split(",", 1)[0])) >= 3
        and not _VI_CLAUSE.search(local)
        and not local_has_vi_marks
    ):
        return "possible_english_sentence"
    if rule.canonical == "backend":
        if _NATURAL_CONTINUATION.match(after):
            return "ordinary_vietnamese"
        role = re.search(r"\b(?:làm|ứng tuyển|vị trí|lập trình)\s+$", before, re.I)
        suffix = re.match(r"\s+(?:developer|engineer|api|server)\b", after, re.I)
        if not suffix and re.search(
            r"\b(?:nhà trường|thầy|cô|công ty|công việc|sếp|họ)\b[^,;.!?]{0,45}$",
            before.rsplit(",", 1)[-1],
            re.I,
        ):
            return "ordinary_vietnamese"
        return "supported_usage" if role or suffix else "insufficient_local_evidence"
    if rule.canonical == "VIN University":
        return "proper_name_requires_confirmation"
    if rule.canonical == "unknown":
        if _IO.search(clause):
            return "input_output_meaning"
        if not _TYPE.search(clause):
            return "insufficient_type_evidence"
        if re.search(r"\b(?:với|còn|thay vì)\s+$", before, re.I) and re.match(
            r"[\s,]+(?:(?:em|mình|tôi)\s+)?(?:(?:phải|cần)\s+)?kiểm tra\b",
            after,
            re.I,
        ):
            return "supported_usage"
    if rule.canonical == "API boundary" and re.search(r"\b(?:ở|tại|qua)\s+$", before, re.I):
        return "supported_usage"
    if rule.disambiguators and re.search(r"\btrong\s+$", before, re.I):
        return "supported_usage"
    if rule.canonical == "useState" and re.search(r"^\s+machines?\b|\bstate\s+machines?\b", after, re.I):
        return "state_machine_meaning"
    if rule.canonical == "useRef" and re.search(
        r"\breview\s+code\b|\bpull request\b|\bđánh giá\b", clause, re.I
    ):
        return "review_meaning"
    if rule.canonical == "useReducer" and re.search(r"\b(?:mảng|array|tính tổng)\b", clause, re.I):
        return "array_reduce_meaning"
    if not _VI_CLAUSE.search(clause):
        return "possible_english_sentence"
    if _USAGE_BEFORE.search(before) or _TERM_AFTER.match(after):
        return "supported_usage"
    # A list introduced by a Vietnamese technical usage predicate. Limit the
    # lookback to this clause, so cues in an earlier sentence cannot leak in.
    if _term_list_before(before):
        return "supported_usage"
    return "insufficient_local_evidence"


def correct_technical_transcript(
    text: str,
    *,
    context: str = "",
    locale: str = "vi-VN",
    is_final: bool = True,
) -> CorrectionResult:
    """Repair only corroborated terms; report abstentions without guessing text."""
    if not text or not locale.lower().startswith("vi"):
        return CorrectionResult(text, ())
    source, offsets = _normalized_source(text)
    context = unicodedata.normalize("NFC", context)
    protected = [match.span() for match in _PROTECTED.finditer(source)]
    matches = sorted(
        ((match, rules) for pattern, rules in _MATCHERS for match in pattern.finditer(source)),
        key=lambda item: (item[0].start(), -len(item[0].group())),
    )
    decisions: list[CorrectionDecision] = []
    consumed = 0
    for match, rules in matches:
        if match.start() < consumed:
            continue
        consumed = match.end()  # Long ambiguous phrases must not be partially repaired.
        start, end = offsets[match.start()][0], offsets[match.end() - 1][1]
        candidates = tuple(dict.fromkeys(rule.canonical for rule in rules))
        replacement = None
        if any(match.start() < stop and match.end() > begin for begin, stop in protected):
            reason = "protected_text"
        elif match.group().casefold() == "in in out":
            reason = "unresolved_homophone"
        elif match.group() == "INL":
            reason = "ambiguous_acronym"
        else:
            confirmed = [rule for rule in rules if any(pattern.search(context) for pattern in _CONTEXT[rule])]
            clause, before, after = _clause(source, match.start(), match.end())
            if not confirmed:
                reason = "missing_context"
            else:
                supported = confirmed
                if len(candidates) > 1:
                    observed = [
                        rule
                        for rule in rules
                        if any(_positive_cue(pattern, clause) for pattern in _DISAMBIGUATORS[rule])
                    ]
                    supported = [rule for rule in observed if rule in confirmed] if len(observed) == 1 else []
                if len(supported) != 1:
                    reason = "ambiguous_context" if len(confirmed) > 1 else "insufficient_disambiguation"
                else:
                    reason = _support(supported[0], clause, before, after)
                    if reason == "supported_usage":
                        # A fragment ending in "...làm bắt em" may become
                        # "...bắt em phải..." in the next ASR update. Do not
                        # repair its tail before right-hand context arrives.
                        if not is_final and len(re.findall(r"\b\w+\b", after)) < 3:
                            reason = "unstable_interim_tail"
                        else:
                            replacement = supported[0].canonical
        decisions.append(CorrectionDecision(start, end, text[start:end], candidates, replacement, reason))
    chunks: list[str] = []
    cursor = 0
    for decision in decisions:
        if decision.replacement is not None:
            chunks.extend((text[cursor : decision.start], decision.replacement))
            cursor = decision.end
    chunks.append(text[cursor:])
    return CorrectionResult("".join(chunks), tuple(decisions))
