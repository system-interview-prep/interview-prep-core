"""Đọc các nhóm kỹ năng phỏng vấn từ những bảng phân loại đang hoạt động.

Phân loại không lưu các vòng phỏng vấn riêng biệt. Vì vậy, các khái niệm nghề
nghiệp hiện có và các quan hệ ``REQUIRES_SKILL`` được dùng để tạo nhóm:
mỗi khái niệm nghề nghiệp yêu cầu kỹ năng là một nhóm, còn các kỹ năng không có
quan hệ như vậy sẽ giữ nguyên thành nhóm riêng. Mô-đun này chỉ đọc dữ liệu phân loại.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class TaxonomySkillGroup:
    concept_id: str
    label: str


@dataclass(frozen=True)
class TaxonomySkillGroups:
    """
    Nhóm kỹ năng được xác định từ các khái niệm nghề nghiệp và các quan hệ ``REQUIRES_SKILL`` 
    trong các phiên bản phân loại đang hoạt động. Mỗi nhóm được xác định bởi một khái niệm nghề nghiệp, và các kỹ năng được liên kết với nhóm đó thông qua quan hệ ``REQUIRES_SKILL``. 
    Các kỹ năng không có quan hệ như vậy sẽ giữ nguyên thành nhóm riêng.
    - versions: là các phiên bản phân loại được sử dụng để xác định nhóm kỹ năng
    - groups: là danh sách các nhóm kỹ năng được xác định từ các khái niệm nghề nghiệp
    - concept_labels: là ánh xạ từ concept_id sang nhãn hiển thị của khái niệm nghề nghiệp
    - skill_groups: là ánh xạ từ concept_id của kỹ năng sang danh sách các concept_id của nhóm nghề nghiệp mà kỹ năng đó yêu cầu
    
    +group_ids: là tập hợp các concept_id của các nhóm kỹ năng
    +labels: là ánh xạ từ concept_id sang nhãn hiển thị của khái niệm nghề nghiệp
    +group_for(concept_id): trả về concept_id của nhóm nghề nghiệp liên kết với concept_id của kỹ năng, hoặc concept_id của kỹ năng nếu không có nhóm nghề nghiệp liên kết
    +spread_key(concept_id): trả về chuỗi định danh dạng "group:{concept_id}" cho concept_id của kỹ năng hoặc nhóm nghề nghiệp
    +group_label(group_id, fallback): trả về nhãn hiển thị của nhóm nghề nghiệp dựa trên concept_id của nhóm, hoặc fallback nếu không tìm thấy nhãn
    +definitions(): trả về danh sách các định nghĩa công khai của các nhóm kỹ năng, bao gồm concept_id, nh
    """

    versions: tuple[str, ...] = ()
    groups: tuple[TaxonomySkillGroup, ...] = ()
    concept_labels: Mapping[str, str] | None = None
    skill_groups: Mapping[str, tuple[str, ...]] | None = None

    @property
    def group_ids(self) -> frozenset[str]:
        return frozenset(group.concept_id for group in self.groups)

    @property
    def labels(self) -> dict[str, str]:
        return dict(self.concept_labels or {})

    def group_for(self, concept_id: str | None) -> str:
        """Return a linked career group, or keep an ungrouped concept separate."""
        key = str(concept_id or "")
        if not key:
            return ""
        if key in self.group_ids:
            return key
        linked = (self.skill_groups or {}).get(key, ())
        return linked[0] if linked else key

    def spread_key(self, concept_id: str | None) -> str:
        return f"group:{self.group_for(concept_id)}"

    def group_label(self, group_id: str, fallback: str | None = None) -> str:
        return self.labels.get(group_id) or fallback or group_id

    def definitions(self) -> list[dict[str, Any]]:
        """Public choices derived from taxonomy; every career group can be focused."""
        return [
            {
                "id": group.concept_id,
                "label": group.label,
                "order": index,
                "focusable": True,
                "alwaysAsked": False,
            }
            for index, group in enumerate(self.groups)
        ]


EMPTY_TAXONOMY_SKILL_GROUPS = TaxonomySkillGroups()


async def load_taxonomy_skill_groups(
    db: AsyncSession, *, versions: Sequence[str] | None = None
) -> TaxonomySkillGroups:
    """Load group candidates and skill links from existing taxonomy tables.

    No taxonomy records are created or changed. Requested versions are included
    alongside active versions so an interview plan can resolve its own version.
    """
    requested = tuple(dict.fromkeys(str(value) for value in (versions or ()) if value))
    active = tuple(
        str(row[0])
        for row in (
            await db.execute(
                text("SELECT version FROM taxonomy_versions WHERE is_active ORDER BY priority DESC, published_at DESC")
            )
        ).all()
    )
    selected_versions = tuple(dict.fromkeys((*requested, *active)))
    if not selected_versions:
        return EMPTY_TAXONOMY_SKILL_GROUPS

    label_rows = (
        await db.execute(
            text(
                "SELECT taxonomy_version, concept_id, label FROM taxonomy_concepts "
                "WHERE taxonomy_version = ANY(:versions) AND is_active "
                "ORDER BY taxonomy_version, concept_id"
            ),
            {"versions": list(selected_versions)},
        )
    ).all()
    concept_labels: dict[str, str] = {}
    for row in label_rows:
        concept_labels.setdefault(str(row[1]), str(row[2] or row[1]))

    rows = (
        await db.execute(
            text(
                """
                SELECT r.taxonomy_version, r.source_concept_id, r.target_concept_id,
                       source.label AS source_label
                FROM taxonomy_relations r
                JOIN taxonomy_concepts source
                  ON source.taxonomy_version = r.taxonomy_version
                 AND source.concept_id = r.source_concept_id AND source.is_active
                JOIN taxonomy_concepts target
                  ON target.taxonomy_version = r.taxonomy_version
                 AND target.concept_id = r.target_concept_id AND target.is_active
                WHERE r.taxonomy_version = ANY(:versions)
                  AND r.relation_type = 'REQUIRES_SKILL'
                ORDER BY r.taxonomy_version, r.source_concept_id, r.target_concept_id
                """
            ),
            {"versions": list(selected_versions)},
        )
    ).mappings().all()

    group_labels: dict[tuple[str, str], str] = {}
    skill_groups: dict[tuple[str, str], set[str]] = {}
    for row in rows:
        version = str(row["taxonomy_version"])
        source_id = str(row["source_concept_id"])
        target_id = str(row["target_concept_id"])
        source_label = str(row["source_label"] or source_id)
        # REQUIRES_SKILL source concepts are career groups in the current
        # taxonomy. Keep the grouping data-driven rather than assuming kinds.
        group_labels[(version, source_id)] = source_label
        skill_groups.setdefault((version, target_id), set()).add(source_id)

    unique_group_labels: dict[str, str] = {}
    for (_version, concept_id), label in sorted(group_labels.items()):
        unique_group_labels.setdefault(concept_id, label)
    groups = tuple(
        TaxonomySkillGroup(concept_id=concept_id, label=label)
        for concept_id, label in sorted(unique_group_labels.items())
    )
    # Multiple taxonomy versions can contain the same stable concept ID. Merge
    # its links without duplicating the public group list.
    merged_links: dict[str, set[str]] = {}
    for (_version, skill_id), linked_groups in skill_groups.items():
        merged_links.setdefault(skill_id, set()).update(linked_groups)
    return TaxonomySkillGroups(
        versions=selected_versions,
        groups=groups,
        concept_labels=concept_labels,
        skill_groups={key: tuple(sorted(value)) for key, value in merged_links.items()},
    )
