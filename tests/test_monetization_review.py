"""수익화 준비 검수: 사실/출처/장면 일치/권리/광고 적합성, 전개 형식, 정보 카드, 음성 권리, 브랜딩 (API 호출 없음)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from PIL import Image

from app.ai.metadata_generator import finalize_metadata
from app.ai.story_format import choose_format, eligible, missing_required
from app.quality.ad_suitability import check_ad_suitability
from app.quality.content_checker import review_content
from app.schemas import (
    AssetInfo, AudioTrackInfo, Explainer, Hypothesis, ResearchFact, ResearchNotes, Scene, ScriptDraft, ShortsContent,
    SourceDoc, SubtitleChunk, TimelineEvent, VideoMetadata,
)
from app.video.info_cards import plan_info_cards
from app.voice.rights import voice_rights

ROOT = Path(__file__).resolve().parents[1]
HOOK = "1872년 12월, 대서양 한가운데서 사람이 한 명도 없는 배가 발견됐습니다."
S2 = "배의 이름은 메리 셀레스트호였고 식량과 물은 여섯 달 치가 남아 있었습니다."
S3 = "사라진 것은 구명보트 하나뿐이었습니다."
S4 = "알코올 증기를 두려워해 잠시 피했다는 가설이 유력하다고 알려져 있습니다."
S5 = "여러분은 그들이 왜 배를 떠났다고 생각하나요?"
SCRIPT = " ".join([HOOK, S2, S3, S4, S5])
SOURCE = SourceDoc(title="Wikipedia(ko) - 메리 셀레스트호", url="https://ko.wikipedia.org/wiki/x", provider="wikipedia",
                   extract="메리 셀레스트호는 1872년 11월 뉴욕을 떠났고 12월 대서양에서 발견되었다. 식량과 물이 남아 있었다.")
RESEARCH = ResearchNotes(summary="1872년 메리 셀레스트호", truth_status="FACT", sources=[SOURCE], facts=[
    ResearchFact(statement="1872년 12월 대서양에서 승선원 없이 발견되었다", status="FACT", source_index=0),
    ResearchFact(statement="식량과 물이 여섯 달 치 남아 있었다", status="FACT", source_index=0),
    ResearchFact(statement="구명보트가 사라져 있었다", status="FACT", source_index=0),
    ResearchFact(statement="알코올 증기를 두려워해 피했다는 가설", status="UNCONFIRMED", source_index=0),
])
SCENES = [Scene(scene_number=i, narration=t, visual_search_query="ship", visual_prompt=f"old sailing ship scene {i}",
                narration_en="An old sailing ship drifting at sea")
          for i, t in enumerate([HOOK, S2, S3, S4, S5], start=1)]
ASSETS = [AssetInfo(scene_number=s.scene_number, media_type="image", local_path="a.png", source="cf_image",
                    generated_by_ai=True, license="AI generated", query=f"{s.visual_prompt}. Style: x") for s in SCENES]
BGM = AudioTrackInfo(file="b.m4a", title="bgm", license="CC0")


def content(script=SCRIPT, scenes=None, **kw):
    scenes = scenes or SCENES
    data = dict(title="사람만 사라진 배", category="unsolved_mystery", topic="메리 셀레스트호", hook=HOOK, script=script,
                scenes=scenes, ending=S5, truth_status="FACT")
    data.update(kw)
    return ShortsContent(**data)


def meta(c=None, research=RESEARCH, title="1872년 메리 셀레스트호에 남은 여섯 달 치 식량"):
    raw = VideoMetadata(title=title, description="1872년 대서양에서 발견된 배 이야기", title_en="Mary Celeste 1872",
                        description_en="A ship found in 1872")
    return finalize_metadata(raw, c or content(), research, ASSETS, BGM)


def codes(report, severity=None):
    return {i.code for i in report.issues if severity is None or i.severity == severity}


# --------------------------------------------------------------- 사실/출처
def test_clean_content_passes_with_only_warnings(settings):
    r = review_content(settings, content(), meta(), RESEARCH, ASSETS, BGM, previous=[])
    assert r.status == "passed", [i.message for i in r.critical]
    assert "voice_rights_unverified" in codes(r, "warning")          # 기본: 경고만
    assert r.ad_suitability["status"] == "pass" and "보장" in r.ad_suitability["note"]


def test_unsourced_year_is_detected_and_strict_blocks(settings):
    script = SCRIPT.replace("여섯 달 치가", "1869년에 실은 여섯 달 치가")
    c = content(script=script, scenes=[*SCENES[:1], SCENES[1].model_copy(update={"narration": S2.replace("여섯 달 치가", "1869년에 실은 여섯 달 치가")}), *SCENES[2:]])
    r = review_content(settings, c, meta(c), RESEARCH, ASSETS, BGM, previous=[])
    assert "unsourced_claim" in codes(r, "warning") and r.status == "passed"
    settings.review.strict = True
    r = review_content(settings, c, meta(c), RESEARCH, ASSETS, BGM, previous=[])
    assert "unsourced_claim" in codes(r, "critical") and r.status == "review_needed"


def test_unconfirmed_claim_stated_as_fact(settings):
    asserted = "알코올 증기를 두려워해 선원들이 잠시 피했습니다."
    script = SCRIPT.replace(S4, asserted)
    scenes = [s.model_copy(update={"narration": asserted}) if s.narration == S4 else s for s in SCENES]
    c = content(script=script, scenes=scenes)
    r = review_content(settings, c, meta(c), RESEARCH, ASSETS, BGM, previous=[])
    assert "unconfirmed_as_fact" in codes(r)
    settings.review.strict = True
    assert "unconfirmed_as_fact" in codes(review_content(settings, c, meta(c), RESEARCH, ASSETS, BGM, previous=[]), "critical")


def test_fact_without_source_link_and_missing_description_link(settings):
    research = RESEARCH.model_copy(update={"facts": [ResearchFact(statement="x", status="FACT", source_index=None)]})
    m = meta()
    m.description = m.description.replace(SOURCE.url, "")
    r = review_content(settings, content(), m, research, ASSETS, BGM, previous=[])
    assert {"fact_unlinked", "source_link_missing"} <= codes(r)


def test_scene_image_generated_for_other_scene_is_blocked(settings):
    swapped = [a.model_copy(update={"query": "a completely different prompt"}) if a.scene_number == 3 else a for a in ASSETS]
    r = review_content(settings, content(), meta(), RESEARCH, swapped, BGM, previous=[])
    assert "visual_scene_mismatch" in codes(r, "critical") and r.status == "review_needed"


def test_unverified_license_is_blocked(settings):
    bgm = AudioTrackInfo(file="b.m4a", title="bgm", license="unknown - 확인 필요")
    r = review_content(settings, content(), meta(), RESEARCH, ASSETS, bgm, previous=[])
    assert "license_unverified" in codes(r, "critical")


# --------------------------------------------------------------- 광고 적합성
def test_ad_suitability_levels():
    ok = check_ad_suitability("메리 셀레스트호의 마지막 항해", SCRIPT, "설명", "FACT", ["old ship at sea"])
    assert ok.status == "pass"
    gore = check_ad_suitability("t", SCRIPT, "d", "FACT", ["a bloody corpse on the deck"])
    assert gore.status == "block"
    accuse = check_ad_suitability("t", "범인은 바로 선장의 동생이었습니다.", "d", "UNCONFIRMED", [])
    assert accuse.status == "review_needed" and accuse.issues[0].code == "unfounded_accusation"
    hedged = check_ad_suitability("t", "범인은 아직 밝혀지지 않았습니다. 선장의 동생이 범인이었다는 주장도 있습니다.", "d", "UNCONFIRMED", [])
    assert hedged.status == "pass"
    shock = check_ad_suitability("끔찍한 유령선", SCRIPT, "d", "FACT", [])
    assert shock.status == "warning"
    legend = check_ad_suitability("실화 괴담", "전해지는 이야기입니다.", "d", "URBAN_LEGEND", [])
    assert legend.status == "review_needed"


def test_ad_block_connects_to_review_gate(settings):
    scenes = [s.model_copy(update={"visual_prompt": "bloody corpse on deck"}) if s.scene_number == 2 else s for s in SCENES]
    assets = [a.model_copy(update={"query": "bloody corpse on deck. Style"}) if a.scene_number == 2 else a for a in ASSETS]
    c = content(scenes=scenes)
    r = review_content(settings, c, meta(c), RESEARCH, assets, BGM, previous=[])
    assert r.status == "review_needed" and r.ad_suitability["status"] == "block"


# ------------------------------------------------------------------ 음성 권리
def test_voice_rights_never_auto_verified(settings):
    assert voice_rights(settings).status == "unverified"
    settings.voice.rights_status = "verified"          # 근거 없이 verified 로 바꿔도 인정하지 않음
    assert voice_rights(settings).status == "unverified"
    settings.voice.rights_evidence = "docs/legal/microsoft-reply.pdf"
    assert voice_rights(settings).ok
    settings.voice.rights_status, settings.voice.rights_evidence = "unverified", ""
    settings.review.unverified_rights_policy = "review_needed"
    assert voice_rights(settings).review_severity == "critical"


# ------------------------------------------------------------- 전개 형식/설명
def test_story_format_rotates_and_respects_category():
    assert eligible("brain_teaser", "FACT") == ["classic"]
    assert "science_vs_legend" not in eligible("unsolved_mystery", "FACT")
    picks = {choose_format("unsolved_mystery", "FACT", seed=f"job{i}") for i in range(30)}
    assert len(picks) >= 3                                               # 같은 형식만 반복하지 않음
    assert choose_format("strange_phenomena", "URBAN_LEGEND", ["two_hypotheses", "evidence"], seed="x") not in (
        "two_hypotheses", "evidence")
    assert missing_required("two_hypotheses", Explainer(hypotheses=[Hypothesis(name="a")], unresolved=["x"])) == ["hypotheses(2개 이상)"]


def test_explainer_is_lenient_and_old_drafts_still_load():
    d = ScriptDraft.model_validate({"title": "t", "hook": "h", "script": "s", "story_format": "weird",
                                    "explainer": {"confirmed_facts": "한 줄", "hypotheses": ["가설1"], "timeline": ["1872년 출항"]}})
    assert d.story_format == "classic" and d.explainer.confirmed_facts == ["한 줄"]
    assert d.explainer.hypotheses[0].name == "가설1" and d.explainer.timeline[0].what == "1872년 출항"
    assert ScriptDraft.model_validate({"title": "t", "hook": "h", "script": "s", "explainer": None}).explainer.is_empty()


def test_explainer_warnings_in_review(settings):
    draft = {"story_format": "two_hypotheses", "explainer": {"confirmed_facts": ["a"], "hypotheses": [{"name": "하나"}]}}
    r = review_content(settings, content(), meta(), RESEARCH, ASSETS, BGM, previous=[], draft=draft)
    assert {"explainer_incomplete", "unique_insight_missing"} <= codes(r, "warning") and r.status == "passed"


# ------------------------------------------------------------------ 정보 카드
EXPLAINER = Explainer(
    confirmed_facts=["식량과 물 여섯 달 치", "구명보트가 사라짐", "오늘 새로 지어낸 사실"],
    unconfirmed_claims=["알코올 증기 때문에 피했다는 가설"],
    key_clues=["사라진 구명보트"],
    hypotheses=[Hypothesis(name="알코올 증기 가설", evidence="화물이 알코올"), Hypothesis(name="해적 습격 가설")],
    timeline=[TimelineEvent(when="1872년 11월", what="뉴욕 출항"), TimelineEvent(when="1872년 12월", what="대서양에서 발견")],
)


def test_info_cards_only_use_grounded_items(settings):
    cards = plan_info_cards(SCENES, EXPLAINER, RESEARCH, "classic", settings.video.info_cards)
    assert 1 <= len(cards) <= settings.video.info_cards.max_cards
    assert all(c.scene_number not in (1, 5) for c in cards)              # Hook·마지막 장면 제외
    facts = next(c for c in cards if c.kind == "facts")
    texts = " ".join(t for _, t in facts.rows)
    assert "지어낸" not in texts and any(label == "확인" for label, _ in facts.rows)
    assert not any(c.kind == "hypotheses" for c in cards)               # '해적 습격 가설'은 대본에 없음 → 가설 카드 안 만듦
    assert not any(c.kind == "timeline" for c in cards)                 # 시점 2개뿐 → 타임라인 카드 안 만듦
    assert plan_info_cards(SCENES, EXPLAINER, None, "classic", settings.video.info_cards)[0].kind != "facts"  # 자료 없으면 '확인' 카드 없음


def test_card_and_ai_label_are_drawn(settings, tmp_path):
    from app.video.subtitle_renderer import SubtitleRenderer

    renderer = SubtitleRenderer(settings.subtitle, settings.video, settings.paths.fonts)
    card = plan_info_cards(SCENES, EXPLAINER, RESEARCH, "classic", settings.video.info_cards)[0]
    chunk = SubtitleChunk(index=1, start=0, end=1, text="자막", scene_number=card.scene_number)
    plain = Image.open(renderer.render_chunk(chunk, tmp_path / "a.png"))
    renderer.ai_label = True
    with_card = Image.open(renderer.render_chunk(chunk, tmp_path / "b.png", card=card))
    top = (0, 0, plain.width, int(plain.height * 0.55))
    assert plain.crop(top).getbbox() != with_card.crop(top).getbbox()
    assert with_card.getpixel((int(plain.width * 0.5), int(plain.height * 0.25)))[3] > 0   # 카드 영역이 그려짐


# ------------------------------------------------------------------ 브랜딩
def test_branding_check_matches_docs_and_scopes(settings):
    shutil.copytree(ROOT / "docs", settings.paths.root / "docs")
    shutil.copytree(ROOT / "assets" / "branding", settings.paths.root / "assets" / "branding",
                    ignore=shutil.ignore_patterns("output"))
    from app.branding.channel import check_consistency, generate_assets

    generate_assets(settings)
    report = {i["name"]: i for i in check_consistency(settings)}
    assert all(i["level"] != "fail" for i in report.values()), [i for i in report.values() if i["level"] == "fail"]
    assert report["범위 youtube.force-ssl"]["level"] == "ok"
    assert report["자산 banner"]["level"] == "ok"
    # 기존 홈페이지·약관 URL(파일)을 보존
    for name in ("index.html", "privacy.html", "terms.html"):
        assert (ROOT / "docs" / name).exists()


class _Req:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value() if callable(self.value) else self.value


class FakeChannels:
    def __init__(self):
        self.item = {"id": "UCme", "snippet": {"title": "팬텀로즥"},
                     "brandingSettings": {"channel": {"title": "팬텀로즥", "description": "옛 소개",
                                                      "unsubscribedTrailer": "trailer123", "keywords": "old"},
                                          "image": {"bannerExternalUrl": "https://yt/banner"},
                                          "hints": [{"property": "x"}]}}
        self.updates: list[dict] = []

    def channels(self):
        fake = self

        class C:
            def list(self, **kw):
                return _Req(lambda: {"items": [fake.item]})

            def update(self, part, body):
                def run():
                    fake.updates.append(body)
                    fake.item["brandingSettings"] = body["brandingSettings"]
                    return body
                return _Req(run)
        return C()


def _brand_settings(settings):
    shutil.copytree(ROOT / "assets" / "branding", settings.paths.root / "assets" / "branding",
                    ignore=shutil.ignore_patterns("output"))
    return settings


def test_branding_plan_is_dry_run_and_keeps_existing_fields(settings):
    from app.branding.channel import ChannelBranding

    fake = FakeChannels()
    service = ChannelBranding(_brand_settings(settings), service_factory=lambda: fake)
    plan = service.plan()
    assert {c["field"] for c in plan["changes"]} >= {"description", "keywords"} and fake.updates == []  # 미리보기는 변경 없음
    result = service.apply(plan)
    sent = fake.updates[0]["brandingSettings"]
    assert sent["channel"]["unsubscribedTrailer"] == "trailer123"     # 기존 필드 유지 (누락 → 삭제 방지)
    assert sent["channel"]["title"] == "팬텀로즥" and sent["image"]["bannerExternalUrl"] == "https://yt/banner"
    assert "hints" not in sent
    assert result["ok"] and all(result["verified"].values())
    assert any("프로필 사진" in m for m in result["manual_required"])  # API 미지원 항목은 성공으로 기록하지 않음
    assert Path(result["log"]).exists()


def test_branding_apply_unverified_change_is_not_success(settings):
    from app.branding.channel import ChannelBranding

    fake = FakeChannels()
    service = ChannelBranding(_brand_settings(settings), service_factory=lambda: fake)
    plan = service.plan()
    real_channels = fake.channels

    def ignoring_channels():
        c = real_channels()
        c.update = lambda part, body: _Req(lambda: fake.updates.append(body))  # 요청은 성공했지만 실제로는 반영 안 됨
        return c

    fake.channels = ignoring_channels
    assert service.apply(plan)["ok"] is False


def test_video_footer_in_description(settings):
    from app.branding.channel import video_footer

    _brand_settings(settings)
    footer = video_footer(settings)
    m = finalize_metadata(VideoMetadata(title="t", description="d"), content(), RESEARCH, ASSETS, BGM, footer=footer)
    assert footer and footer in m.description and m.description.rstrip().endswith("#Shorts")


@pytest.mark.parametrize("name", ["index.html", "privacy.html", "terms.html"])
def test_docs_pages_keep_links(name):
    text = (ROOT / "docs" / name).read_text(encoding="utf-8")
    assert "기묘한 기록" in text and ("privacy.html" in text or name == "privacy.html")
