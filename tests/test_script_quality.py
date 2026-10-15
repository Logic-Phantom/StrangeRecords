"""대본 품질 / 자연스러운 루프 엔딩 / 제목 후보 / 콘텐츠 검수 (Gemini 호출 없음)."""

from __future__ import annotations

import pytest

from app.ai.gemini_client import ContentValidationError, GeminiClient
from app.ai.metadata_generator import MetadataGenerator, finalize_metadata
from app.ai.script_generator import ScriptGenerator
from app.ai.script_quality import apply_loop_decision, evaluate_script, loop_quality, validate_script_quality
from app.ai.title_selector import select_title
from app.config.settings import GeminiConfig, ScriptConfig
from app.quality.content_checker import fix_title, review_content
from app.schemas import (
    AssetInfo, AudioTrackInfo, ResearchNotes, Scene, ScriptDraft, ShortsContent, SourceDoc, TitleCandidate, TopicCandidate,
    VideoMetadata,
)

HOOK = "1872년 12월, 대서양 한가운데서 사람이 한 명도 없는 배가 발견됐습니다."
BODY = ("배의 이름은 메리 셀레스트호였죠. 식량과 물은 여섯 달 치가 그대로 남아 있었습니다. 사라진 것은 구명보트 하나뿐이었습니다. "
        "유명한 따뜻한 차 이야기는 사실 소설에서 나온 허구였습니다. 알코올 증기를 두려워해 잠시 피했다는 가설이 유력하다고 알려져 있습니다.")
COMMENT = "여러분은 그들이 왜 배를 떠났다고 생각하나요?"
LOOP_END = "그리고 이 모든 기록은 1872년 12월, 대서양의 그 배에서 멈췄습니다."


def draft(script=None, **kw) -> ScriptDraft:
    data = dict(title="사람만 사라진 배", hook=HOOK, script=script or f"{HOOK} {BODY} {COMMENT}", ending="행방은 모릅니다.",
                comment_prompt=COMMENT, truth_status="FACT")
    data.update(kw)
    return ScriptDraft(**data)


CFG = ScriptConfig(min_seconds=1, max_seconds=200, chars_per_second=5.0)


# --------------------------------------------------------------- schema
def test_old_script_json_is_still_valid():
    d = ScriptDraft.model_validate({"title": "t", "hook": "h", "script": "s"})
    assert d.hook_type == "other" and d.story_beats == [] and d.loop_quality is None
    assert ScriptDraft(title="t", hook="h", script="s", hook_type="weird").hook_type == "other"
    TopicCandidate.model_validate({"title": "t", "topic": "x", "category": "c"})  # story_beats/trend_ref 없어도 됨


# ----------------------------------------------------------------- loop
def test_natural_loop_is_accepted():
    script = f"{HOOK} {BODY} {COMMENT} {LOOP_END}"
    score, note = loop_quality(HOOK, LOOP_END, script)
    assert score >= 0.75, note
    d = draft(script=script, ending=LOOP_END, loop=True)
    apply_loop_decision(d, evaluate_script(d, CFG), CFG)
    assert d.loop and d.loop_quality >= 0.75 and "루프 사용" in d.loop_note


@pytest.mark.parametrize("ending, reason", [
    ("그리고 그 배에서 발견된 것은:", "미완성"),
    ("오늘 저녁 메뉴는 김치찌개였습니다.", "겹침"),
    (HOOK, "반복"),
])
def test_unnatural_loop_is_rejected(ending, reason):
    script = f"{HOOK} {BODY} {COMMENT} {ending}"
    d = draft(script=script, ending=ending, loop=True)
    score, note = loop_quality(HOOK, ending, script)
    assert score < 0.75
    apply_loop_decision(d, evaluate_script(d, CFG), CFG)
    assert not d.loop and "루프 미사용" in d.loop_note  # 억지 루프는 쓰지 않음


def test_loop_disabled_by_config():
    cfg = ScriptConfig(min_seconds=1, max_seconds=200)
    cfg.loop_ending.enabled = False
    d = draft(script=f"{HOOK} {BODY} {COMMENT} {LOOP_END}", ending=LOOP_END, loop=True)
    apply_loop_decision(d, evaluate_script(d, cfg), cfg)
    assert not d.loop


def test_loop_ending_must_be_last_sentence():
    score, note = loop_quality(HOOK, LOOP_END, f"{HOOK} {LOOP_END} {COMMENT}")
    assert score == 0 and "마지막 문장" in note


# -------------------------------------------------------------- quality
def test_good_script_passes():
    q = validate_script_quality(draft(), CFG)
    assert not q.problems and q.hook_seconds > 0


def test_repetition_is_rejected():
    s = f"{HOOK} 식량과 물은 여섯 달 치가 그대로 남아 있었습니다. 식량과 물은 여섯 달 치가 그대로 남아 있었습니다. {COMMENT}"
    with pytest.raises(ContentValidationError, match="반복"):
        validate_script_quality(draft(script=s), CFG)


def test_legend_without_hedging_is_rejected():
    s = "크라켄은 바다 괴물입니다. 크라켄은 배를 삼켰습니다. 정말 거대합니다."
    d = draft(script=s, hook="크라켄은 바다 괴물입니다.", truth_status="LEGEND", truth_notice="전설")
    with pytest.raises(ContentValidationError, match="단정"):
        validate_script_quality(d, CFG)
    d.script = s.replace("정말 거대합니다.", "정말 거대했다고 전해집니다.")
    validate_script_quality(d, CFG)


def test_incomplete_final_sentence_is_rejected():
    with pytest.raises(ContentValidationError, match="미완성"):
        validate_script_quality(draft(script=f"{HOOK} {BODY} 그 진실은 다음 편에서 공개합니다."), CFG)


def test_similar_to_recent_script_and_hook_is_rejected():
    previous = [{"title": "예전 영상", "hook": HOOK, "script": f"{HOOK} {BODY} {COMMENT}"}]
    with pytest.raises(ContentValidationError) as exc:
        validate_script_quality(draft(), CFG, previous)
    assert "최근 영상" in str(exc.value) and "첫 문장" in str(exc.value)
    other = [{"title": "크라켄", "hook": "크라켄은 정말 있었을까요?", "script": "크라켄은 북유럽 전설 속 바다 괴물입니다. 대왕오징어가 정체라는 주장도 있습니다."}]
    assert validate_script_quality(draft(), CFG, other).max_similarity < 0.55  # 단어 몇 개 겹친다고 중복 아님


class FakeClient(GeminiClient):
    def __init__(self, responses):
        self.config = GeminiConfig(model="t", json_retries=2)
        self.responses = list(responses)
        self.prompts: list[str] = []

    def generate(self, prompt, **kwargs):
        self.prompts.append(prompt)
        return self.responses.pop(0)


def test_script_generator_regenerates_with_feedback_and_keeps_existing_validation(settings):
    import json

    settings.script.min_seconds, settings.script.max_seconds = 1, 200
    bad = draft(script=f"{HOOK} {BODY} {COMMENT} 그 결과는:")
    good = draft(script=f"{HOOK} {BODY} {COMMENT} {LOOP_END}", ending=LOOP_END, loop=True, hook_type="number",
                 story_beats=["의문", "배경", "반전", "회수"])
    client = FakeClient([bad.model_dump_json(), good.model_dump_json()])
    result = ScriptGenerator(client, settings).generate(TopicCandidate(title="t", topic="x", category="unsolved_mystery"), None,
                                                        previous=[], performance_context="표본 부족")
    assert len(client.prompts) == 2 and "미완성" in client.prompts[1]  # 제한된 횟수만 재요청
    assert result.loop and result.hook_type == "number"
    assert "{{" not in client.prompts[0] and "표본 부족" in client.prompts[0]
    json.dumps(result.model_dump())


# ---------------------------------------------------------------- titles
def content(**kw):
    data = dict(title="사람만 사라진 배", category="unsolved_mystery", topic="1872년 승선원이 사라진 메리 셀레스트호", hook=HOOK,
                script=f"{HOOK} {BODY} {COMMENT}", ending="행방은 아직도 모릅니다.",
                scenes=[Scene(scene_number=1, narration=f"{HOOK} {BODY} {COMMENT}", visual_search_query="ship")],
                truth_status="FACT")
    data.update(kw)
    return ShortsContent(**data)


def test_title_selection_prefers_honest_relevant_title():
    meta = VideoMetadata(title="충격 경악!! 배", description="d", title_candidates=[
        TitleCandidate(strategy="mystery", title="충격 경악!! 배의 비밀", title_en="Shocking ship"),
        TitleCandidate(strategy="evidence", title="1872년 메리 셀레스트호, 남겨진 여섯 달 치 식량", title_en="Mary Celeste, 1872: six months of food left"),
        TitleCandidate(strategy="paradox", title="오늘의 요리 레시피", title_en="Today's recipe"),
    ])
    out = select_title(meta, content(), previous_titles=[], banned=["경악"])
    assert out.title.startswith("1872년 메리 셀레스트호") and out.title_strategy == "evidence"
    assert out.title_en.startswith("Mary Celeste")
    assert len(out.title_selection) == 4 and any(s["excluded"] for s in out.title_selection)


def test_title_similar_to_recent_is_avoided():
    meta = VideoMetadata(title="a", description="d", title_candidates=[
        TitleCandidate(strategy="mystery", title="메리 셀레스트호에서 사라진 사람들", title_en="x"),
        TitleCandidate(strategy="evidence", title="메리 셀레스트호에 남은 여섯 달 치 식량", title_en="y"),
    ])
    out = select_title(meta, content(), previous_titles=["메리 셀레스트호에서 사라진 사람들"])
    assert out.title == "메리 셀레스트호에 남은 여섯 달 치 식량"


def test_metadata_bilingual_years_must_match(settings):
    import json

    bad = {"title": "t", "description": "1872년 사건", "title_en": "T", "description_en": "In 1873 a ship...", "hashtags": []}
    good = dict(bad, description_en="In 1872 a ship...")
    client = FakeClient([json.dumps(bad), json.dumps(good)])
    meta = MetadataGenerator(client, settings).generate(content(), None, [], None)
    assert "연도가 다르다" in client.prompts[1] and "1872" in meta.description


# ---------------------------------------------------------------- review
def _meta(settings, title="메리 셀레스트호에 남은 여섯 달 치 식량", **kw):
    research = ResearchNotes(summary="s", sources=[SourceDoc(title="Wikipedia", url="https://ko.wikipedia.org/wiki/x")])
    raw = VideoMetadata(title=title, description="요약", title_en="Mary Celeste", description_en="Summary", **kw)
    return finalize_metadata(raw, content(), research, ASSETS, BGM), research


ASSETS = [AssetInfo(scene_number=1, media_type="image", local_path="a.png", source="cf_image", generated_by_ai=True,
                    license="AI generated")]
BGM = AudioTrackInfo(file="b.m4a", title="bgm", license="CC0")


def test_review_passes_good_content(settings):
    meta, research = _meta(settings)
    report = review_content(settings, content(), meta, research, ASSETS, BGM, previous=[])
    assert report.status == "passed" and not report.critical and 0 < report.originality_score <= 1


def test_review_blocks_missing_sources_license_and_same_structure(settings):
    meta, _ = _meta(settings)
    stock = [AssetInfo(scene_number=1, media_type="video", local_path="v.mp4", source="pexels", license="")]
    settings.assets.require_ai_image = False
    previous = [{"title": "예전", "hook": HOOK, "script": f"{HOOK} {BODY} {COMMENT}", "hook_type": "anomaly"}]
    report = review_content(settings, content(), meta, None, stock, AudioTrackInfo(file="x"), previous=previous)
    codes = {i.code for i in report.critical}
    assert {"sources_missing", "license_missing", "same_structure"} <= codes and report.status == "review_needed"


def test_review_keeps_ai_image_requirement(settings):
    meta, research = _meta(settings)
    procedural = [AssetInfo(scene_number=1, media_type="image", local_path="p.jpg", source="procedural", license="자체")]
    report = review_content(settings, content(), meta, research, procedural, BGM, previous=[])
    assert "ai_image_required" in {i.code for i in report.critical}
    assert review_content(settings, content(), meta, research, procedural, BGM, previous=[], offline=True).status == "passed"


def test_review_fixes_clickbait_title_with_other_candidate(settings):
    meta, research = _meta(settings, title="충격!! 사라진 배")
    meta.title_selection = [{"title": "충격!! 사라진 배", "score": 0.9, "excluded": ""},
                            {"title": "메리 셀레스트호의 마지막 항해", "score": 0.7, "excluded": "", "title_en": "Last voyage"}]
    report = review_content(settings, content(), meta, research, ASSETS, BGM, previous=[])
    assert [i.code for i in report.fixable] == ["clickbait_title"] and report.status == "passed"
    assert fix_title(meta, report.issues, [], settings) == "메리 셀레스트호의 마지막 항해"
