from datetime import date

from sqlalchemy import select

from app.models import AnalysisToolTemplate
from app.services.analysis_harness import validate_analysis_tool
from app.services.analysis_seeds import SEED_SPECS, seed_analysis_templates


TODAY = date(2026, 8, 17)


def test_every_seed_passes_the_same_static_policy_as_planner_output():
    for seed in SEED_SPECS:
        report = validate_analysis_tool(seed.build(TODAY), TODAY)
        failed = [check["name"] for check in report["checks"] if not check["passed"]]
        assert report["passed"], f"{seed.capability_name}: {failed}"


def test_seeding_is_idempotent_and_lazily_embedded(db):
    created = seed_analysis_templates(db, today=TODAY)
    templates = list(db.scalars(select(AnalysisToolTemplate)))

    assert created == len(SEED_SPECS) == len(templates)
    assert all(template.status == "active" for template in templates)
    assert all(template.created_by_user_id is None for template in templates)
    # Embeddings are backfilled by retrieval, never stored at seed time.
    assert all(template.retrieval_embedding is None for template in templates)

    assert seed_analysis_templates(db, today=TODAY) == 0
    assert len(list(db.scalars(select(AnalysisToolTemplate)))) == len(SEED_SPECS)


def test_reseeding_restores_curated_retrieval_text_on_a_matching_structure(db):
    seed_analysis_templates(db, today=TODAY)
    template = db.scalar(select(AnalysisToolTemplate).limit(1))
    curated_name = template.capability_name
    template.capability_name = "drifted"
    template.capability_description = "drifted"
    db.flush()

    assert seed_analysis_templates(db, today=TODAY) == 0
    db.refresh(template)
    assert template.capability_name == curated_name
    assert template.capability_description != "drifted"
