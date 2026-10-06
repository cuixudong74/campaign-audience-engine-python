"""Shared fixtures."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from app.api import Services, create_app
from app.storage import Storage


@pytest.fixture
def services():
    svc = Services(Storage(":memory:"))
    yield svc
    svc.shutdown()


@pytest.fixture
def client(services):
    from fastapi.testclient import TestClient
    return TestClient(create_app(services))


@pytest.fixture
def file_db(tmp_path):
    path = tmp_path / "test.db"
    return str(path)


def make_file_services(path: str) -> Services:
    return Services(Storage(path))


@pytest.fixture
def active_campaign(services):
    """A campaign in ACTIVE status with small quotas, no scheduling."""
    from datetime import datetime, timedelta, timezone
    c = services.campaigns.create_campaign(
        "promo", total_quota=100, per_user_quota=2,
        start_at=datetime.now(timezone.utc) - timedelta(seconds=10))
    services.campaigns.activate_due()
    return services.campaigns.get(c.id)
