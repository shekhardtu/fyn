from __future__ import annotations

import os

os.environ["PRIMARY_AGENT_ENABLED"] = "false"
# The suite drives the real sign-in path, so a provider credential in a
# developer's .env would send one-time codes to fixture addresses — and a
# fixture address that bounces is suppressed for every later send. Blanking
# the credentials resolves both channels to the console sender for every run.
os.environ["POSTMARK_SERVER_TOKEN"] = ""
os.environ["MSG91_AUTH_KEY"] = ""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.seed import seed_demo_user, seed_system_taxonomy


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as session:
        seed_demo_user(session)
        seed_system_taxonomy(session)
        yield session
    Base.metadata.drop_all(engine)


@pytest.fixture()
def file_store(monkeypatch):
    import hashlib
    from app.services import file_cleanup, attachment_tools, document_assets, files
    from app.services.object_storage import ObjectStorageError

    class MemoryStore:
        objects = {}

        def __init__(self, settings):
            pass

        def read(self, key, limit, *, digest=None):
            content = self.objects[key]
            if len(content) > limit or (digest and hashlib.sha256(content).hexdigest() != digest):
                raise ObjectStorageError("The stored file failed its integrity check.")
            return content

        def write(self, key, content, media_type):
            self.objects[key] = content

        def delete(self, key):
            self.objects.pop(key, None)

    for module in (file_cleanup, attachment_tools, document_assets, files):
        monkeypatch.setattr(module, "R2ObjectStore", MemoryStore)
    return MemoryStore
