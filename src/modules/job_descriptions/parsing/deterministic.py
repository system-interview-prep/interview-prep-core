import re
import unicodedata
from datetime import datetime, timezone
from hashlib import sha1

from src.modules.job_descriptions.domain.schemas import CanonicalJobDescription, GroundedJobText, JobRequirement
from src.modules.user_cvs.domain.schemas import CareerClassification, ParsingMetadata, TaxonomyRef
from src.modules.user_cvs.parsing.domain.source import EvidenceMapper, SourceDocument

_SKILLS = {
    "skill-unity": ("Unity", ("unity", "unity engine")), "skill-java": ("Java", ("java",)),
    "skill-spring-boot": ("Spring Boot", ("spring boot",)), "skill-python": ("Python", ("python",)),
    "skill-fastapi": ("FastAPI", ("fastapi",)), "skill-react": ("React", ("react", "reactjs")),
    "skill-javascript": ("JavaScript", ("javascript",)), "skill-typescript": ("TypeScript", ("typescript",)),
    "skill-docker": ("Docker", ("docker",)), "skill-kubernetes": ("Kubernetes", ("kubernetes", "k8s")),
    "skill-aws": ("AWS", ("aws",)), "skill-git": ("Git", ("git",)), "skill-json": ("JSON", ("json",)),
    "skill-sql": ("SQL", ("sql",)),
    "skill-http": ("HTTP", ("http",)), "skill-android": ("Android", ("android",)), "skill-ios": ("iOS", ("ios",)),
    "skill-artificial-intelligence": ("Artificial Intelligence", ("artificial intelligence", "ai")),
    "skill-machine-learning": ("Machine Learning", ("machine learning", "ml")),
    "skill-natural-language-processing": ("Natural Language Processing", ("natural language processing", "nlp")),
    "skill-generative-ai": ("Generative AI", ("generative ai", "genai")),
    "skill-large-language-models": ("Large Language Models", ("large language model", "large language models", "llm")),
}
PARSER_VERSION = "deterministic-jd-v4"
_HEADERS = {
    "requirements": {
        "requirements", "qualifications", "minimum qualifications", "basic qualifications",
        "required qualifications", "required skills", "skills and qualifications", "education and experience",
        "what you bring", "what were looking for", "what we are looking for", "candidate requirements",
        "yeu cau", "yeu cau ung vien", "yeu cau ung tuyen",
    },
    "preferred": {"preferred", "nice to have", "uu tien"},
    "responsibilities": {
        "responsibilities", "key responsibilities", "responsibilities and duties", "duties",
        "what you will do", "what youll do", "the role", "about the role", "about the job", "job description",
        "mo ta cong viec", "trach nhiem",
    },
    "benefits": {"benefits", "benefits and perks", "compensation and benefits", "what we offer", "perks", "why join us", "quyen loi", "phuc loi"},
    "location": {"location", "dia diem", "dia diem lam viec"},
    # This is a section boundary only.  Without it, a requirements section can
    # accidentally consume application instructions at the end of a Vietnamese JD.
    "application": {"how to apply", "cach thuc ung tuyen", "cach ung tuyen"},
}


def _key(value: str) -> str:
    value = unicodedata.normalize("NFD", value.casefold())
    value = "".join(char for char in value if unicodedata.category(char) != "Mn")
    value = value.replace("đ", "d")
    return re.sub(r"[^a-z0-9 ]+", " ", value).strip()


def _evidence_id(kind: str, start: int, end: int) -> str:
    return f"ev-jd-{kind}-{sha1(f'{start}:{end}'.encode(), usedforsecurity=False).hexdigest()[:12]}"


def _lines(text: str) -> list[tuple[int, str]]:
    return [(match.start(), match.group().strip()) for match in re.finditer(r"(?m)^.*$", text)]


def _heading(line: str) -> str | None:
    line = re.sub(r"^(?:[-•o#*]+\s*)+", "", line).rstrip(":").strip()
    key = _key(line)
    # Recruitment posts commonly decorate a heading (e.g. "Yêu cầu ứng tuyển"
    # or "Quyền lợi dành cho bạn").  Match the known heading as a complete
    # leading phrase, never as an arbitrary substring in normal prose.
    return next(
        (kind for kind, values in _HEADERS.items() if any(key == value or key.startswith(f"{value} ") for value in values)),
        None,
    )


def _ranges(text: str) -> dict[str, tuple[int, int]]:
    headings = [(offset, kind) for offset, line in _lines(text) if (kind := _heading(line)) and kind != "preferred"]
    result: dict[str, tuple[int, int]] = {}
    for index, (start, kind) in enumerate(headings):
        # A company introduction can reuse "Mô tả công việc" before the actual
        # task list.  The later heading is the structured section candidates
        # must review, so retain the last occurrence.
        if kind in {"requirements", "responsibilities", "benefits"}:
            result[kind] = (start, headings[index + 1][0] if index + 1 < len(headings) else len(text))
    return result


def _bullet(line: str) -> tuple[int, str] | None:
    match = re.match(r"\s*(?:[-•o*]+\s*)+(?P<value>.+?)\s*$", line)
    if not match or not (value := match.group("value").strip()):
        return None
    return match.start("value"), value


class DeterministicJobDescriptionParser:
    """Evidence-grounded English/Vietnamese JD parser; no inferred LLM claims."""

    def __init__(self, taxonomy: dict[str, tuple[str, tuple[str, ...]]] | None = None, taxonomy_version: str = "internal-2026.1") -> None:
        self._taxonomy = taxonomy or _SKILLS
        self._taxonomy_version = taxonomy_version

    def parse(self, source: SourceDocument, *, extraction_version: str, artifact_key: str | None = None) -> CanonicalJobDescription:
        mapper, evidence, ranges = EvidenceMapper(source), {}, _ranges(source.text)
        requirements = self._requirements(source, mapper, evidence, ranges.get("requirements"))
        return CanonicalJobDescription(
            schemaVersion="1.0", jobTitle=self._title(source, ranges), careerClassifications=self._classifications(requirements),
            seniority=self._seniority(self._title(source, ranges) or ""), employmentType=self._employment_type(source.text), workMode=self._work_mode(source.text),
            location=self._location(source), responsibilities=self._texts(source, mapper, evidence, ranges.get("responsibilities"), "responsibility"),
            requirements=requirements, benefits=self._texts(source, mapper, evidence, ranges.get("benefits"), "benefit"), evidence=list(evidence.values()),
            parsing=ParsingMetadata(parserVersion=PARSER_VERSION, extractionVersion=extraction_version, parsedAt=datetime.now(timezone.utc), status="review_required", sourceArtifactKey=artifact_key),
        )

    def _requirements(self, source, mapper, evidence, section):
        if not section:
            return []
        start, end, priority, result = *section, "must_have", []
        for offset, line in _lines(source.text[start:end]):
            if _heading(line) == "preferred":
                priority = "preferred"
                continue
            bullet = _bullet(line)
            # Production JDs frequently put an entire qualification paragraph
            # under a heading rather than using bullets.  Preserve that
            # paragraph as one evidence-grounded requirement instead of
            # silently dropping it.
            if bullet:
                relative_start, value = bullet
            elif line and not _heading(line):
                relative_start, value = 0, line.strip()
            else:
                continue
            line_priority = "preferred" if "preferred" in _key(value) else priority
            absolute_start = start + offset + relative_start
            found_skill = False
            for concept_id, (label, aliases) in self._taxonomy.items():
                match = next((re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", value, re.I) for alias in aliases if re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", value, re.I)), None)
                if not match:
                    continue
                found_skill = True
                skill_start, skill_end = absolute_start + match.start(), absolute_start + match.end()
                ref = _evidence_id("requirement", skill_start, skill_end)
                evidence[ref] = mapper.from_offsets(evidence_id=ref, char_start=skill_start, char_end=skill_end)
                result.append(JobRequirement(requirementId=f"req-{concept_id}-{skill_start}", kind="skill", priority=line_priority, concept=TaxonomyRef(conceptId=concept_id, scheme="internal", taxonomyVersion=self._taxonomy_version, label=label), rawLabel=source.text[skill_start:skill_end], minimumExperienceMonths=self._experience_months(value), evidenceRefs=[ref]))
            # Preserve eligibility, education, language, and domain requirements
            # even when they do not map to the small deterministic skill catalog.
            if not found_skill:
                absolute_end = absolute_start + len(value)
                ref = _evidence_id("requirement", absolute_start, absolute_end)
                evidence[ref] = mapper.from_offsets(evidence_id=ref, char_start=absolute_start, char_end=absolute_end)
                result.append(JobRequirement(requirementId=f"req-other-{absolute_start}", kind="other", priority=line_priority, rawLabel=value, minimumExperienceMonths=self._experience_months(value), evidenceRefs=[ref]))
        return result

    def _texts(self, source, mapper, evidence, section, kind):
        if not section:
            return []
        start, end = section
        result = []
        for offset, line in _lines(source.text[start:end]):
            bullet = _bullet(line)
            if _heading(line):
                continue
            if bullet:
                relative_start, value = bullet
            elif kind in {"responsibility", "benefit"} and line:
                relative_start, value = 0, line
            else:
                continue
            if len(value) < 3:
                continue
            absolute_start, absolute_end = start + offset + relative_start, start + offset + relative_start + len(value)
            ref = _evidence_id(kind, absolute_start, absolute_end)
            evidence[ref] = mapper.from_offsets(evidence_id=ref, char_start=absolute_start, char_end=absolute_end)
            result.append(GroundedJobText(text=value, evidenceRefs=[ref]))
        return result

    @staticmethod
    def _title(source: SourceDocument, ranges: dict[str, tuple[int, int]]) -> str | None:
        for _, line in _lines(source.text):
            label, separator, value = line.partition(":")
            if separator and _key(label) in {"job title", "position", "vi tri", "chuc danh"} and value.strip():
                return value.strip()
        # Titles are often a standalone line, or occur in a recruitment
        # sentence rather than an explicit "Job title:" label.  Select only
        # role-shaped phrases, never generic headings such as "description".
        role_words = r"(?:engineer|developer|designer|manager|specialist|consultant|coordinator|analyst|architect|administrator|recruiter|representative|director|intern|assistant|officer|technician)"
        for _, line in _lines(source.text):
            candidate = line.strip(" -:\t")
            normalized = _key(candidate)
            if not candidate or _heading(candidate) or len(candidate) > 100:
                continue
            if re.fullmatch(rf"(?:[a-z0-9 .&/+-]+\s+)?{role_words}(?:\s+[a-z0-9 .&/+-]+)?", normalized):
                return candidate
        patterns = (
            rf"(?:looking|searching|hiring)\s+for\s+(?:an?\s+)?(?:talented\s+)?(?P<title>[a-z0-9 .&/+-]*{role_words})",
            rf"(?:join\s+(?:us|our team)\s+as\s+)(?:an?\s+)?(?P<title>[a-z0-9 .&/+-]*{role_words})",
            rf"(?:position|role)\s+(?:is|for)\s+(?:an?\s+)?(?P<title>[a-z0-9 .&/+-]*{role_words})",
        )
        for match in re.finditer(r"(?s).{0,120}", source.text.lower()):
            excerpt = match.group()
            for pattern in patterns:
                found = re.search(pattern, excerpt)
                if found:
                    return found.group("title").strip(" .,;:-")
        first_section = min((start for start, _ in ranges.values()), default=len(source.text))
        return next((block.text.strip() for block in source.blocks if block.char_start < first_section and 3 <= len(block.text.strip()) <= 180 and not _heading(block.text)), None)

    @staticmethod
    def _experience_months(text: str) -> int | None:
        normalized = _key(text)
        match = re.search(r"(?:it nhat\s*)?(\d+)\+?\s*(?:years?\s+(?:of\s+)?experience|nam\s+kinh\s+nghiem)", normalized)
        return int(match.group(1)) * 12 if match else None

    @staticmethod
    def _seniority(text: str) -> str | None:
        normalized = _key(text)
        mapping = {"intern": ("intern", "thuc tap"), "junior": ("junior", "fresher"), "mid": ("mid", "middle"), "senior": ("senior",), "lead": ("lead", "team lead"), "manager": ("manager", "quan ly")}
        return next((name for name, aliases in mapping.items() if any(alias in normalized for alias in aliases)), None)

    @staticmethod
    def _employment_type(text: str) -> str | None:
        normalized = _key(text)
        mapping = {"full_time": ("full time", "toan thoi gian"), "part_time": ("part time", "ban thoi gian"), "contract": ("contract", "hop dong"), "internship": ("internship", "thuc tap")}
        return next((name for name, aliases in mapping.items() if any(alias in normalized for alias in aliases)), None)

    @staticmethod
    def _work_mode(text: str) -> str | None:
        normalized = _key(text)
        mapping = {
            "remote": ("remote", "tu xa"),
            "hybrid": ("hybrid",),
            # Do not infer a work arrangement from incidental benefits such as
            # food supplied "tại văn phòng".
            "on_site": ("on site", "lam viec tai van phong"),
        }
        return next((name for name, aliases in mapping.items() if any(alias in normalized for alias in aliases)), None)

    @staticmethod
    def _location(source: SourceDocument) -> str | None:
        lines = _lines(source.text)
        for index, (_, line) in enumerate(lines):
            key = _key(line)
            if key.startswith("location "):
                return line.split(":", 1)[1].strip() if ":" in line else None
            if _heading(line) == "location" and index + 1 < len(lines):
                value = line.split(":", 1)[1].strip() if ":" in line else lines[index + 1][1].lstrip("-• ").split(":", 1)[0].strip()
                if value:
                    return value
        match = re.search(r"(?im)^\s*(hà nội|ha noi|hanoi|đà nẵng|da nang|ho chi minh city)\s*:", source.text)
        return match.group(1).title() if match else None

    @staticmethod
    def _classifications(requirements):
        ids = {item.concept.concept_id for item in requirements if item.concept}
        if "skill-unity" in ids:
            code, label = "technology.game-development", "Game Development"
        elif {"skill-artificial-intelligence", "skill-machine-learning", "skill-natural-language-processing", "skill-generative-ai", "skill-large-language-models"}.intersection(ids):
            code, label = "technology.artificial-intelligence", "Artificial Intelligence"
        elif {"skill-java", "skill-spring-boot", "skill-fastapi"}.intersection(ids):
            code, label = "technology.software-engineering.backend", "Backend Engineering"
        elif {"skill-react", "skill-javascript", "skill-typescript"}.intersection(ids):
            code, label = "technology.software-engineering.frontend", "Frontend Engineering"
        elif {"skill-docker", "skill-kubernetes", "skill-aws"}.intersection(ids):
            code, label = "technology.cloud-devops", "Cloud & DevOps"
        else:
            return []
        refs = [ref for item in requirements for ref in item.evidence_refs]
        return [CareerClassification(code=code, label=label, dimension="specialization", taxonomyVersion="internal-career-2026.1", isPrimary=True, confidence=0.7, evidenceRefs=refs)]
