import pytest
from pydantic import ValidationError

from src.modules.taxonomy.schemas import TaxonomyConceptUpsert, TaxonomyRelationUpsert


def test_taxonomy_concept_accepts_question_bank_kinds() -> None:
    assert TaxonomyConceptUpsert(label="Production debugging", kind="competency").kind == "competency"
    assert TaxonomyConceptUpsert(label="Backend engineer", kind="job_role").kind == "job_role"


def test_taxonomy_concept_rejects_unknown_kind() -> None:
    with pytest.raises(ValidationError):
        TaxonomyConceptUpsert(label="Free text", kind="anything")


def test_taxonomy_relation_rejects_unknown_type() -> None:
    with pytest.raises(ValidationError):
        TaxonomyRelationUpsert(
            sourceConceptId="backend", targetConceptId="postgresql", relationType="MAGIC"
        )
