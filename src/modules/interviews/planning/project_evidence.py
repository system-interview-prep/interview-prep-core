"""Structured CV-based project evidence extraction, scoring, and question formulation.

Governing contract:
- Represents CV projects with structured evidence: role, domain, tech, decisions, outcomes, JD relevance.
- Does not invent missing data: marks as missing/unknown and prompts candidate to verify.
- Ranks candidate projects deterministically: JD relevance > evidence richness > CV order tie-break.
- Formulates 4-axis project questions:
  1. Role & direct contributions (verified if present, asked to verify if missing).
  2. Problem & specific constraints.
  3. Technical decisions & trade-offs.
  4. Outcomes, measurement & baseline.
"""

from dataclasses import dataclass, field
import re
from typing import Any


_COMMON_TECH_TOKENS = {
    "python", "java", "typescript", "javascript", "golang", "go", "c++", "c#", "rust",
    "fastapi", "flask", "django", "spring boot", "spring", "express", "nestjs", "node.js", "nodejs",
    "react", "next.js", "nextjs", "vue", "angular", "tailwind",
    "pytorch", "tensorflow", "keras", "huggingface", "transformers", "langchain", "langgraph", "llamaindex",
    "gemini", "openai", "gpt", "claude", "llm", "genai", "generative ai", "nlp", "rag", "bm25", "tf-idf",
    "semantic search", "vector database", "milvus", "qdrant", "pinecone", "chroma", "weaviate",
    "postgresql", "postgres", "mysql", "mongodb", "redis", "elasticsearch",
    "docker", "kubernetes", "aws", "gcp", "azure", "cloudflare", "r2", "s3", "rabbitmq", "kafka", "celery",
}

_ROLE_PREFIX_RE = re.compile(
    r"(?:vai trò|role|vị trí|position)\s*[:\-–—]\s*([^\n.,;]+)",
    re.IGNORECASE,
)

_COMMON_ROLES_RE = re.compile(
    r"\b("
    r"lead\s+developer|core\s+builder|tech\s+lead|team\s+lead|"
    r"backend\s+developer|frontend\s+developer|fullstack\s+developer|"
    r"ai\s+engineer|machine\s+learning\s+engineer|data\s+scientist|ml\s+engineer|"
    r"software\s+engineer|developer|contributor|trưởng\s+nhóm|thành\s+viên"
    r")\b",
    re.IGNORECASE,
)

_METRIC_RE = re.compile(
    r"\b(?:\d+[\d.,]*\s*(?:%|users?|người dùng|khách hàng|req/s|rps|ms|s|stars?|triệu|tỷ|k|usd|\$))\b",
    re.IGNORECASE,
)

_OUTCOME_PREFIX_RE = re.compile(
    r"(?:kết quả|outcomes?|results?|metrics?|impact)\s*[:\-–—]\s*([^\n]+)",
    re.IGNORECASE,
)

_DECISION_PREFIX_RE = re.compile(
    r"(?:quyết định|architectural\s+decision|trade-off|lựa chọn kiến trúc|giải pháp|tối ưu|tối ưu hóa)\s*[:\-–—]?\s*([^\n.]+)",
    re.IGNORECASE,
)


@dataclass
class StructuredProjectEvidence:
    project_id: str
    name: str
    cv_order: int = 0
    evidence_refs: list[str] = field(default_factory=list)
    role: str | None = None
    role_status: str = "missing"  # "present" | "missing"
    problem_domain: str | None = None
    technologies: list[str] = field(default_factory=list)
    technologies_status: str = "missing"  # "present" | "missing"
    decisions: list[str] = field(default_factory=list)
    outcomes: list[str] = field(default_factory=list)
    outcomes_status: str = "missing"  # "present" | "missing"
    relevant_requirements: list[str] = field(default_factory=list)
    jd_relevance_score: float = 0.0
    evidence_richness_score: float = 0.0
    confidence: float = 1.0
    selection_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "projectId": self.project_id,
            "name": self.name,
            "cvOrder": self.cv_order,
            "evidenceRefs": self.evidence_refs,
            "role": self.role,
            "roleStatus": self.role_status,
            "problemDomain": self.problem_domain,
            "technologies": self.technologies,
            "technologiesStatus": self.technologies_status,
            "decisions": self.decisions,
            "outcomes": self.outcomes,
            "outcomesStatus": self.outcomes_status,
            "relevantRequirements": self.relevant_requirements,
            "jdRelevanceScore": round(self.jd_relevance_score, 4),
            "evidenceRichnessScore": round(self.evidence_richness_score, 4),
            "confidence": self.confidence,
            "selectionReason": self.selection_reason,
        }


def extract_project_evidences(
    projects_data: list[Any],
    job_requirements: list[Any] | None = None,
    cv_skills: list[Any] | None = None,
    requirement_match_statuses: dict[str, str] | None = None,
) -> list[StructuredProjectEvidence]:
    """Parse raw or CanonicalResume project entries into StructuredProjectEvidence."""
    if not projects_data or not isinstance(projects_data, list):
        return []

    req_match_statuses = requirement_match_statuses or {}
    results: list[StructuredProjectEvidence] = []

    for index, raw_proj in enumerate(projects_data):
        if not isinstance(raw_proj, dict):
            # Might be a pydantic model (e.g. ProjectEntry)
            if hasattr(raw_proj, "model_dump"):
                raw_proj = raw_proj.model_dump()
            elif hasattr(raw_proj, "__dict__"):
                raw_proj = raw_proj.__dict__
            else:
                continue

        name = str(raw_proj.get("name") or "").strip()
        if not name or name.lower() in {"project", "dự án"}:
            continue

        desc = str(raw_proj.get("description") or "").strip()
        full_text = f"{name}\n{desc}"

        # 1. Project ID
        pid = str(raw_proj.get("project_id") or raw_proj.get("projectId") or f"project-{index + 1}")

        # 2. Evidence Refs
        refs = raw_proj.get("evidence_refs") or raw_proj.get("evidenceRefs") or []
        if isinstance(refs, list):
            evidence_refs = [str(r) for r in refs]
        else:
            evidence_refs = []

        # 3. Role extraction (Grounding: only if explicitly in text)
        role = None
        role_match = _ROLE_PREFIX_RE.search(full_text)
        if role_match:
            role = role_match.group(1).strip()
        else:
            common_match = _COMMON_ROLES_RE.search(full_text)
            if common_match:
                role = common_match.group(1).strip()

        role_status = "present" if role else "missing"

        # 4. Problem Domain extraction
        problem_domain = None
        # Often in project name: "Project Name - Domain/Subtitle"
        if " - " in name or " – " in name:
            parts = re.split(r"\s+[-–—]\s+", name)
            if len(parts) > 1:
                problem_domain = parts[1].strip()
        if not problem_domain and desc:
            first_line = desc.splitlines()[0]
            if len(first_line) < 120 and ("platform" in first_line.lower() or "system" in first_line.lower() or "hệ thống" in first_line.lower() or "ứng dụng" in first_line.lower()):
                problem_domain = first_line.strip()

        found_techs: set[str] = set()
        # Include explicit technologies field if provided
        raw_techs = raw_proj.get("technologies") or raw_proj.get("tech_stack") or []
        if isinstance(raw_techs, list):
            for t in raw_techs:
                if t and str(t).strip():
                    found_techs.add(str(t).strip())

        lower_text = full_text.lower()
        for token in _COMMON_TECH_TOKENS:
            # Word boundary search for accurate token matching
            pattern = r"(?<![a-zA-Z0-9])" + re.escape(token) + r"(?![a-zA-Z0-9])"
            if re.search(pattern, lower_text):
                found_techs.add(token.title() if len(token) > 3 else token.upper())

        # Include linked skills if present
        if cv_skills and isinstance(cv_skills, list):
            skill_claim_ids = set(raw_proj.get("skill_claim_ids") or raw_proj.get("skillClaimIds") or [])
            for sk in cv_skills:
                if isinstance(sk, dict):
                    cid = sk.get("claim_id") or sk.get("claimId")
                    raw_lbl = sk.get("raw_label") or (sk.get("concept") or {}).get("label")
                    if cid in skill_claim_ids and raw_lbl:
                        found_techs.add(str(raw_lbl).strip())

        technologies = sorted(list(found_techs))
        technologies_status = "present" if technologies else "missing"

        # 6. Decisions extraction
        decisions: list[str] = []
        for d_match in _DECISION_PREFIX_RE.finditer(full_text):
            decisions.append(d_match.group(1).strip())

        # 7. Outcomes / Metrics extraction
        outcomes: list[str] = []
        outcome_prefix_match = _OUTCOME_PREFIX_RE.search(full_text)
        if outcome_prefix_match:
            outcomes.append(outcome_prefix_match.group(1).strip())
        metric_matches = _METRIC_RE.findall(full_text)
        for m in metric_matches:
            if m not in outcomes and len(outcomes) < 3:
                outcomes.append(m)

        outcomes_status = "present" if outcomes else "missing"

        # 8. Evidence Richness Score
        richness = 0.0
        if role:
            richness += 1.0
        if technologies:
            richness += min(1.0, 0.4 + 0.15 * len(technologies))
        if outcomes:
            richness += 1.5
        if decisions:
            richness += 0.8
        if len(desc) > 50:
            richness += min(1.0, len(desc) / 250.0)

        # 9. JD Relevance Matching
        relevance_score = 0.0
        relevant_reqs: list[str] = []
        if job_requirements:
            for req in job_requirements:
                req_id = getattr(req, "requirement_id", None) or (req.get("requirement_id") or req.get("requirementId") if isinstance(req, dict) else None)
                priority = (getattr(req, "priority", "nice_to_have") if not isinstance(req, dict) else req.get("priority", "nice_to_have")) or "nice_to_have"
                raw_label = (getattr(req, "raw_label", None) or getattr(req, "label", "")) if not isinstance(req, dict) else (req.get("raw_label") or req.get("label") or "")
                
                # Concept matching
                concepts = []
                if hasattr(req, "skill") and hasattr(req.skill, "label"):
                    concepts.append(req.skill.label.lower())
                elif isinstance(req, dict) and req.get("conceptIds"):
                    concepts.extend([str(c).lower() for c in req.get("conceptIds", [])])
                if raw_label:
                    concepts.append(raw_label.lower())

                # Check if concept overlaps with project tech or domain or text
                matched = False
                for c in concepts:
                    if c in lower_text or any(c in t.lower() or t.lower() in c for t in technologies):
                        matched = True
                        break

                if matched:
                    if req_id and req_id not in relevant_reqs:
                        relevant_reqs.append(req_id)
                    is_must_have = str(priority).strip().lower() in ("must_have", "must-have", "musthave")
                    weight = 3.0 if is_must_have else 1.5
                    # Boost if this requirement was unknown/not_met (gap needing project evidence!)
                    m_status = req_match_statuses.get(req_id, "unknown")
                    boost = 1.4 if m_status in ("unknown", "not_met") else 1.0
                    relevance_score += weight * boost

        reason_parts = []
        if relevant_reqs:
            reason_parts.append(f"Matched {len(relevant_reqs)} JD requirements ({', '.join(relevant_reqs[:2])})")
        if role:
            reason_parts.append(f"Role declared: {role}")
        if outcomes:
            reason_parts.append(f"Metrics: {', '.join(outcomes[:2])}")
        if not reason_parts:
            reason_parts.append(f"Found in CV at index {index}")

        selection_reason = "; ".join(reason_parts)

        evidence = StructuredProjectEvidence(
            project_id=pid,
            name=name,
            cv_order=index,
            evidence_refs=evidence_refs,
            role=role,
            role_status=role_status,
            problem_domain=problem_domain,
            technologies=technologies,
            technologies_status=technologies_status,
            decisions=decisions,
            outcomes=outcomes,
            outcomes_status=outcomes_status,
            relevant_requirements=relevant_reqs,
            jd_relevance_score=relevance_score,
            evidence_richness_score=richness,
            selection_reason=selection_reason,
        )
        results.append(evidence)

    return results


def select_best_project(
    candidates: list[StructuredProjectEvidence],
) -> StructuredProjectEvidence | None:
    """Deterministically select the most JD-relevant and rich project.

    Tie-breaking:
    1. Highest JD relevance score (descending)
    2. Highest evidence richness score (descending)
    3. CV order (ascending) - stable and independent of DB random ordering.
    """
    if not candidates:
        return None

    ranked = sorted(
        candidates,
        key=lambda p: (
            -p.jd_relevance_score,
            -p.evidence_richness_score,
            p.cv_order,
        ),
    )
    return ranked[0]


def build_project_validation_question(
    project: StructuredProjectEvidence,
    job_title: str = "ứng viên",
    locale: str = "vi",
) -> str:
    """One short, grounded question about the candidate's CV project.

    A real interviewer asks one thing at a time: what the candidate did and the
    hardest decision. The CV's own facts (project, role, technologies, stated
    outcome) are quoted, never invented; a missing role is asked, not asserted.
    Measurement and baselines are left to the follow-up probe instead of being
    packed into the opening question.
    """
    is_vi = (locale or "vi").lower().startswith("vi")
    unique_techs: list[str] = []
    if project.technologies_status == "present":
        for tech in project.technologies:
            if tech.casefold() not in {item.casefold() for item in unique_techs}:
                unique_techs.append(tech)
    techs = ", ".join(unique_techs[:3])
    has_role = project.role_status == "present" and bool(project.role)
    outcome = project.outcomes[0] if project.outcomes_status == "present" and project.outcomes else ""

    if is_vi:
        if has_role:
            opening = f"Ở dự án '{project.name}', với vai trò {project.role}, bạn trực tiếp làm phần nào"
        else:
            opening = (
                f"CV chưa ghi rõ vai trò của bạn trong dự án '{project.name}'. "
                "Bạn xác minh chính xác vai trò và phần việc bạn trực tiếp làm"
            )
        decision = f"quyết định kỹ thuật khó nhất với {techs}" if techs else "quyết định kỹ thuật khó nhất"
        question = f"{opening}, và {decision} là gì?"
        if outcome:
            question += f" Kết quả “{outcome}” được đo thế nào?"
        return question

    if has_role:
        opening = f"In the '{project.name}' project, as {project.role}, which parts did you build yourself"
    else:
        opening = (
            f"Your CV does not state your role in the '{project.name}' project. "
            "Please verify your direct contribution"
        )
    decision = f"the hardest technical decision with {techs}" if techs else "the hardest technical decision"
    question = f"{opening}, and what was {decision}?"
    if outcome:
        question += f" How was “{outcome}” measured?"
    return question
