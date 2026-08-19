from __future__ import annotations

from datetime import date
from uuid import uuid4

from app.domain import AnalysisToolStatus
from app.models import AnalysisToolTemplate
from app.seed import default_user
from app.services.analysis_seeds import seed_analysis_templates
from app.services.manifest import native_manifest_fingerprint
from app.services.semantic_registry import semantic_schema_registry
from app.services.template_retrieval import ANALYSIS_TEMPLATE_VERSION, retrieve_templates


TODAY = date(2026, 8, 17)


def _template_row(
    name: str,
    description: str,
    signature: str,
    *,
    registry_hash: str | None = None,
    success_count: int = 0,
) -> AnalysisToolTemplate:
    registry = semantic_schema_registry()
    return AnalysisToolTemplate(
        capability_name=name,
        capability_description=description,
        capability_signature=signature,
        template_version=ANALYSIS_TEMPLATE_VERSION,
        status=AnalysisToolStatus.ACTIVE,
        semantic_registry_version=registry.version,
        source_manifest_hash=registry_hash or native_manifest_fingerprint(),
        parameter_schema=[],
        plan_template={"objective": "descriptive", "analysis_type": "monthly_comparison"},
        template_hash=uuid4().hex + uuid4().hex[:32],
        validation_report={"passed": True},
        success_count=success_count,
    )


def test_bm25_ranks_the_lexically_matching_seed_first(db):
    # Agent is disabled in tests, so ranking is BM25-only and fully deterministic.
    user = default_user(db)
    seed_analysis_templates(db, today=TODAY)

    results = retrieve_templates(db, user.id, "show my recurring subscriptions and repeated charges")

    assert results
    assert results[0].template.capability_name == "Recurring expense patterns"
    assert results[0].bm25_rank == 1
    assert results[0].cosine_rank is None
    # A single contributing ranking makes the top fused score exactly 1/(60+1).
    assert abs(results[0].fused_score - 1 / 61) < 1e-12


def test_templates_from_another_registry_generation_never_surface(db):
    user = default_user(db)
    stale = _template_row(
        "Recurring expense patterns stale",
        "recurring subscriptions repeated charges",
        "analysis recurring_expenses",
        registry_hash="0" * 64,
    )
    db.add(stale)
    db.flush()

    results = retrieve_templates(db, user.id, "recurring subscriptions repeated charges")

    assert all(item.template.id != stale.id for item in results)


def test_tied_lexical_scores_break_deterministically_by_success_count(db):
    user = default_user(db)
    weaker = _template_row("Cash runway view", "cash runway forecast burn", "analysis cash runway", success_count=1)
    stronger = _template_row("Cash runway view", "cash runway forecast burn", "analysis cash runway", success_count=9)
    db.add_all([weaker, stronger])
    db.flush()

    first = retrieve_templates(db, user.id, "cash runway burn forecast")
    second = retrieve_templates(db, user.id, "cash runway burn forecast")

    assert [item.template.id for item in first] == [item.template.id for item in second]
    assert first[0].template.id == stronger.id


def test_newly_created_templates_are_retrievable_immediately(db):
    user = default_user(db)
    assert retrieve_templates(db, user.id, "wildcat esoteric metric") == []

    db.add(_template_row(
        "Wildcat esoteric metric",
        "wildcat esoteric metric analysis",
        "analysis wildcat esoteric",
    ))
    db.flush()

    results = retrieve_templates(db, user.id, "wildcat esoteric metric")
    assert len(results) == 1
