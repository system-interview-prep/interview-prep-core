import re
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha1

from pydantic import BaseModel, ConfigDict, Field

from src.modules.matching.schemas import (
    CanonicalResume,
    LanguageClaim,
    ParserWarning,
    ParsingMetadata,
    SkillClaim,
    TaxonomyRef,
)
from src.modules.user_cvs.parsing.source import EvidenceMapper, SourceDocument

PARSER_VERSION = "deterministic-resume-v1"
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
    "skill-git": ("Git", ("git",)),
}

_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![\w.-])")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+84|0)(?:[ .-]?\d){9,10}(?!\d)")
_LANGUAGE_RE = re.compile(
    r"(?P<label>english|tiếng\s+anh)\s*(?:[:\-–|]|trình\s+độ)?\s*(?P<level>[ABC][12])\b",
    re.IGNORECASE,
)


class IdentityValue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)


class ResumeIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    emails: list[IdentityValue] = Field(default_factory=list)
    phones: list[IdentityValue] = Field(default_factory=list)


@dataclass(frozen=True)
class DeterministicParseResult:
    resume: CanonicalResume
    identity: ResumeIdentity


def _evidence_id(kind: str, start: int, end: int) -> str:
    digest = sha1(f"{kind}:{start}:{end}".encode(), usedforsecurity=False).hexdigest()[:12]
    return f"ev-{kind}-{digest}"


def _pattern(alias: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w]){re.escape(alias)}(?![\w])", re.IGNORECASE)


class DeterministicResumeParser:
    def parse(
        self,
        source: SourceDocument,
        *,
        extraction_version: str,
        source_artifact_key: str | None = None,
    ) -> DeterministicParseResult:
        mapper = EvidenceMapper(source)
        evidence_by_id = {}
        skills: list[SkillClaim] = []

        for concept_id, (label, aliases) in _SKILLS.items():
            matches = [match for alias in aliases for match in _pattern(alias).finditer(source.text)]
            if not matches:
                continue
            matches.sort(key=lambda item: (item.start(), -(item.end() - item.start())))
            refs: list[str] = []
            for match in matches:
                evidence_id = _evidence_id("skill", match.start(), match.end())
                if evidence_id not in evidence_by_id:
                    evidence_by_id[evidence_id] = mapper.from_offsets(
                        evidence_id=evidence_id,
                        char_start=match.start(),
                        char_end=match.end(),
                    )
                refs.append(evidence_id)
            first = matches[0]
            skills.append(
                SkillClaim(
                    claimId=f"claim-{concept_id}",
                    concept=TaxonomyRef(
                        conceptId=concept_id,
                        scheme="internal",
                        taxonomyVersion=TAXONOMY_VERSION,
                        label=label,
                    ),
                    rawLabel=source.text[first.start() : first.end()],
                    evidenceRefs=list(dict.fromkeys(refs)),
                    assertionSource="explicit",
                    confidence=1.0,
                )
            )

        languages: list[LanguageClaim] = []
        for match in _LANGUAGE_RE.finditer(source.text):
            evidence_id = _evidence_id("language", match.start(), match.end())
            evidence_by_id[evidence_id] = mapper.from_offsets(
                evidence_id=evidence_id,
                char_start=match.start(),
                char_end=match.end(),
                section="languages",
            )
            languages.append(
                LanguageClaim(
                    code="en",
                    level=match.group("level").upper(),
                    framework="CEFR",
                    evidenceRefs=[evidence_id],
                )
            )

        identity = ResumeIdentity(
            emails=[
                IdentityValue(value=match.group(), char_start=match.start(), char_end=match.end())
                for match in _EMAIL_RE.finditer(source.text)
            ],
            phones=[
                IdentityValue(value=match.group(), char_start=match.start(), char_end=match.end())
                for match in _PHONE_RE.finditer(source.text)
            ],
        )
        warnings = []
        if not skills:
            warnings.append(
                ParserWarning(
                    code="NO_SKILLS_DETECTED",
                    path="skills",
                    message="Deterministic taxonomy did not find an explicit skill alias.",
                )
            )
        warnings.append(
            ParserWarning(
                code="DETERMINISTIC_SUBSET_ONLY",
                severity="info",
                message="Employment, education, projects and certifications require a later extractor.",
            )
        )
        languages_detected = ["vi"] if re.search(r"[ăâđêôơưĂÂĐÊÔƠƯ]", source.text) else ["en"]
        resume = CanonicalResume(
            schemaVersion="2.1",
            resumeId=source.document_id,
            documentId=source.document_id,
            documentSha256=source.document_sha256,
            documentLanguages=languages_detected,
            skills=skills,
            languages=languages,
            evidence=list(evidence_by_id.values()),
            parsing=ParsingMetadata(
                parserVersion=PARSER_VERSION,
                extractionVersion=extraction_version,
                parsedAt=datetime.now(timezone.utc),
                status="review_required",
                sourceArtifactKey=source_artifact_key,
                warnings=warnings,
            ),
        )
        return DeterministicParseResult(resume=resume, identity=identity)
