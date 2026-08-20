"""In-process hybrid retrieval over the shared analysis-template pool.

Retrieval is a hypothesis generator, never an authority: it proposes candidate
templates that the binding layer must deterministically validate before
anything executes. Ranking fuses two independent signals — hand-rolled Okapi
BM25 over each template's retrieval document (lexical: merchant names,
negations, exact vocabulary) and cosine similarity over lazily backfilled
512-dimension embeddings (semantic: paraphrase) — with reciprocal-rank fusion.
The lexical half is pure local computation, so retrieval keeps working with the
embeddings provider down or disabled; results then rank on BM25 alone.

The corpus is the canonical ``analysis_tool_templates`` table — there is no
second search store to drift. At the 50-row prefilter ceiling, rebuilding the
BM25 statistics per call is cheaper than maintaining a cache, so freshness is
structural rather than invalidated.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from uuid import UUID

from openai import OpenAI
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..domain import AnalysisToolStatus
from ..models import AnalysisToolTemplate, UserAnalysisTool
from .manifest import native_manifest_fingerprint
from .semantic_registry import semantic_schema_registry


# v3: visualization specifications were removed from the plan grammar — every
# result renders as markdown, so v2 templates (whose canonical structure and
# hashes included view declarations) are flushed and reseeded on startup.
ANALYSIS_TEMPLATE_VERSION = "governed-analysis-template.v3"

_RRF_OFFSET = 60
_COSINE_FLOOR = 0.15
_BM25_K1 = 1.5
_BM25_B = 0.75
_PREFILTER_LIMIT = 50

_TOKEN_ALIASES = {
    "spending": "spend",
    "spent": "spend",
    "expenses": "expense",
    "earnings": "income",
    "monthly": "month",
    "projected": "projection",
    "projecting": "projection",
}
_TOKEN_STOPWORDS = frozenset({"the", "and", "for", "this", "that", "with"})


def _token_stream(value: str) -> list[str]:
    return [
        _TOKEN_ALIASES.get(token, token)
        for token in re.findall(r"[a-z0-9]+", value.casefold())
        if len(token) > 2 and token not in _TOKEN_STOPWORDS
    ]


def _tokenize(value: str) -> set[str]:
    return set(_token_stream(value))


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    denominator = math.sqrt(sum(value * value for value in left)) * math.sqrt(sum(value * value for value in right))
    return sum(a * b for a, b in zip(left, right)) / denominator if denominator else 0.0


def _retrieval_document(template: AnalysisToolTemplate) -> str:
    return (
        f"Capability: {template.capability_name}\n"
        f"Description: {template.capability_description}\n"
        f"Signature: {template.capability_signature}\n"
        f"Plan template: {json.dumps(template.plan_template, sort_keys=True, default=str)}"
    )


@dataclass(frozen=True)
class RetrievedTemplate:
    template: AnalysisToolTemplate
    fused_score: float
    bm25_rank: int | None
    cosine_rank: int | None
    saved_by_user: bool


def _ordered_ids(
    scores: dict[UUID, float],
    templates: list[AnalysisToolTemplate],
) -> list[UUID]:
    """Order scored templates deterministically: exact score ties fall back to
    adoption evidence (success count) before the arbitrary id."""
    success = {template.id: template.success_count for template in templates}
    return [
        template_id
        for template_id, _score in sorted(
            scores.items(),
            key=lambda item: (-item[1], -success.get(item[0], 0), str(item[0])),
        )
    ]


def _bm25_ranking(templates: list[AnalysisToolTemplate], query_tokens: set[str]) -> list[UUID]:
    """Okapi BM25 ranks by lexical evidence; only positive-score documents rank."""
    if not query_tokens:
        return []
    documents = {template.id: _token_stream(_retrieval_document(template)) for template in templates}
    total = len(documents)
    average_length = sum(len(tokens) for tokens in documents.values()) / total if total else 0.0
    frequencies = {
        template_id: {token: tokens.count(token) for token in set(tokens)}
        for template_id, tokens in documents.items()
    }
    scores: dict[UUID, float] = {}
    for token in query_tokens:
        containing = sum(1 for terms in frequencies.values() if token in terms)
        if not containing:
            continue
        idf = math.log(1.0 + (total - containing + 0.5) / (containing + 0.5))
        for template_id, terms in frequencies.items():
            frequency = terms.get(token)
            if not frequency:
                continue
            length_norm = 1.0 - _BM25_B + _BM25_B * (len(documents[template_id]) / average_length)
            scores[template_id] = scores.get(template_id, 0.0) + idf * (
                frequency * (_BM25_K1 + 1.0) / (frequency + _BM25_K1 * length_norm)
            )
    return _ordered_ids(scores, templates)


def _ensure_embeddings(
    db: Session,
    prompt: str,
    templates: list[AnalysisToolTemplate],
    settings,
) -> list[float] | None:
    """Embed the prompt and backfill missing template embeddings in one call.

    Failure degrades retrieval to BM25-only rather than stalling the turn, so
    the provider call keeps its short timeout and swallows errors.
    """
    try:
        missing = [
            template for template in templates
            if not template.retrieval_embedding or template.retrieval_embedding_model != settings.embedding_model
        ]
        inputs = [prompt, *[_retrieval_document(template) for template in missing]]
        response = OpenAI(api_key=settings.openai_api_key, timeout=15, max_retries=1).embeddings.create(
            model=settings.embedding_model,
            input=inputs,
            dimensions=512,
        )
        template_embeddings = response.data[1:]
        if len(template_embeddings) != len(missing):
            raise ValueError("Embedding response did not match the template batch")
        for template, item in zip(missing, template_embeddings):
            template.retrieval_embedding = item.embedding
            template.retrieval_embedding_model = settings.embedding_model
        return response.data[0].embedding
    except Exception:
        return None


def _cosine_ranking(prompt_embedding: list[float], templates: list[AnalysisToolTemplate]) -> list[UUID]:
    scored = {
        template.id: _cosine_similarity(prompt_embedding, template.retrieval_embedding or [])
        for template in templates
    }
    qualifying = {template_id: score for template_id, score in scored.items() if score >= _COSINE_FLOOR}
    return _ordered_ids(qualifying, templates)


def retrieve_templates(db: Session, user_id: UUID, prompt: str, *, limit: int = 5) -> list[RetrievedTemplate]:
    """Rank current-registry active templates for a prompt; no user data leaves scope."""
    registry = semantic_schema_registry()
    templates = list(db.scalars(
        select(AnalysisToolTemplate)
        .where(
            AnalysisToolTemplate.status == AnalysisToolStatus.ACTIVE,
            AnalysisToolTemplate.template_version == ANALYSIS_TEMPLATE_VERSION,
            AnalysisToolTemplate.semantic_registry_version == registry.version,
            AnalysisToolTemplate.source_manifest_hash == native_manifest_fingerprint(),
        )
        .order_by(AnalysisToolTemplate.last_used_at.desc().nullslast(), AnalysisToolTemplate.success_count.desc())
        .limit(_PREFILTER_LIMIT)
    ))
    if not templates:
        return []
    saved_template_ids = set(db.scalars(
        select(UserAnalysisTool.template_id).where(
            UserAnalysisTool.user_id == user_id,
            UserAnalysisTool.status == AnalysisToolStatus.ACTIVE,
        )
    ))
    rankings: list[list[UUID]] = []
    bm25_ranking = _bm25_ranking(templates, _tokenize(prompt))
    if bm25_ranking:
        rankings.append(bm25_ranking)
    cosine_ranking: list[UUID] = []
    settings = get_settings()
    if settings.openai_api_key and settings.primary_agent_enabled:
        prompt_embedding = _ensure_embeddings(db, prompt, templates, settings)
        if prompt_embedding:
            cosine_ranking = _cosine_ranking(prompt_embedding, templates)
            if cosine_ranking:
                rankings.append(cosine_ranking)
    fused: dict[UUID, float] = {}
    for ranking in rankings:
        for rank, template_id in enumerate(ranking, start=1):
            fused[template_id] = fused.get(template_id, 0.0) + 1.0 / (_RRF_OFFSET + rank)
    if not fused:
        return []
    by_id = {template.id: template for template in templates}
    bm25_positions = {template_id: rank for rank, template_id in enumerate(bm25_ranking, start=1)}
    cosine_positions = {template_id: rank for rank, template_id in enumerate(cosine_ranking, start=1)}
    results = [
        RetrievedTemplate(
            template=by_id[template_id],
            fused_score=score,
            bm25_rank=bm25_positions.get(template_id),
            cosine_rank=cosine_positions.get(template_id),
            saved_by_user=template_id in saved_template_ids,
        )
        for template_id, score in fused.items()
    ]
    results.sort(
        key=lambda item: (
            -item.fused_score,
            not item.saved_by_user,
            -item.template.success_count,
            str(item.template.id),
        )
    )
    return results[:limit]
