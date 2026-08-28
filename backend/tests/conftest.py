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
