from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config.settings import Paths, load_settings  # noqa: E402
from app.database.db import Database  # noqa: E402


@pytest.fixture
def settings(tmp_path):
    s = load_settings()
    s.paths = Paths(root=tmp_path)
    s.paths.ensure()
    s.retry.delays = [(0, 0), (0, 0)]
    s.secrets.hf_api_key = ""  # 테스트에서 실제 Hugging Face / Cloudflare 를 호출하지 않도록
    s.secrets.cloudflare_account_id = s.secrets.cloudflare_api_token = ""
    return s


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "test.sqlite")
