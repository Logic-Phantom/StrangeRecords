"""자료 조사: Wikipedia(무료 공개 API)에서 근거 자료를 모으고 Gemini 로 사실/추측을 구분해 정리한다."""

from __future__ import annotations

from urllib.parse import quote

import requests

from app.ai.gemini_client import GeminiClient, load_prompt
from app.config.settings import ResearchConfig, Settings
from app.schemas import ResearchNotes, SourceDoc, TopicCandidate
from app.utils.logger import get_logger
from app.utils.retry import retry_call

logger = get_logger("research")


class WikipediaClient:
    def __init__(self, config: ResearchConfig):
        self.config = config
        self.session = requests.Session()
        self.session.headers["User-Agent"] = config.user_agent

    def _api(self, lang: str, params: dict) -> dict:
        def _call() -> dict:
            resp = self.session.get(
                f"https://{lang}.wikipedia.org/w/api.php",
                params={"format": "json", "formatversion": 2, **params},
                timeout=20,
            )
            resp.raise_for_status()
            return resp.json()

        return retry_call(_call, step=f"Wikipedia({lang})", max_attempts=2, delays=((2, 4),))

    def search(self, lang: str, query: str, limit: int = 2) -> list[str]:
        data = self._api(lang, {"action": "query", "list": "search", "srsearch": query, "srlimit": limit})
        return [item["title"] for item in data.get("query", {}).get("search", [])]

    def extract(self, lang: str, title: str) -> SourceDoc | None:
        data = self._api(
            lang,
            {"action": "query", "prop": "extracts", "explaintext": 1, "redirects": 1, "titles": title},
        )
        pages = data.get("query", {}).get("pages", [])
        if not pages or pages[0].get("missing"):
            return None
        page = pages[0]
        text = (page.get("extract") or "").strip()
        if len(text) < 80:
            return None
        return SourceDoc(
            title=f"Wikipedia({lang}) - {page['title']}",
            url=f"https://{lang}.wikipedia.org/wiki/{quote(page['title'].replace(' ', '_'))}",
            lang=lang,
            provider="wikipedia",
            extract=text[: self.config.max_chars_per_source],
        )


class Researcher:
    def __init__(self, settings: Settings, client: GeminiClient | None):
        self.settings = settings
        self.config = settings.research
        self.client = client
        self.wiki = WikipediaClient(self.config)

    def collect_sources(self, candidate: TopicCandidate) -> list[SourceDoc]:
        sources: list[SourceDoc] = []
        seen: set[str] = set()
        terms = {
            "ko": candidate.search_terms_ko or candidate.keywords[:2] or [candidate.title],
            "en": candidate.search_terms_en,
        }
        for lang in self.config.languages:
            for query in terms.get(lang, []):
                if len(sources) >= self.config.max_sources:
                    return sources
                try:
                    titles = self.wiki.search(lang, query, limit=1)
                    for title in titles:
                        if (lang, title) in seen:
                            continue
                        seen.add((lang, title))
                        doc = self.wiki.extract(lang, title)
                        if doc:
                            sources.append(doc)
                except Exception as exc:
                    logger.warning("Wikipedia 검색 실패 (%s, %s): %s", lang, query, exc)
        return sources

    def research(self, candidate: TopicCandidate) -> ResearchNotes:
        sources = self.collect_sources(candidate) if self.config.enabled else []
        logger.info("자료 %d건 수집: %s", len(sources), ", ".join(s.title for s in sources) or "-")
        if self.client is None:
            return ResearchNotes(summary=candidate.summary, truth_status=candidate.truth_status, sources=sources)

        if self.settings.gemini.use_google_search:
            try:
                grounded = self.client.generate(
                    f"다음 주제에 대해 신뢰할 수 있는 출처로 확인되는 사실만 한국어로 10줄 이내로 정리하라. "
                    f"확인되지 않은 내용은 '미확인'이라고 표시하라.\n주제: {candidate.topic}",
                    use_search=True,
                    temperature=0.2,
                )
                sources.append(SourceDoc(title="Google 검색 요약 (Gemini grounding)", url="", provider="gemini_search", extract=grounded[:3000]))
            except Exception as exc:
                logger.warning("Gemini Google Search grounding 실패: %s", exc)

        category = self.settings.category(candidate.category)
        sources_text ="\n\n".join(f"[source_index={i}] {s.title}\n{s.extract}" for i, s in enumerate(sources))
        prompt = load_prompt(
            "research_prompt",
            category_name=category.name if category else candidate.category,
            topic=candidate.topic,
            summary=candidate.summary,
            truth_status=candidate.truth_status,
            sources=sources_text or "(수집된 자료 없음 - 일반적으로 알려진 내용만 쓰고 불확실하면 UNCONFIRMED 로 표시)",
        )
        notes = self.client.generate_json(prompt, ResearchNotes, temperature=0.2)
        notes.sources = sources
        logger.info("Research completed: 사실 %d건, 미확인 %d건 (%s)", len(notes.facts), len(notes.open_questions), notes.truth_status)
        return notes
