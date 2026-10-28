"""Staging persistence for CSV Question Bank imports."""
# ruff: noqa: E501

import csv
import io
from datetime import UTC, datetime
from hashlib import sha256
from types import SimpleNamespace
from uuid import UUID, uuid4

from fastapi import HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.question_bank.import_parser import (
    normalize_row_payload,
    parse_csv,
    parse_xlsx,
    validate_row_payload,
)
from src.modules.question_bank.models import (
    InterviewQuestion,
    InterviewQuestionVersion,
    QuestionImport,
    QuestionImportRow,
    QuestionVersionRubric,
    QuestionVersionTaxonomyConcept,
    Rubric,
)
from src.modules.question_bank.service import QuestionBankService

MAX_IMPORT_FILE_SIZE = 5 * 1024 * 1024


class QuestionImportService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def create_csv_import(self, file: UploadFile, actor_id: str) -> QuestionImport:
        if not file.filename or not file.filename.lower().endswith((".csv", ".xlsx")):
            raise HTTPException(422, "Only CSV and XLSX imports are supported.")
        content = await file.read(MAX_IMPORT_FILE_SIZE + 1)
        if not content or len(content) > MAX_IMPORT_FILE_SIZE:
            raise HTTPException(422, "CSV file must be between 1 byte and 5 MB.")
        try:
            parsed = parse_xlsx(content) if file.filename.lower().endswith(".xlsx") else parse_csv(content)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        import_run = QuestionImport(
            status="READY_FOR_REVIEW",
            file_name=file.filename,
            file_format="XLSX" if file.filename.lower().endswith(".xlsx") else "CSV",
            content_hash=sha256(content).hexdigest(),
            total_rows=len(parsed),
            created_by=actor_id,
            completed_at=datetime.now(UTC),
        )
        self.db.add(import_run)
        await self.db.flush()
        rows = []
        for parsed_row in parsed:
            row_status = "ERROR" if parsed_row.errors else "VALID"
            rows.append(
                QuestionImportRow(
                    import_id=import_run.id,
                    row_number=parsed_row.row_number,
                    raw_payload=parsed_row.payload,
                    normalized_payload=parsed_row.payload,
                    validation_errors=parsed_row.errors,
                    validation_warnings=[],
                    status=row_status,
                )
            )
        self.db.add_all(rows)
        import_run.valid_rows = sum(row.status == "VALID" for row in rows)
        import_run.error_rows = sum(row.status == "ERROR" for row in rows)
        await self.db.flush()
        return import_run

    async def summary(self, import_id: object) -> QuestionImport:
        result = await self.db.scalar(select(QuestionImport).where(QuestionImport.id == import_id))
        if result is None:
            raise HTTPException(404, "Import not found.")
        return result

    async def rows(self, import_id: object) -> list[QuestionImportRow]:
        await self.summary(import_id)
        result = await self.db.scalars(
            select(QuestionImportRow)
            .where(QuestionImportRow.import_id == import_id)
            .order_by(QuestionImportRow.row_number)
        )
        return list(result)

    async def patch_row(self, import_id: UUID, row_id: UUID, payload: dict) -> QuestionImportRow:
        row = await self.db.scalar(
            select(QuestionImportRow).where(
                QuestionImportRow.id == row_id, QuestionImportRow.import_id == import_id
            )
        )
        if row is None:
            raise HTTPException(404, "Import row not found.")
        import_run = await self.summary(import_id)
        if import_run.status != "READY_FOR_REVIEW":
            raise HTTPException(409, "Only imports awaiting review can be edited.")
        editable = {
            "canonical_text",
            "objective",
            "primary_competency_id",
            "soft_answer_seconds",
            "hard_answer_seconds",
        }
        if set(payload).difference(editable):
            raise HTTPException(422, "Payload contains non-editable import fields.")
        normalized = {**row.normalized_payload, **payload}
        for key in ("soft_answer_seconds", "hard_answer_seconds"):
            if isinstance(normalized.get(key), str) and normalized[key].strip().isdigit():
                normalized[key] = int(normalized[key])
        errors = self._validate_payload(normalized)
        row.normalized_payload, row.validation_errors = normalized, errors
        row.status = "ERROR" if errors else "VALID"
        await self.db.flush()
        all_rows = await self.rows(import_id)
        import_run.valid_rows = sum(item.status == "VALID" for item in all_rows)
        import_run.error_rows = sum(item.status == "ERROR" for item in all_rows)
        await self.db.flush()
        return row

    async def report_csv(self, import_id: UUID) -> bytes:
        rows = await self.rows(import_id)
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=("row_number", "status", "stable_key", "errors"))
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "row_number": row.row_number,
                    "status": row.status,
                    "stable_key": row.normalized_payload.get("stable_key", ""),
                    "errors": "; ".join(error["code"] for error in row.validation_errors),
                }
            )
        return stream.getvalue().encode("utf-8")

    async def commit(self, import_id: UUID, actor_id: str, idempotency_key: str) -> list[QuestionImportRow]:
        # Lock the import so two concurrent commits cannot both create drafts.
        import_run = await self.db.scalar(
            select(QuestionImport).where(QuestionImport.id == import_id).with_for_update()
        )
        if import_run is None:
            raise HTTPException(404, "Import not found.")
        if import_run.status == "COMMITTED":
            if import_run.commit_idempotency_key == idempotency_key:
                return await self.rows(import_id)
            raise HTTPException(409, "Import has already been committed.")
        rows = await self.rows(import_id)
        if import_run.status != "READY_FOR_REVIEW" or any(row.status != "VALID" for row in rows):
            raise HTTPException(422, "All import rows must be valid before commit.")
        await self._reject_duplicate_keys(rows)
        for row in rows:
            payload = row.normalized_payload
            errors = self._validate_payload(payload)
            if errors:
                raise HTTPException(422, "Import validation changed; refresh and review rows.")
            mappings = self._taxonomy_mappings(payload)
            await QuestionBankService(self.db)._validate_taxonomy_mappings(payload["taxonomy_version"], mappings)
            rubric_version_id = await self._resolve_rubric(payload)
            question = InterviewQuestion(id=uuid4(), stable_key=payload["stable_key"], created_by=actor_id)
            version = InterviewQuestionVersion(
                id=uuid4(),
                question_id=question.id,
                version=payload["version"],
                taxonomy_version=payload["taxonomy_version"],
                question_type=payload["question_type"],
                difficulty_band=payload["difficulty_band"],
                canonical_locale=payload["canonical_locale"],
                canonical_text=payload["canonical_text"],
                objective=payload["objective"],
                thinking_seconds=int(payload["thinking_seconds"]),
                soft_answer_seconds=int(payload["soft_answer_seconds"]),
                hard_answer_seconds=int(payload["hard_answer_seconds"]),
                context_policy={},
                personalization_policy={},
                canonical_snapshot={},
                created_by=actor_id,
                change_summary=payload.get("change_summary", ""),
            )
            self.db.add_all([question, version])
            self.db.add_all(
                QuestionVersionTaxonomyConcept(
                    question_version_id=version.id,
                    taxonomy_version=payload["taxonomy_version"],
                    concept_id=item.concept_id,
                    purpose=item.purpose,
                    relevance=item.relevance,
                )
                for item in mappings
            )
            if rubric_version_id is not None:
                self.db.add(QuestionVersionRubric(question_version_id=version.id, rubric_version_id=rubric_version_id))
            row.status, row.draft_question_id, row.draft_question_version_id = (
                "COMMITTED",
                question.id,
                version.id,
            )
        import_run.status, import_run.committed_at, import_run.commit_idempotency_key = (
            "COMMITTED",
            datetime.now(UTC),
            idempotency_key,
        )
        await self.db.flush()
        return rows

    async def _reject_duplicate_keys(self, rows: list[QuestionImportRow]) -> None:
        """A duplicate stable key used to surface as an IntegrityError (500) mid-commit."""
        keys = [str(row.normalized_payload.get("stable_key") or "").strip().lower() for row in rows]
        in_file = sorted({key for key in keys if keys.count(key) > 1})
        existing = await self.db.scalars(
            select(InterviewQuestion.stable_key).where(InterviewQuestion.stable_key.in_(keys))
        )
        in_bank = sorted(set(existing))
        if in_file or in_bank:
            raise HTTPException(
                409,
                {
                    "message": "Import contains stable keys that are duplicated or already exist.",
                    "duplicatedInFile": in_file,
                    "alreadyInBank": in_bank,
                },
            )

    @staticmethod
    def _taxonomy_mappings(payload: dict) -> list[SimpleNamespace]:
        """Every mapping the selector can match on, not only the primary competency.

        P1 skill targets match TARGET_SKILL and the career-classification
        fallback matches TARGET_ROLE only; dropping those columns made imported
        questions invisible to P2.
        """
        primary = str(payload.get("primary_competency_id", "")).strip()
        mappings = [SimpleNamespace(concept_id=primary, purpose="PRIMARY_COMPETENCY", relevance=1)]
        for column, purpose in (
            ("skill_ids", "TARGET_SKILL"),
            ("target_role_ids", "TARGET_ROLE"),
            ("supporting_competency_ids", "SUPPORTING_COMPETENCY"),
        ):
            for raw_id in payload.get(column) or []:
                concept_id = str(raw_id).strip()
                if concept_id and all((m.concept_id, m.purpose) != (concept_id, purpose) for m in mappings):
                    mappings.append(SimpleNamespace(concept_id=concept_id, purpose=purpose, relevance=1))
        return mappings

    async def _resolve_rubric(self, payload: dict) -> UUID | None:
        rubric_key = str(payload.get("rubric_key") or "").strip()
        if not rubric_key:
            return None
        rubric = await self.db.scalar(select(Rubric).where(Rubric.stable_key == rubric_key))
        if rubric is None or rubric.current_version_id is None:
            raise HTTPException(422, f"Rubric {rubric_key!r} does not exist.")
        return rubric.current_version_id

    @staticmethod
    def _validate_payload(payload: dict) -> list[dict]:
        normalize_row_payload(payload)
        return validate_row_payload(payload)
