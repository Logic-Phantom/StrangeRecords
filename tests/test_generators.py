"""Gemini 생성기 연결 테스트 (프롬프트 변수 치환 + JSON → 모델 변환).

실제 서비스는 Gemini API 만 사용한다. 여기서는 API 호출 없이 응답을 흉내낸다.
"""

from __future__ import annotations

import json

from app.ai.metadata_generator import MetadataGenerator
from app.ai.scene_generator import SceneGenerator
from app.ai.script_generator import ScriptGenerator
from app.ai.topic_generator import TopicGenerator
from app.database.db import Database
from app.database.repositories import TopicRepository
from app.research.researcher import Researcher
from app.schemas import ResearchNotes, ScriptDraft, ShortsContent, TopicCandidate
from app.topics.duplicate_checker import DuplicateChecker
from app.topics.selector import TopicSelector

SCRIPT = (
    "이 배는 바다 한가운데서 흔적도 없이 사라졌습니다. 1872년 대서양에서 발견된 메리 셀레스트호에는 아무도 없었죠. "
    "식량과 물은 여섯 달 치가 그대로 남아 있었고 화물도 대부분 멀쩡했습니다. 사라진 것은 구명보트 하나뿐이었습니다. 마지막 항해 기록은 발견 열흘 전에 멈춰 있었죠. "
    "그런데 이상한 건 지금부터입니다. 유명한 따뜻한 차 이야기는 사실 소설에서 나온 허구였습니다. "
    "알코올 증기를 두려워해 잠시 피했다는 가설이 유력하지만 그들의 행방은 아직도 모릅니다. "
    "여러분은 그들이 왜 배를 떠났다고 생각하나요? 댓글로 알려주세요."
)


class ScriptedGemini:
    def __init__(self, responses: dict[str, object]):
        self.responses = responses
        self.prompts: list[str] = []

    def generate_json(self, prompt, model, *, validate=None, **kwargs):
        self.prompts.append(prompt)
        assert "{{" not in prompt, "치환되지 않은 프롬프트 변수"
        data = self.responses[model.__name__]
        result = model.model_validate(data)
        if validate:
            validate(result)
        return result


def responses():
    sentences = [s.strip() + ("" if s.strip().endswith(("?", ".")) else ".") for s in SCRIPT.split(". ") if s.strip()]
    scenes = [
        {"scene_number": i + 1, "narration": s, "visual_search_query": "dark ocean ship",
         "alt_search_queries": ["sea"], "visual_type": "video", "effect": "zoom_in", "sfx": None}
        for i, s in enumerate(sentences)
    ]
    return {
        "TopicCandidates": {"candidates": [
            {"title": "메리 셀레스트호", "topic": "1872년 승선원이 사라진 메리 셀레스트호", "category": "x",
             "keywords": ["메리 셀레스트호", "유령선"], "truth_status": "FACT", "search_terms_ko": ["메리 셀레스트호"]},
        ]},
        "ResearchNotes": {"summary": "요약", "truth_status": "FACT", "facts": [{"statement": "사실", "status": "FACT", "source_index": 0}]},
        "ScriptDraft": {"title": "사람만 사라진 배", "hook": "이 배는 바다 한가운데서 흔적도 없이 사라졌습니다.", "script": SCRIPT,
                        "ending": "행방은 모릅니다.", "comment_prompt": "왜 떠났을까요?", "truth_status": "FACT"},
        "SceneList": {"scenes": scenes},
        "VideoMetadata": {"title": "바다에서 사라진 사람들", "description": "요약입니다.", "hashtags": ["#미스터리"], "tags": ["유령선"]},
    }


def test_full_ai_chain(settings, tmp_path):
    fake = ScriptedGemini(responses())
    topics = TopicRepository(Database(tmp_path / "db.sqlite"))
    selector = TopicSelector(settings, TopicGenerator(fake, settings), DuplicateChecker(settings.duplicate, fake), topics)  # type: ignore[arg-type]
    selection = selector.select("unsolved_mystery")
    candidate = selection.selected
    assert candidate.category == "unsolved_mystery"
    assert "미제 사건" in fake.prompts[0]

    settings.research.enabled = False  # 네트워크 없이
    notes = Researcher(settings, fake).research(candidate)  # type: ignore[arg-type]
    assert isinstance(notes, ResearchNotes)

    draft = ScriptGenerator(fake, settings).generate(candidate, notes)  # type: ignore[arg-type]
    assert isinstance(draft, ScriptDraft) and draft.hook
    script_prompt = fake.prompts[-1]
    assert str(settings.script.min_chars) in script_prompt and "사실 목록" in script_prompt

    scenes = SceneGenerator(fake, settings).generate(draft, candidate)  # type: ignore[arg-type]
    assert [s.scene_number for s in scenes] == list(range(1, len(scenes) + 1))

    content = ShortsContent(title=draft.title, category=candidate.category, topic=candidate.topic, hook=draft.hook,
                            script=draft.script, scenes=scenes, truth_status=draft.truth_status)
    meta = MetadataGenerator(fake, settings).generate(content, notes, [], None)  # type: ignore[arg-type]
    assert meta.hashtags[-1] == "#Shorts" and "AI" in meta.description


def test_history_is_sent_to_topic_prompt(settings, tmp_path):
    fake = ScriptedGemini(responses())
    topics = TopicRepository(Database(tmp_path / "db.sqlite"))
    topics.add(TopicCandidate(title="크라켄의 정체", topic="크라켄 전설", category="legendary_creatures"))
    TopicGenerator(fake, settings).generate(settings.categories[2], topics.history())  # type: ignore[arg-type]
    assert "크라켄의 정체" in fake.prompts[0]
    json.dumps(responses())  # 직렬화 가능
