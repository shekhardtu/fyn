from __future__ import annotations

import hashlib
import json
import logging
import re
from functools import lru_cache
from uuid import UUID

from agno.db.postgres import PostgresDb
from agno.memory import MemoryManager, UserMemory

from ..config import get_settings
from ..database import engine
from ..models import Category, Subcategory


logger = logging.getLogger(__name__)


def _memory_enabled() -> bool:
    settings = get_settings()
    return settings.primary_agent_enabled and settings.database_url.startswith("postgresql")


@lru_cache
def _agno_memory_db() -> PostgresDb | None:
    # Account export/deletion must still reach persisted memory when the agent
    # itself is temporarily disabled. Storage availability and agent behavior
    # are separate concerns.
    if not get_settings().database_url.startswith("postgresql"):
        return None
    # Agno owns the schema of this table and creates/upgrades it as documented.
    # We deliberately do not attach this Db to Agent session storage: the app's
    # conversations table remains the single thread-history source of truth.
    return PostgresDb(db_engine=engine, memory_table="agno_user_memories")


def agent_memory_manager() -> MemoryManager | None:
    if not _memory_enabled():
        return None
    memory_db = _agno_memory_db()
    return MemoryManager(db=memory_db) if memory_db else None


def remember_taxonomy_mapping(
    user_id: UUID,
    category: Category,
    subcategory: Subcategory | None,
    alias: str | None = None,
) -> None:
    """Upsert a privacy-minimized, user-scoped categorization memory.

    Amounts, transaction descriptions and raw prompts are intentionally never
    stored here. Canonical taxonomy IDs remain in the finance tables; this
    memory only helps the model interpret future language consistently.
    """
    memory_db = _agno_memory_db() if _memory_enabled() else None
    if not memory_db:
        return
    subcategory_name = subcategory.name if subcategory else None
    user_term = " ".join(re.sub(r"[\x00-\x1f\x7f]+", " ", alias or subcategory_name or category.name).split())[:80]
    identity = f"taxonomy:{user_id}:{category.id}:{subcategory.id if subcategory else '-'}:{user_term.casefold()}"
    memory_id = hashlib.sha256(identity.encode()).hexdigest()[:32]
    hierarchy = category.name + (f" → {subcategory_name}" if subcategory_name else "")
    content = json.dumps({
        "kind": "finance_taxonomy_mapping",
        "term": user_term,
        "category": category.name,
        "subcategory": subcategory_name,
        "hierarchy": hierarchy,
        "authority": "user_explicit",
    }, ensure_ascii=False)
    manager = MemoryManager(db=memory_db)
    memory = UserMemory(
        memory_id=memory_id,
        memory=content,
        topics=["finance_taxonomy", "categorization"],
        user_id=str(user_id),
        input=f"taxonomy mapping: {user_term} -> {hierarchy}",
    )
    try:
        existing = manager.get_user_memory(memory_id, user_id=str(user_id))
        if existing:
            manager.replace_user_memory(memory_id, memory, user_id=str(user_id))
        else:
            manager.add_user_memory(memory, user_id=str(user_id))
    except Exception as error:  # Memory must never block a financial workflow.
        logger.warning("Unable to persist user taxonomy memory: %s", type(error).__name__)


def export_user_memories(user_id: UUID) -> list[dict]:
    """Export every Agno memory in the authenticated user's namespace."""
    memory_db = _agno_memory_db()
    if not memory_db:
        return []
    memories = MemoryManager(db=memory_db).get_user_memories(user_id=str(user_id)) or []
    return [memory.to_dict() for memory in memories]


def clear_user_memories(user_id: UUID) -> int:
    """Delete every Agno memory before the owning account is removed."""
    memory_db = _agno_memory_db()
    if not memory_db:
        return 0
    manager = MemoryManager(db=memory_db)
    memories = manager.get_user_memories(user_id=str(user_id)) or []
    manager.clear_user_memories(user_id=str(user_id))
    return len(memories)
