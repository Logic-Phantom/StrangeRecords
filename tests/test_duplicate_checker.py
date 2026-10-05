"""주제 중복 검사: 단어가 달라도 핵심 내용이 같으면 중복."""

from __future__ import annotations

from app.config.settings import DuplicateConfig
from app.database.models import TopicRecord
from app.schemas import DuplicateJudgement, DuplicateJudgements, TopicCandidate
from app.topics.duplicate_checker import DuplicateChecker, extract_keywords, keyword_overlap


def record(title: str, topic: str, keywords: list[str] | None = None, rid: int = 1) -> TopicRecord:
    return TopicRecord(id=rid, title=title, category="unsolved_mystery", topic=topic, keywords=keywords or [],
                       summary="", truth_status="FACT", created_at="2026-01-01", status="uploaded")


def cand(title: str, topic: str | None = None, keywords: list[str] | None = None) -> TopicCandidate:
    return TopicCandidate(title=title, topic=topic or title, category="unsolved_mystery", keywords=keywords or [])


def test_spec_example_is_duplicate_without_ai():
    checker = DuplicateChecker(DuplicateConfig(semantic_check=False))
    history = [record("타이타닉에서 사라진 승객", "타이타닉에서 사라진 승객")]
    result = checker.check([cand("타이타닉호 실종 승객의 미스터리")], history)[0]
    assert result.duplicate, result


def test_keyword_normalization():
    a = extract_keywords("타이타닉에서 사라진 승객")
    b = extract_keywords("타이타닉호 실종 승객의 미스터리")
    assert "실종" in a and "승객" in b
    assert keyword_overlap(a, b) >= 0.67


def test_different_topics_not_duplicate():
    checker = DuplicateChecker(DuplicateConfig(semantic_check=False))
    history = [record("타이타닉에서 사라진 승객", "타이타닉 침몰 당시 실종된 승객")]
    result = checker.check([cand("크라켄 전설의 기원", "북유럽 바다 괴물 크라켄 전설은 어디서 왔나")], history)[0]
    assert not result.duplicate


def test_duplicate_among_candidates():
    checker = DuplicateChecker(DuplicateConfig(semantic_check=False))
    results = checker.check(
        [cand("메리 셀레스트호 실종", keywords=["메리 셀레스트호", "유령선"]),
         cand("유령선 메리 셀레스트호의 비밀", keywords=["메리 셀레스트호", "유령선"])],
        [],
    )
    assert not results[0].duplicate and results[1].duplicate


class FakeGemini:
    """의미 판단 단계만 흉내내는 테스트 더블 (실제 서비스는 Gemini 만 사용)."""

    def __init__(self, duplicate_indexes: set[int]):
        self.duplicate_indexes = duplicate_indexes
        self.prompts: list[str] = []

    def generate_json(self, prompt, model, **kwargs):
        self.prompts.append(prompt)
        return DuplicateJudgements(results=[
            DuplicateJudgement(index=i, duplicate=i in self.duplicate_indexes, similar_to="x", reason="same")
            for i in range(5)
        ])


def test_semantic_check_marks_duplicate():
    fake = FakeGemini({0})
    checker = DuplicateChecker(DuplicateConfig(semantic_check=True), client=fake)  # type: ignore[arg-type]
    history = [record("바다에서 흔적 없이 사라진 배", "1872년 대서양 유령선")]
    results = checker.check([cand("선원 열 명이 증발한 범선", "승선원이 사라진 채 표류한 범선")], history)
    assert results[0].duplicate and results[0].method == "gemini"
    assert fake.prompts, "Gemini 의미 판단이 호출되어야 함"
