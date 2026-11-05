import pytest

from analyst.bq import FakeBigQuery
from analyst.config import Settings, User
from analyst.llm import FakeLLM
from analyst.runtime import build_runtime


@pytest.fixture
def settings(tmp_path):
    s = Settings()
    s.data_dir = tmp_path / "data"
    s.data_dir.mkdir()
    s.golden_path = tmp_path / "trios.jsonl"
    s.golden_path.write_text(
        '{"id": "g1", "status": "approved", "tags": ["revenue"], "question": "monthly revenue last 6 months", '
        '"sql": "SELECT 1", "report": "exclude cancelled"}\n',
        encoding="utf-8",
    )
    return s


@pytest.fixture
def alice():
    return User(id="alice", name="Alice", role="Brand Manager", brands=["Calvin Klein", "Levi's"])


@pytest.fixture
def bob():
    return User(id="bob", name="Bob", role="Brand Manager", brands=["Diesel"])


@pytest.fixture
def ceo():
    return User(id="ceo", name="Dana", role="CEO", brands=[])


@pytest.fixture
def rt(settings):
    return build_runtime(settings)


@pytest.fixture
def make_agent(rt):
    def _make(user, llm=None, bq=None, conversation_id="conv1"):
        return rt.agent(user, llm or FakeLLM(), bq or FakeBigQuery(), conversation_id)

    return _make
