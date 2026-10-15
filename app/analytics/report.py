"""성과 보고서: 영상별 성과 + 게시 후 경과 시점별 비교 + 제작 조건별 비교 + 원인 진단 + 개선 제안.

비교 원칙
- 절대 조회수만으로 판단하지 않는다. 조회수 비교는 "72시간 이후 첫 수집값"처럼 같은 시점 값만 쓰고, 그 값이 없는 영상은 제외한다.
- 제작 조건(도입 유형, 루프, Hook 효과 등) 비교는 같은 카테고리 중앙값 대비 상대값으로 한다 (카테고리 차이를 섞지 않음).
- 표본 수를 항상 함께 표시하고, min_samples 미만이면 "판단 불가"로 둔다.
- API 로 알 수 없는 노출수/시청 선택 비율은 Studio CSV 를 가져온 경우에만 사용하고 출처를 표시한다.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

from app.config.settings import Settings
from app.database.db import Database
from app.database.repositories import PerformanceRepository, ProductionMetadataRepository, uploaded_jobs

CHECKPOINT_LABELS = {24: "24h", 72: "72h", 168: "7d"}
GROUP_KEYS = {
    "category": "카테고리",
    "duration_bucket": "영상 길이",
    "hook_type": "도입 유형",
    "loop_ending_enabled": "루프 엔딩",
    "hook_effect_preset": "첫 3초 시각 효과",
    "title_strategy": "제목 전략",
    "visual_style": "시각 자료",
}


def _num(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _med(values: list[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return median(vals) if vals else None


def duration_bucket(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    if seconds < 45:
        return "<45s"
    if seconds < 55:
        return "45-55s"
    return ">=55s"


def confidence_label(n: int, min_samples: int) -> str:
    if n < min_samples:
        return "판단 불가(표본 부족)"
    if n < 10:
        return "낮음"
    return "보통"


def checkpoint_values(snapshots: list[dict], checkpoints: list[float]) -> dict[str, dict]:
    """각 기준 시간 이후 첫 수집값 (실제 경과 시간 함께). 정확히 그 시점의 값이 아님을 actual_hours 로 드러낸다."""
    out: dict[str, dict] = {}
    usable = [s for s in snapshots if s.get("hours_since_publish") is not None and s["collection_status"] in ("ok", "partial", "no_scope")]
    for cp in checkpoints:
        label = CHECKPOINT_LABELS.get(int(cp), f"{int(cp)}h")
        after = [s for s in usable if s["hours_since_publish"] >= cp]
        if not after:
            continue
        s = min(after, key=lambda x: x["hours_since_publish"])
        out[label] = {
            "actual_hours": s["hours_since_publish"],
            "views": s.get("views") if s.get("views") is not None else s.get("public_view_count"),
            "views_source": "analytics" if s.get("views") is not None else "data_api_public",
            "engaged_views": s.get("engaged_views"),
            "average_view_percentage": s.get("average_view_percentage"),
            "average_view_duration": s.get("average_view_duration"),
            "shares": s.get("shares"),
            "subscribers_gained": s.get("subscribers_gained"),
            "collected_at": s.get("collected_at"),
            "report_end_date": s.get("report_end_date"),
        }
    return out


def _per_1k(count: Any, views: Any) -> float | None:
    c, v = _num(count), _num(views)
    if c is None or not v:
        return None
    return c / v * 1000


def build_video_rows(db: Database, settings: Settings, days: int | None = None, now: datetime | None = None) -> list[dict]:
    cfg = settings.analytics
    tz = ZoneInfo(settings.app.timezone)
    now = now or datetime.now(tz)
    since = (now - timedelta(days=days or cfg.lookback_days)).isoformat(timespec="seconds")
    perf = PerformanceRepository(db)
    prod = {p["job_id"]: p for p in ProductionMetadataRepository(db, settings.app.timezone).all()}
    studio = perf.studio_metrics()
    rows: list[dict] = []
    for job in uploaded_jobs(db, since):
        vid = job.youtube_video_id or ""
        snaps = perf.snapshots(vid)
        good = [s for s in snaps if s["collection_status"] in ("ok", "partial", "no_scope")]
        latest = good[-1] if good else (snaps[-1] if snaps else None)
        meta = prod.get(job.id, {})
        state = job.state or {}
        duration = _num(meta.get("video_duration")) or _num((state.get("render") or {}).get("duration")) \
            or _num((latest or {}).get("duration_seconds"))
        views = None
        if latest:
            views = latest.get("views") if latest.get("views") is not None else latest.get("public_view_count")
        subs_net = None
        if latest and latest.get("subscribers_gained") is not None:
            subs_net = (latest.get("subscribers_gained") or 0) - (latest.get("subscribers_lost") or 0)
        hours = _num((latest or {}).get("hours_since_publish"))
        cps = checkpoint_values(snaps, cfg.checkpoints_hours)
        row = {
            "job_id": job.id,
            "video_id": vid,
            "url": job.youtube_url,
            "title": (state.get("metadata") or {}).get("title") or (state.get("topic") or {}).get("title", ""),
            "category": meta.get("topic_category") or (state.get("topic") or {}).get("category", "-"),
            "published_at": (latest or {}).get("published_at") or job.uploaded_at,
            "hours_since_publish": hours,
            "collection_status": (latest or {}).get("collection_status", "not_collected"),
            "collected_at": (latest or {}).get("collected_at"),
            "views": views,
            "views_source": "analytics" if latest and latest.get("views") is not None else ("data_api_public" if views is not None else None),
            "engaged_views": (latest or {}).get("engaged_views"),
            "average_view_duration": (latest or {}).get("average_view_duration"),
            "average_view_percentage": (latest or {}).get("average_view_percentage"),
            "likes": (latest or {}).get("likes") if (latest or {}).get("likes") is not None else (latest or {}).get("public_like_count"),
            "comments": (latest or {}).get("comments") if (latest or {}).get("comments") is not None else (latest or {}).get("public_comment_count"),
            "shares": (latest or {}).get("shares"),
            "subscribers_net": subs_net,
            "shares_per_1k": _per_1k((latest or {}).get("shares"), views),
            "subs_per_1k": _per_1k(subs_net, views),
            "checkpoints": cps,
            "views_72h": (cps.get("72h") or {}).get("views"),
            "duration": duration,
            "duration_bucket": duration_bucket(duration),
            "hook_type": meta.get("hook_type") or (state.get("draft") or {}).get("hook_type") or "unknown",
            "loop_ending_enabled": bool(meta.get("loop_ending_enabled")) if meta else bool((state.get("draft") or {}).get("loop")),
            "hook_effect_preset": meta.get("hook_effect_preset") or "none",
            "title_strategy": meta.get("title_strategy") or "unknown",
            "visual_style": meta.get("visual_style") or "unknown",
            "topic_score": meta.get("topic_score"),
            "studio": studio.get(vid, {}),
            "ready": bool(hours is not None and hours >= cfg.min_hours_for_judgement and views is not None),
        }
        rows.append(row)
    _add_relative(rows, cfg.min_samples)
    for row in rows:
        row["diagnosis"] = diagnose(row, rows, cfg.min_samples)
    return rows


def _add_relative(rows: list[dict], min_samples: int) -> None:
    """같은 카테고리(표본 부족 시 채널 전체) 중앙값 대비 상대 성과."""
    ready = [r for r in rows if r["ready"]]
    channel_avp = _med([_num(r["average_view_percentage"]) for r in ready])
    channel_views = _med([_num(r["views_72h"]) for r in ready])
    for r in rows:
        peers = [p for p in ready if p["category"] == r["category"]]
        basis = "category" if len(peers) >= min_samples else "channel"
        avp_ref = _med([_num(p["average_view_percentage"]) for p in peers]) if basis == "category" else channel_avp
        views_ref = _med([_num(p["views_72h"]) for p in peers]) if basis == "category" else channel_views
        avp = _num(r["average_view_percentage"])
        v72 = _num(r["views_72h"])
        r["relative_basis"] = basis
        r["rel_avp"] = (avp - avp_ref) if (r["ready"] and avp is not None and avp_ref is not None) else None
        r["rel_views_72h"] = (v72 / views_ref) if (v72 is not None and views_ref) else None


def diagnose(row: dict, rows: list[dict], min_samples: int) -> list[dict]:
    """설명 가능한 규칙으로 원인 후보를 고른다. 확인할 수 없는 값(노출, 이탈 시점, CTR)은 만들지 않는다."""
    if not row["ready"]:
        hours = row["hours_since_publish"]
        why = "성과 미수집" if hours is None else f"게시 후 {hours:.0f}시간 (판단 보류)"
        return [{"code": "insufficient_data", "label": "데이터 부족", "detail": why,
                 "suggestion": "다음 수집까지 기다린 뒤 판단"}]
    ready = [r for r in rows if r["ready"]]
    if len(ready) < min_samples:
        return [{"code": "insufficient_peers", "label": "비교 표본 부족",
                 "detail": f"비교 가능한 영상 {len(ready)}개 (< {min_samples})", "suggestion": "기존 기준 유지"}]
    out: list[dict] = []
    med_views = _med([_num(r["views"]) for r in ready])
    med_avp = _med([_num(r["average_view_percentage"]) for r in ready])
    med_share = _med([r["shares_per_1k"] for r in ready])
    med_subs = _med([r["subs_per_1k"] for r in ready])
    studio = row.get("studio") or {}
    shown = (studio.get("shown_in_feed") or {}).get("value")
    viewed = (studio.get("viewed_rate") or {}).get("value")
    med_shown = _med([((r.get("studio") or {}).get("shown_in_feed") or {}).get("value") for r in ready])
    med_viewed = _med([((r.get("studio") or {}).get("viewed_rate") or {}).get("value") for r in ready])
    views = _num(row["views"])
    avp = _num(row["average_view_percentage"])

    if shown is not None and med_shown and shown < 0.5 * med_shown:
        out.append({"code": "low_exposure", "label": "낮은 노출", "detail": f"피드 노출 {shown:.0f} (중앙값 {med_shown:.0f}, 출처 {studio['shown_in_feed']['source']})",
                     "suggestion": "주제 선택(검색/관심 신호)과 업로드 시간 점검. 한 영상만으로 결론 내리지 않음"})
    elif viewed is not None and med_viewed and viewed < 0.8 * med_viewed:
        out.append({"code": "low_choice", "label": "낮은 시청 선택 비율", "detail": f"시청 선택 {viewed:.1f}% (중앙값 {med_viewed:.1f}%)",
                     "suggestion": "첫 장면·첫 문장(Hook)과 제목이 실제 내용과 맞는지 점검"})
    elif views is not None and med_views and views < 0.5 * med_views:
        out.append({"code": "low_views", "label": "조회수 낮음", "detail": f"{views:.0f}회 (채널 중앙값 {med_views:.0f})"
                     " - 노출 부족인지 시청 선택 부족인지는 API 로 구분 불가 (Studio CSV 가져오기 필요)",
                     "suggestion": "Studio 의 피드 노출/시청 선택 지표를 가져와 원인 구분"})
    if avp is not None and med_avp and avp < 0.85 * med_avp:
        out.append({"code": "low_retention", "label": "시청 지속 낮음", "detail": f"평균 시청 비율 {avp:.1f}% (중앙값 {med_avp:.1f}%)",
                     "suggestion": "배경 설명이 긴지, 장면마다 새 정보가 있는지, 결말이 늦게 나오는지 점검"})
    elif avp is not None and med_avp and avp >= med_avp:
        sp, su = row["shares_per_1k"], row["subs_per_1k"]
        if sp is not None and su is not None and med_share and med_subs and sp < 0.5 * med_share and su < 0.5 * med_subs:
            out.append({"code": "low_conversion", "label": "지속 양호, 공유·구독 전환 낮음",
                        "detail": f"공유 {sp:.1f}/천회, 구독 {su:.1f}/천회",
                        "suggestion": "결말에서 질문 회수/댓글 유도 문장, 채널 정체성 문구 점검"})
    if row["shares_per_1k"] is not None and med_share and row["shares_per_1k"] >= 1.5 * med_share:
        out.append({"code": "high_share", "label": "공유 상대적으로 높음", "detail": f"공유 {row['shares_per_1k']:.1f}/천회 (중앙값 {med_share:.1f})",
                    "suggestion": "이 영상의 주제·정보 구성을 후속 주제 후보에 반영"})
    if not out:
        out.append({"code": "normal", "label": "채널 평균 범위", "detail": "", "suggestion": "기존 기준 유지"})
    return out


def group_stats(rows: list[dict], key: str, min_samples: int) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for r in rows:
        if r["ready"]:
            groups.setdefault(str(r.get(key)), []).append(r)
    out = []
    for value, members in sorted(groups.items()):
        n = len(members)
        out.append({
            "value": value, "n": n, "confidence": confidence_label(n, min_samples),
            "n_views_72h": sum(1 for m in members if m["views_72h"] is not None),
            "median_views_72h": _med([_num(m["views_72h"]) for m in members]),
            "median_avp": _med([_num(m["average_view_percentage"]) for m in members]),
            "mean_rel_avp": (sum(m["rel_avp"] for m in members if m["rel_avp"] is not None)
                             / max(sum(1 for m in members if m["rel_avp"] is not None), 1))
            if any(m["rel_avp"] is not None for m in members) else None,
            "median_rel_views_72h": _med([m["rel_views_72h"] for m in members]),
            "median_shares_per_1k": _med([m["shares_per_1k"] for m in members]),
            "median_subs_per_1k": _med([m["subs_per_1k"] for m in members]),
        })
    return out


def suggestions(groups: dict[str, list[dict]], rows: list[dict], min_samples: int) -> list[str]:
    out: list[str] = []
    ready = [r for r in rows if r["ready"]]
    if len(ready) < min_samples:
        return [f"판단 가능한 영상이 {len(ready)}개뿐입니다 (최소 {min_samples}개). 데이터가 쌓일 때까지 기존 제작 기준을 유지합니다."]
    for key, stats in groups.items():
        if key == "category":
            continue
        enough = [s for s in stats if s["n"] >= min_samples and s["mean_rel_avp"] is not None]
        if len(enough) < 2:
            continue
        best = max(enough, key=lambda s: s["mean_rel_avp"])
        worst = min(enough, key=lambda s: s["mean_rel_avp"])
        gap = best["mean_rel_avp"] - worst["mean_rel_avp"]
        if gap >= 5:
            out.append(
                f"{GROUP_KEYS[key]}: '{best['value']}' 가 '{worst['value']}' 보다 같은 카테고리 대비 평균 시청 비율이 {gap:.1f}%p 높음"
                f" (n={best['n']} vs {worst['n']}, 신뢰도 {best['confidence']}/{worst['confidence']}) → 점진적으로 비중 확대 검토"
            )
    codes: dict[str, int] = {}
    for r in ready:
        for d in r["diagnosis"]:
            codes[d["code"]] = codes.get(d["code"], 0) + 1
    if codes.get("low_retention", 0) >= max(2, len(ready) // 3):
        out.append(f"시청 지속이 낮은 영상이 {codes['low_retention']}개 → 배경 설명을 줄이고 장면마다 새 정보를 넣는 대본 원칙 강화")
    if codes.get("low_views", 0) >= 2:
        out.append("조회수가 낮은 영상의 원인(노출 vs 시청 선택)은 API 로 구분되지 않음 → Studio CSV 를 주기적으로 가져오기 권장")
    if codes.get("high_share"):
        out.append("공유가 높은 영상의 주제·구성이 다음 주제 프롬프트에 참고로 들어갑니다")
    if not out:
        out.append("유의미한 차이(표본 충분 + 5%p 이상)가 아직 없습니다 → 기존 기준 유지")
    return out


def build_report(db: Database, settings: Settings, days: int | None = None, now: datetime | None = None) -> dict:
    cfg = settings.analytics
    tz = ZoneInfo(settings.app.timezone)
    now = now or datetime.now(tz)
    rows = build_video_rows(db, settings, days, now)
    groups = {key: group_stats(rows, key, cfg.min_samples) for key in GROUP_KEYS}
    ready = [r for r in rows if r["ready"]]
    unknown = [
        "피드 노출수(Shown in feed) / 시청함 대 스와이프 비율: YouTube Analytics API 미제공 → `analytics import-csv` 로 Studio 내보내기 파일을 가져온 경우에만 사용",
        "노출 클릭률(CTR) / 이탈 시점: API 로 확인하지 않음 (추정값을 만들지 않음)",
    ]
    if len(ready) < cfg.min_samples:
        unknown.insert(0, f"제작 조건별 비교: 판단 가능한 영상 {len(ready)}개 < 최소 {cfg.min_samples}개")
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "period_days": days or cfg.lookback_days,
        "videos": rows,
        "ready_count": len(ready),
        "groups": groups,
        "top_share_subs": sorted(
            [r for r in ready if r["shares_per_1k"] is not None or r["subs_per_1k"] is not None],
            key=lambda r: (r["shares_per_1k"] or 0) + (r["subs_per_1k"] or 0), reverse=True,
        )[:5],
        "undecidable": unknown,
        "suggestions": suggestions(groups, rows, cfg.min_samples),
    }


# ------------------------------------------------------------------ output
def _fmt(v: Any, digits: int = 1, suffix: str = "") -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.{digits}f}{suffix}"
    return f"{v}{suffix}"


def format_report(report: dict) -> str:
    lines = [f"📊 성과 보고서 ({report['generated_at']}, 최근 {report['period_days']}일, 판단 가능 {report['ready_count']}개)", ""]
    lines.append("■ 최근 업로드 영상")
    lines.append(f"{'VIDEO':<12} {'경과':>6} {'조회':>7} {'유효조회':>7} {'시청%':>6} {'평균초':>6} {'공유/천':>7} {'구독/천':>7}  카테고리 / 제목")
    for r in report["videos"]:
        lines.append(
            f"{r['video_id']:<12} {_fmt(r['hours_since_publish'], 0, 'h'):>6} {_fmt(r['views']):>7} {_fmt(r['engaged_views']):>7}"
            f" {_fmt(r['average_view_percentage']):>6} {_fmt(r['average_view_duration']):>6} {_fmt(r['shares_per_1k']):>7}"
            f" {_fmt(r['subs_per_1k']):>7}  [{r['category']}] {r['title'][:40]}"
        )
        cps = ", ".join(f"{k}: {_fmt(v['views'])}회(실제 {v['actual_hours']:.0f}h)" for k, v in r["checkpoints"].items())
        if cps:
            lines.append(f"{'':<12} 시점별 조회: {cps}")
        diag = "; ".join(f"{d['label']}" + (f" ({d['detail']})" if d["detail"] else "") for d in r["diagnosis"])
        lines.append(f"{'':<12} 진단: {diag}  [{r['collection_status']}, 조회수 출처 {r['views_source'] or '-'}]")
    for key, label in GROUP_KEYS.items():
        stats = report["groups"].get(key) or []
        if not stats:
            continue
        lines += ["", f"■ {label}별 (판단 가능 영상만, 상대값 = 같은 카테고리 중앙값 대비)"]
        for s in stats:
            lines.append(
                f"  {s['value']:<16} n={s['n']:<3} 72h조회 중앙값 {_fmt(s['median_views_72h'], 0)} (n={s['n_views_72h']})"
                f" 시청% {_fmt(s['median_avp'])} 상대시청 {_fmt(s['mean_rel_avp'], 1, '%p')}"
                f" 공유/천 {_fmt(s['median_shares_per_1k'])} 구독/천 {_fmt(s['median_subs_per_1k'])}  신뢰도: {s['confidence']}"
            )
    if report["top_share_subs"]:
        lines += ["", "■ 공유·구독 증가가 상대적으로 좋았던 영상"]
        for r in report["top_share_subs"]:
            lines.append(f"  {r['video_id']} 공유/천 {_fmt(r['shares_per_1k'])} 구독/천 {_fmt(r['subs_per_1k'])} — {r['title'][:40]}")
    lines += ["", "■ 아직 판단할 수 없는 항목"] + [f"  - {u}" for u in report["undecidable"]]
    lines += ["", "■ 다음 제작 개선 제안"] + [f"  - {s}" for s in report["suggestions"]]
    return "\n".join(lines)


CSV_FIELDS = [
    "job_id", "video_id", "url", "title", "category", "published_at", "hours_since_publish", "collection_status",
    "views", "views_source", "views_72h", "engaged_views", "average_view_duration", "average_view_percentage",
    "likes", "comments", "shares", "subscribers_net", "shares_per_1k", "subs_per_1k", "rel_avp", "rel_views_72h",
    "relative_basis", "duration", "duration_bucket", "hook_type", "loop_ending_enabled", "hook_effect_preset",
    "title_strategy", "visual_style", "topic_score", "ready", "diagnosis",
]


def export_report(report: dict, path: Path, fmt: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "json":
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    else:
        with open(path, "w", newline="", encoding="utf-8-sig") as fp:
            writer = csv.DictWriter(fp, fieldnames=CSV_FIELDS, extrasaction="ignore")
            writer.writeheader()
            for r in report["videos"]:
                writer.writerow({**r, "diagnosis": "; ".join(d["label"] for d in r["diagnosis"])})
    return path
