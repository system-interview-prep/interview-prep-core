import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha1
from typing import Protocol

from src.modules.user_cvs.domain.schemas import (
    CanonicalResume,
    EvidenceSpan,
    IdentityValue,
    LanguageClaim,
    ParsedResume,
    ParserWarning,
    ParsingMetadata,
    ResumeIdentity,
    SkillClaim,
    TaxonomyRef,
)
from src.modules.user_cvs.parsing.domain.classification import DeterministicCareerClassifier
from src.modules.user_cvs.parsing.domain.source import EvidenceMapper, SourceDocument
from src.modules.user_cvs.parsing.domain.structured import (
    CertificationExtractor,
    EducationExtractor,
    EmploymentExtractor,
    ProjectExtractor,
)

PARSER_VERSION = "deterministic-resume-v4"
TAXONOMY_VERSION = "internal-2026.1"

_SKILLS = {
    "skill-python": ("Python", ("python",)),
    "skill-java": ("Java", ("java",)),
    "skill-spring-boot": ("Spring Boot", ("spring boot", "springboot")),
    "skill-javascript": ("JavaScript", ("javascript", "js")),
    "skill-typescript": ("TypeScript", ("typescript",)),
    "skill-react": ("React", ("react", "reactjs", "react.js")),
    "skill-fastapi": ("FastAPI", ("fastapi",)),
    "skill-postgresql": ("PostgreSQL", ("postgresql", "postgres")),
    "skill-docker": ("Docker", ("docker",)),
    "skill-kubernetes": ("Kubernetes", ("kubernetes", "k8s")),
    "skill-aws": ("AWS", ("aws", "amazon web services")),
    "skill-azure": ("Microsoft Azure", ("azure", "microsoft azure")),
    "skill-gcp": ("Google Cloud Platform", ("gcp", "google cloud platform")),
    "skill-git": ("Git", ("git",)),
    "skill-sql": ("SQL", ("sql",)),
    "skill-mysql": ("MySQL", ("mysql",)),
    "skill-oracle": ("Oracle Database", ("oracle", "oracle database")),
    "skill-mongodb": ("MongoDB", ("mongodb",)),
    "skill-linux": ("Linux", ("linux",)),
    "skill-html": ("HTML", ("html",)),
    "skill-css": ("CSS", ("css",)),
    "skill-php": ("PHP", ("php",)),
    "skill-cpp": ("C++", ("c++",)),
    "skill-csharp": ("C#", ("c#",)),
    "skill-dotnet": (".NET", (".net", "dotnet")),
    "skill-angular": ("Angular", ("angular", "angularjs", "angular.js")),
    "skill-nodejs": ("Node.js", ("node.js", "nodejs")),
    "skill-django": ("Django", ("django",)),
    "skill-flask": ("Flask", ("flask",)),
    "skill-tensorflow": ("TensorFlow", ("tensorflow",)),
    "skill-numpy": ("NumPy", ("numpy",)),
    "skill-pandas": ("pandas", ("pandas",)),
    "skill-excel": ("Microsoft Excel", ("excel", "ms excel", "microsoft excel")),
    "skill-ms-office": ("Microsoft Office", ("ms office", "microsoft office")),
    "skill-autocad": ("AutoCAD", ("autocad",)),
    "skill-jira": ("Jira", ("jira",)),
    "skill-selenium": ("Selenium", ("selenium",)),
    "skill-android": ("Android", ("android",)),
    "skill-ios": ("iOS", ("ios",)),
    "skill-unity": ("Unity", ("unity",)),
    "skill-json": ("JSON", ("json",)),
    "skill-xml": ("XML", ("xml",)),
    "skill-http": ("HTTP", ("http",)),
    "skill-rest-api": ("REST API", ("rest", "rest api", "restful api")),
}

_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![\w.-])")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+84|0)(?:[ .-]?\d){9,10}(?!\d)")
_NAME_RE = re.compile(
    r"(?:full\s+name|name|họ\s*(?:và|va)?\s*tên|họ\s*tên)\s*[:\-]\s*"
    r"(?P<value>[^\n|]{2,100})",
    re.IGNORECASE,
)
_DATE_OF_BIRTH_RE = re.compile(
    r"(?:date\s+of\s+birth|dob|ngày\s+sinh)\s*[:\-]\s*"
    r"(?P<value>\d{1,2}[/-]\d{1,2}[/-](?:19|20)\d{2})",
    re.IGNORECASE,
)
_NAME_TITLE_WORDS = {
    "developer",
    "engineer",
    "designer",
    "manager",
    "intern",
    "student",
    "consultant",
    "software",
    "backend",
    "frontend",
    "fullstack",
    "data",
    "scientist",
}
_LANGUAGE_RE = re.compile(
    r"(?P<label>english|ti\u1ebfng\s+anh)\s*(?:[:\-|]|tr\u00ecnh\s+\u0111\u1ed9)?\s*"
    r"(?P<level>[ABC][12])\b",
    re.IGNORECASE,
)


def _evidence_id(kind: str, start: int, end: int) -> str:
    digest = sha1(f"{kind}:{start}:{end}".encode(), usedforsecurity=False).hexdigest()[:12]
    return f"ev-{kind}-{digest}"


def _pattern(alias: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w]){re.escape(alias)}(?![\w])", re.IGNORECASE)


@dataclass
class ResumeDraft:
    """Mutable internal accumulator; never crosses the parser boundary."""

    skills: list[SkillClaim] = field(default_factory=list)
    languages: list[LanguageClaim] = field(default_factory=list)
    employment: list = field(default_factory=list)
    education: list = field(default_factory=list)
    projects: list = field(default_factory=list)
    certifications: list = field(default_factory=list)
    evidence: dict[str, EvidenceSpan] = field(default_factory=dict)
    warnings: list[ParserWarning] = field(default_factory=list)


class ClaimExtractor(Protocol):
    """Extension point for one independently testable claim family."""

    def extract(self, source: SourceDocument, mapper: EvidenceMapper, draft: ResumeDraft) -> None: ...


class IdentityExtractor(Protocol):
    def extract(self, source: SourceDocument) -> ResumeIdentity: ...


class TaxonomySkillExtractor:
    def __init__(
        self,
        taxonomy: dict[str, tuple[str, tuple[str, ...]]] | None = None,
        taxonomy_version: str = TAXONOMY_VERSION,
    ) -> None:
        self._taxonomy = taxonomy or _SKILLS
        self._taxonomy_version = taxonomy_version

    def extract(self, source: SourceDocument, mapper: EvidenceMapper, draft: ResumeDraft) -> None:
        for concept_id, (label, aliases) in self._taxonomy.items():
            matches = [match for alias in aliases for match in _pattern(alias).finditer(source.text)]
            if not matches:
                continue
            matches.sort(key=lambda item: (item.start(), -(item.end() - item.start())))
            refs: list[str] = []
            for match in matches:
                evidence_id = _evidence_id("skill", match.start(), match.end())
                draft.evidence.setdefault(
                    evidence_id,
                    mapper.from_offsets(
                        evidence_id=evidence_id,
                        char_start=match.start(),
                        char_end=match.end(),
                    ),
                )
                refs.append(evidence_id)
            first = matches[0]
            draft.skills.append(
                SkillClaim(
                    claimId=f"claim-{concept_id}",
                    concept=TaxonomyRef(
                        conceptId=concept_id,
                        scheme="internal",
                        taxonomyVersion=self._taxonomy_version,
                        label=label,
                    ),
                    rawLabel=source.text[first.start() : first.end()],
                    evidenceRefs=list(dict.fromkeys(refs)),
                    assertionSource="explicit",
                    confidence=1.0,
                )
            )
        if not draft.skills:
            draft.warnings.append(
                ParserWarning(
                    code="NO_SKILLS_DETECTED",
                    path="skills",
                    message="Deterministic taxonomy did not find an explicit skill alias.",
                )
            )


class CefrLanguageExtractor:
    def extract(self, source: SourceDocument, mapper: EvidenceMapper, draft: ResumeDraft) -> None:
        for match in _LANGUAGE_RE.finditer(source.text):
            evidence_id = _evidence_id("language", match.start(), match.end())
            draft.evidence[evidence_id] = mapper.from_offsets(
                evidence_id=evidence_id,
                char_start=match.start(),
                char_end=match.end(),
                section="languages",
            )
            draft.languages.append(
                LanguageClaim(
                    code="en",
                    level=match.group("level").upper(),
                    framework="CEFR",
                    evidenceRefs=[evidence_id],
                )
            )


class RegexIdentityExtractor:
    def extract(self, source: SourceDocument) -> ResumeIdentity:
        name = _NAME_RE.search(source.text)
        date_of_birth = _DATE_OF_BIRTH_RE.search(source.text)
        fallback_name = self._top_of_document_name(source) if name is None else None
        normalized_date_of_birth = (
            self._normalize_date_of_birth(date_of_birth.group("value")) if date_of_birth else None
        )
        return ResumeIdentity(
            fullName=(
                IdentityValue(
                    value=name.group("value").strip(),
                    charStart=name.start("value"),
                    charEnd=name.end("value"),
                )
                if name
                else fallback_name
            ),
            dateOfBirth=(
                IdentityValue(
                    value=self._normalize_date_of_birth(date_of_birth.group("value")),
                    charStart=date_of_birth.start("value"),
                    charEnd=date_of_birth.end("value"),
                )
                if date_of_birth and normalized_date_of_birth
                else None
            ),
            emails=[
                IdentityValue(value=match.group(), charStart=match.start(), charEnd=match.end())
                for match in _EMAIL_RE.finditer(source.text)
            ],
            phones=[
                IdentityValue(value=match.group(), charStart=match.start(), charEnd=match.end())
                for match in _PHONE_RE.finditer(source.text)
            ],
        )

    @staticmethod
    def _normalize_date_of_birth(value: str) -> str | None:
        date_format = "%d/%m/%Y" if "/" in value else "%d-%m-%Y"
        try:
            return datetime.strptime(value, date_format).date().isoformat()
        except ValueError:
            return None

    @staticmethod
    def _top_of_document_name(source: SourceDocument) -> IdentityValue | None:
        for block in source.blocks[:5]:
            value = block.text.strip()
            words = value.split()
            if (
                block.page == 1
                and 2 <= len(words) <= 5
                and all(re.fullmatch(r"[A-Za-zÀ-ỹĐđ'.-]+", word) for word in words)
                and not _NAME_TITLE_WORDS.intersection(word.casefold() for word in words)
            ):
                return IdentityValue(value=value, charStart=block.char_start, charEnd=block.char_end)
        return None


class DeterministicResumeParser:
    """Coordinates replaceable extractors and publishes only schema-valid output."""

    def __init__(
        self,
        claim_extractors: Iterable[ClaimExtractor] | None = None,
        identity_extractor: IdentityExtractor | None = None,
        career_classifier: DeterministicCareerClassifier | None = None,
        taxonomy: dict[str, tuple[str, tuple[str, ...]]] | None = None,
        taxonomy_version: str = TAXONOMY_VERSION,
    ) -> None:
        self._claim_extractors = tuple(
            claim_extractors
            or (
                TaxonomySkillExtractor(taxonomy, taxonomy_version),
                CefrLanguageExtractor(),
                EmploymentExtractor(),
                EducationExtractor(),
                ProjectExtractor(),
                CertificationExtractor(),
            )
        )
        self._identity_extractor = identity_extractor or RegexIdentityExtractor()
        self._career_classifier = career_classifier or DeterministicCareerClassifier()

    def parse(
        self,
        source: SourceDocument,
        *,
        extraction_version: str,
        source_artifact_key: str | None = None,
    ) -> ParsedResume:
        mapper = EvidenceMapper(source)
        draft = ResumeDraft()
        for extractor in self._claim_extractors:
            extractor.extract(source, mapper, draft)
        document_languages = ["vi"] if re.search(r"[\u0102-\u01b0\u1ea0-\u1ef9]", source.text) else ["en"]
        resume = CanonicalResume(
            schemaVersion="2.1",
            resumeId=source.document_id,
            documentId=source.document_id,
            documentSha256=source.document_sha256,
            documentLanguages=document_languages,
            skills=draft.skills,
            employment=draft.employment,
            education=draft.education,
            projects=draft.projects,
            certifications=draft.certifications,
            languages=draft.languages,
            careerClassifications=self._career_classifier.classify(
                skills=draft.skills, employment=draft.employment
            ),
            evidence=list(draft.evidence.values()),
            parsing=ParsingMetadata(
                parserVersion=PARSER_VERSION,
                extractionVersion=extraction_version,
                parsedAt=datetime.now(UTC),
                status="review_required",
                sourceArtifactKey=source_artifact_key,
                warnings=draft.warnings,
            ),
        )
        return ParsedResume(resume=resume, identity=self._identity_extractor.extract(source))
