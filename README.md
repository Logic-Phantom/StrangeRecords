# 기묘한 기록 (StrangeRecords) — AI YouTube Shorts 자동 제작·업로드 시스템

매일 **새로운 주제 선정 → 자료 조사 → 대본 → 음성 → 시각자료 → 자막 → 편집 → 품질 검사 → YouTube 업로드(즉시 공개) → 이력 저장** 을 자동으로 수행하는 Python 프로젝트입니다. (GitHub Actions 로 매일 22:07 KST 실행 — PC 를 켜 두지 않아도 됨)

2026-10-15 부터 **성장 엔진**이 추가되었습니다: 업로드한 영상의 YouTube 성과를 수집·분석하고, 외부 관심 신호(위키백과 조회수, YouTube)와 함께 다음 영상의 주제 선정·대본·편집에 반영합니다 (→ [14절](#14-성장-엔진-트렌드--성과-분석--자동-개선)).

2026-10-16 부터 **수익화 준비 기능**이 추가되었습니다: 공개 영상 주제 맞춤 댓글(등록/고정 상태 분리), 채널 브랜딩 자산·일관성 점검, 영상별 전개 형식·정보 카드, 사실성·출처·장면 일치·광고 적합성 검수, Edge TTS 권리 상태 표시 (→ [15절](#15-수익화-준비-댓글--브랜딩--검수--권리)).

2026-10-17 부터 **이미지 → 영상 변환**이 추가되었습니다: 검증된 무료 Image-to-Video API 로 가능한 장면만 AI 영상으로 만들고, 나머지(무료 한도 도달·미검증·실패)는 기존 AI 이미지에 FFmpeg 움직임 효과(안개·먼지·조명·패럴랙스 등)로 완성합니다. 현재 공식 문서로 무료 조건을 모두 확인한 API 가 없어 **외부 호출은 0회**이고 FFmpeg 움직임 효과만 동작합니다 (→ [16절](#16-이미지--영상-무료-image-to-video--ffmpeg-움직임-효과)).

- AI 엔진: **Google Gemini API 하나만 사용** (기본 `gemini-3.5-flash`, 과부하 시 자동 대체 모델 전환, 모델명은 `config.yaml` 한 곳에서 관리)
- 음성: Edge TTS (무료) · 시각자료: **대본 기반 AI 장면 이미지: Cloudflare Workers AI / Hugging Face SDXL** (Gemini 이미지는 무료 티어 한도 0 이라 비활성) → Pexels / Pixabay (무료 API) · 자막: faster-whisper (로컬) · 편집: FFmpeg · DB: SQLite
- 출력: **1080x1920 · 9:16 · 30fps · H.264 · AAC · MP4**, 한국어 40~60초, **한국어 + 영어 자막 / 한국어 + 영어 설명란** (해외 시청자용)

---

## 1. 파이프라인

```text
[0] (보조) 이전 영상 성과 수집: YouTube Analytics + 공개 통계 → SQLite
[1] DB 기존 주제 조회 + 카테고리 선택(순환 + 성과 반영) → [1-1] (보조) 채널 관련 트렌드 힌트 수집
→ [2] Gemini 주제 후보 → [3] 중복 검사(키워드 + 텍스트 유사도 + Gemini 의미 판단)
→ [3-1] 후보별 외부 신호(위키백과 조회수/증가 속도, YouTube 과밀도) + 채널 성과 → 점수 → [4] 최종 주제 + 선정 근거 기록
→ [5] 자료 조사(Wikipedia + Gemini 사실/추측 구분) → [6] Gemini 대본(Hook, 전개 단계, 자연스러운 루프 엔딩)
→ [7] 대본 검증(길이/금지표현/사실여부 고지 + 반복/단정/최근 대본 유사도/루프 품질) → [8] Gemini Scene 분할 + 영어 검색어
→ [9] Edge TTS (Scene 별 합성 → 실제 길이 확정, 길면 속도 자동 조정)
→ [10] AI 장면 이미지(Scene 별 visual_prompt: Gemini → Hugging Face) → Pexels → Pixabay → 로컬 → 자체 그래픽 순으로 자료 수집
→ [10-1] 이미지→영상(motion): 검증된 무료 Image-to-Video API 로 가능한 장면만 AI 영상, 나머지는 FFmpeg 움직임 효과 (실패해도 계속)
→ [11] faster-whisper 단어 타이밍 + 대본 텍스트 정렬 → Shorts 자막
→ [12] FFmpeg: 9:16 crop / Ken Burns / 첫 장면 Hook 효과 / 전환 / 자막 / BGM 덕킹 / 효과음 / -14 LUFS
→ [13] 품질 검사(실패 시 업로드 금지) → [14] Gemini 제목 후보 3개·설명·해시태그 (+ 출처 자동 표기) → 제목 평가/선택
→ [14-1] 콘텐츠 검수(출처/단정/독창성/라이선스/AI 고지, critical 이면 업로드 금지 → review_needed)
→ [15] YouTube 업로드 → [16] 공개(즉시 또는 12:00 예약) → [16-1] (보조) 공개 확인 후 주제 맞춤 댓글 1개 (comments.enabled)
→ [17] SQLite + JSON 이력 + 제작 조건(production_metadata) 저장
```

[0]·[1-1]·[3-1] 은 보조 기능입니다. 외부 API/권한이 없거나 실패해도 기존 방식(Gemini 후보 + 중복 검사 + 카테고리 순환)으로 제작·업로드합니다.

음성을 시각자료보다 먼저 만드는 이유: Scene 별 실제 음성 길이를 알아야 영상 클립 길이를 정확히 고를 수 있기 때문입니다.

모든 단계 결과는 `jobs.state_json` 과 `data/`, `output/` 에 저장되므로, 실패하면 `retry` 로 **실패한 단계부터** 다시 실행합니다.

## 2. 설치

### Windows (운영 환경 권장)

1. [Python 3.11+](https://www.python.org/downloads/) 설치 (설치 화면에서 *Add python.exe to PATH* 체크)
2. 프로젝트 폴더에서 `setup_windows.bat` 실행 → 가상환경, 패키지, DB, 기본 BGM/효과음 생성
3. `.env` 에 API Key 입력 (아래 3절)
4. `run_test.bat --offline` → API Key 없이 샘플 대본으로 영상 제작 확인
5. `run_test.bat` → Gemini 로 실제 주제/대본 제작 확인

### macOS / Linux

```bash
bash scripts/setup_macos.sh
.venv/bin/python -m app.main test --offline
```

> **FFmpeg**: 별도 설치가 필요 없습니다. PATH 에 `ffmpeg` 가 없으면 `imageio-ffmpeg` 패키지에 포함된 FFmpeg(libx264/AAC 포함)를 자동 사용합니다. ffprobe 가 없으면 PyAV 로 영상을 분석합니다.
>
> **Whisper 모델**: 첫 실행 시 `models/` 에 faster-whisper `small` 모델(약 460MB)을 내려받습니다.

## 3. API Key (`.env`)

| 항목 | 필수 | 비용 | 발급 |
|---|---|---|---|
| `GEMINI_API_KEY` | ✅ | 무료 티어 (한도/모델은 수시 변경) | https://aistudio.google.com/apikey |
| `PEXELS_API_KEY` | 권장 | 무료 | https://www.pexels.com/api/ |
| `PIXABAY_API_KEY` | 권장 | 무료 | https://pixabay.com/api/docs/ |
| `CLOUDFLARE_ACCOUNT_ID` / `CLOUDFLARE_API_TOKEN` | 권장 | 무료 (하루 10,000 Neurons, SDXL 0원) | https://dash.cloudflare.com → Workers AI → REST API 사용 → Account ID 복사 + "Workers AI" API 토큰 생성 |
| `HF_API_KEY` | 선택 | 크레딧 필요 | https://huggingface.co/settings/tokens → Fine-grained 토큰, **"Make calls to Inference Providers"** 권한 체크 |
| `YOUTUBE_CLIENT_ID` / `YOUTUBE_CLIENT_SECRET` | 업로드 시 | 무료 (일일 할당량 10,000 units) | Google Cloud Console → YouTube Data API v3 사용 설정 → OAuth 클라이언트 ID(**데스크톱 앱**) |
| `YOUTUBE_API_KEY` | 선택 | 무료 (Data API 할당량) | 트렌드 신호 조회용. 없으면 `token.json`(youtube.readonly)으로 조회 |
| YouTube Analytics 권한 | 성과 수집 시 | 무료 | Key 없음. Cloud Console 에서 **YouTube Analytics API** 사용 설정 + `auth` 재인증 (14.2절) |
| `GOOGLE_TRENDS_API_KEY` | - | - | 공식 API 는 **알파(승인자만)** → 승인 전에는 사용 안 함 |
| `ZHIPU_API_KEY` | - | 미검증 | 智谱 CogVideoX-Flash(이미지→영상). 무료 조건 미검증이라 **키를 넣어도 호출하지 않음** (16절) |
| Edge TTS | - | 무료, Key 불필요 | - |
| Wikipedia / Wikimedia Pageviews | - | 무료, Key 불필요 | 자료 조사 + 관심도 신호 |

- 시각자료는 Scene 마다 대본 내용대로 그린 AI 이미지를 사용합니다. **2026-10-13 부터 Gemini 이미지(`assets.ai_image.enabled: false`)는 쓰지 않고 Cloudflare → Hugging Face 순으로 그립니다.** 아래는 Gemini 이미지를 다시 켤 때의 설명입니다 (`assets.ai_image`). 같은 `GEMINI_API_KEY` 를 쓰지만, 무료 티어에서 이미지 모델이 막혀 있으면 [Google AI Studio](https://aistudio.google.com/) 에서 **결제(Billing) 연결**이 필요합니다 (장당 약 $0.04, 영상 1개 6~9장).
- Gemini 이미지가 실패(무료 티어 `limit: 0`, 429 RESOURCE_EXHAUSTED, 권한 오류)하면 **같은 Scene 에서 바로 Cloudflare Workers AI** (`assets.cf_image`, `@cf/stabilityai/stable-diffusion-xl-base-1.0`, 768x1344, 무료)로 같은 프롬프트를 그립니다. 인증 오류·무료 한도 초과(429)면 이번 작업에서 제외하고 다음 provider 로 넘어갑니다.
- Cloudflare 도 실패하면 **Hugging Face Inference API** (`assets.hf_image`, 기본 `stabilityai/stable-diffusion-xl-base-1.0`, 768x1344)로 같은 프롬프트를 그립니다. `provider: "auto"` 라 그 모델을 서비스 중인 Inference Provider(현재 SDXL 은 fal-ai)로 자동 라우팅되며, 토큰 권한 없음(403)·크레딧 소진(402)이면 이번 작업에서 제외하고 Pexels 로 넘어갑니다.
- **AI 이미지 필수 (`assets.require_ai_image: true`, 2026-10-13~)**: 모든 Scene 을 대본(자막) 내용대로 그린 AI 이미지로 채웁니다. 어두운 자체 그래픽·스톡 자료로 대체하지 않습니다.
  - Cloudflare 안에서 모델 전환: SDXL base → SDXL-Lightning → FLUX.1 schnell (`cf_image.fallback_models`, 타임아웃·5xx·모델 거부 시 같은 Scene 에서 바로 다음 모델)
  - 그래도 빠진 Scene 은 30초 / 90초 / 180초 쉬었다가 다시 그림 (`assets.ai_retry_waits`)
  - 끝내 빠지면 업로드하지 않고 `assets` 단계 실패 → 클라우드가 5분/10분 뒤 그 단계부터 재실행. 이미 그린 Scene 은 `data/sources/<job>_assets_partial.json` 에서 재사용하고 빠진 Scene 만 그림
  - AI 이미지 provider(Cloudflare/HF 키)가 하나도 없으면 바로 실패합니다. `test --offline` 은 예외(자체 그래픽 사용)
  - `require_ai_image: false` 로 두면 예전처럼 Pexels/Pixabay → 로컬 → 자체 제작 그래픽으로 대체해 제작을 멈추지 않습니다.
- Gemini 무료 모델/한도가 바뀌면 `python -m app.main models` 로 사용 가능한 모델을 확인하고 `config.yaml` 의 `gemini.model` (또는 `.env` 의 `GEMINI_MODEL`)만 바꾸면 됩니다.
- 기본 모델이 과부하(503)/한도 초과(429)로 3회 실패하면 `gemini.fallback_models` 순서(`gemini-flash-latest` → `gemini-2.5-flash` → `gemini-flash-lite-latest`)로 자동 전환하고, 그 실행 동안은 전환된 모델을 유지합니다.
- 하루 Gemini 호출 수: 주제 1 + 중복 판단 1 + 자료 정리 1 + 대본 1 + Scene 1 + 메타데이터 1 ≈ **6회** (+ 검증 실패 시 재요청).

## 4. 명령어

```bash
python -m app.main today        # 오늘 영상 제작 (production 모드면 업로드 + 12:00 예약 공개)
python -m app.main test         # 영상까지만 제작 (업로드 안 함)
python -m app.main test --offline   # Gemini 없이 샘플 대본(tests/fixtures)으로 제작
python -m app.main topic        # 주제 후보 + 중복 검사 + 점수/선정 근거 미리보기 (주제 저장 안 함, --no-trends: 기존 방식)
python -m app.main trends --category legendary_creatures --keyword "크라켄"   # 트렌드 신호 미리보기
python -m app.main analytics sync              # 업로드 영상 성과 수집 (--days 30, --video ID)
python -m app.main analytics report            # 성과 보고서 (--days 30, --format json|csv, --output 경로)
python -m app.main analytics status            # token.json 의 Analytics 권한 점검
python -m app.main analytics import-csv studio.csv   # Studio 전용 지표(피드 노출, 시청 선택 비율) 가져오기
python -m app.main history      # 제작 이력
python -m app.main retry [JOB]  # 실패 작업을 실패한 단계부터 재시도
python -m app.main remake [JOB] --from-step voice   # 완료된 작업을 그 단계부터 다시 제작 + 업로드 (영상 삭제 후 다시 올릴 때)
python -m app.main upload [JOB] # 제작 완료(rendered) 영상 업로드
python -m app.main upload --privacy private   # 이번 업로드만 비공개로 (scheduled | public 도 가능)
python -m app.main upload JOB --approve-review   # 콘텐츠 검수 보류(review_needed) 작업을 사람이 확인 후 업로드
python -m app.main comments status [--verify]   # 자동 댓글 등록 상태 + 고정 상태 (따로 표시)
python -m app.main comments retry [JOB]   # 대기(예약/비공개)·실패·권한 부족 댓글 재시도 (같은 본문, 중복 확인 후)
python -m app.main comments preview [JOB] [--offline]   # 댓글 본문 미리보기 (저장/등록 안 함)
python -m app.main comments pins          # 고정 처리가 필요한 댓글 (API 미지원 → 수동)
python -m app.main comments mark-pinned VIDEO_OR_JOB [--note ..] [--undo]   # 사람이 고정했다고 기록
python -m app.main comments guide         # 수동 고정 운영 가이드 → data/reports/pin_guide.md
python -m app.main review [JOB]           # 콘텐츠 검수 결과 (사실성·출처·장면·라이선스·광고 적합성)
python -m app.main rights                 # Edge TTS 상업적 이용 권리 상태
python -m app.main branding check         # 홈페이지·약관·OAuth 범위·브랜드 자산 일관성 점검 (API 호출 없음)
python -m app.main branding assets        # 아이콘/배너/워터마크 PNG 생성 (채널 적용 안 함)
python -m app.main branding status        # 현재 채널 브랜딩 조회 (읽기)
python -m app.main branding apply [--yes] [--banner]   # 기본은 변경 미리보기, --yes 일 때만 채널 수정
python -m app.main auth         # YouTube OAuth 인증 (업로드 + 읽기 + Analytics 읽기 + 댓글(force-ssl), token.json 저장. --no-analytics / --no-comments)
python -m app.main setup        # 폴더/DB/기본 BGM·효과음 생성
python -m app.main doctor       # 환경 점검
python -m app.main models       # 사용 가능한 Gemini 모델 목록
python -m app.main schedule     # (보조) Python 스케줄러
```

옵션: `today --force` (오늘 이미 업로드했어도 제작), `today --no-upload`, `--topic "주제"`, `--category unsolved_mystery`

## 5. 운영 모드

`app/config/config.yaml`

```yaml
app:
  mode: "production"    # 전체 파이프라인 + 업로드 (매일 자동 실행에 필요)
  # mode: "development" # 주제~품질검사까지 수행, YouTube 업로드 X  →  `upload` 명령으로 나중에 업로드 가능
youtube:
  publish_mode: "public"      # public(업로드 즉시 공개) | scheduled(publish_time 예약 공개) | private
  publish_time: "12:00"
  late_policy: "public_now"   # 완성 시점이 12:00 이후면: public_now | next_day | private
```

`today` 는 **하루 1개 정책**을 지킵니다: 오늘 이미 업로드했으면 종료, 오늘 실패한 작업이 있으면 새 주제 대신 이어서 실행, 업로드 대기 영상이 있으면 업로드합니다.

## 6. YouTube 연결

1. Google Cloud Console 에서 프로젝트 생성 → **YouTube Data API v3** 사용 설정
2. OAuth 동의 화면 구성(외부, 테스트 사용자에 본인 계정 추가) → OAuth 클라이언트 ID(**데스크톱 앱**) 생성
3. `.env` 에 `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET` 입력 (또는 다운로드한 JSON 을 `credentials.json` 으로 저장)
4. `python -m app.main auth` → 브라우저 로그인/승인 → `token.json` 저장 (이후 자동 갱신)
5. 먼저 `python -m app.main upload --privacy private` 로 비공개 업로드 테스트 → 확인 후 `scheduled`

**로그인 오류 해결**

| 화면 메시지 | 원인 / 해결 |
|---|---|
| `액세스 차단됨: (앱)은(는) Google 인증 절차를 완료하지 않았습니다` | OAuth 동의 화면이 "테스트" 상태인데 로그인 계정이 테스트 사용자가 아님 → [Google 인증 플랫폼 → 대상](https://console.cloud.google.com/auth/audience) → **테스트 사용자 추가** 후 `auth` 다시 실행 |
| `Google에서 확인하지 않은 앱` | 테스트 상태에서는 정상 → **고급 → (앱)(으)로 이동** |
| `redirect_uri_mismatch` | OAuth 클라이언트가 "웹 애플리케이션" 유형 → **데스크톱 앱** 유형으로 다시 생성 |
| `YouTube Data API v3 has not been used` | [API 라이브러리](https://console.cloud.google.com/apis/library/youtube.googleapis.com)에서 사용 설정 |

> ⚠️ **API 감사(Audit) 전 제한**: YouTube 정책상 미인증 API 프로젝트로 업로드한 영상은 비공개로 잠길 수 있습니다. 현재 프로젝트는 2026-10-10 클라우드 업로드가 **실제 public 으로 확인**되었지만, 업로드 직후 상태를 조회해 private 로 잠기면 로그에 [감사 신청](https://support.google.com/youtube/contact/yt_api_form) 안내 경고를 남깁니다.
>
> ⚠️ OAuth 동의 화면이 "테스트" 상태면 refresh token 이 7일 후 만료됩니다 → 2026-10-10 **프로덕션으로 게시 완료**, 게시 후 다시 받은 토큰 사용 중.
> 프로덕션 게시에 필요한 홈페이지/개인정보처리방침/약관은 GitHub Pages(`docs/`)로 제공: [홈](https://logic-phantom.github.io/StrangeRecords/) · [개인정보처리방침](https://logic-phantom.github.io/StrangeRecords/privacy.html) · [약관](https://logic-phantom.github.io/StrangeRecords/terms.html) (승인된 도메인 `logic-phantom.github.io`, 앱 로고는 올리지 않음 → 올리면 브랜드 인증 심사 필요)
>
> 업로드 시 `containsSyntheticMedia`(AI 생성 콘텐츠 고지)를 설정하고, 설명란에 AI 활용 사실을 자동으로 표기합니다.

## 7. 자동 실행

### GitHub Actions (기본, PC 꺼져 있어도 동작)

`.github/workflows/daily-shorts.yml` 이 매일 **22:07 KST** (GitHub 혼잡 시 수십 분 지연 가능) GitHub 서버(Ubuntu 24.04)에서 `today` 를 실행해 제작 → 업로드합니다. 1회 실행 약 5분 (공개 저장소라 Actions 무료).

1. 저장소 **Settings → Secrets and variables → Actions → New repository secret** 에 등록
   | Secret | 값 |
   |---|---|
   | `GEMINI_API_KEY` | Gemini API Key (필수) |
   | `YOUTUBE_TOKEN_JSON` | 로컬 `token.json` 파일 내용 전체 (필수, `Get-Content token.json -Raw \| Set-Clipboard` 로 복사) |
   | `CLOUDFLARE_ACCOUNT_ID` / `CLOUDFLARE_API_TOKEN` | Cloudflare Workers AI (권장, 무료 AI 이미지) |
   | `HF_API_KEY` | Hugging Face 토큰 (선택, 크레딧 필요) |
   | `PEXELS_API_KEY` / `PIXABAY_API_KEY` | 선택 |
   | `STATE_DB_KEY` | **권장** — `pipeline-state` 브랜치의 DB 를 AES-256 으로 암호화하는 비밀번호 (아래 주의) |
   | `YOUTUBE_API_KEY` | 선택 — 트렌드 신호용 (없으면 `YOUTUBE_TOKEN_JSON` 사용) |
   | `GOOGLE_TRENDS_API_KEY` | 선택 — Google Trends 알파 승인 시에만 |
   | `ZHIPU_API_KEY` | 선택 — 이미지→영상 CogVideoX-Flash (현재 미검증이라 넣어도 호출 안 함, 16절) |
2. **Actions → Daily Shorts → Run workflow** 로 수동 실행해 확인 (`test` = 제작만, `today` = 제작 + 업로드, `remake` = 완료된 작업(`job`, 비우면 오늘 것)을 `from_step` 단계부터 다시 제작 + 업로드)
- 주제 이력/하루 1개 정책/성과 이력 DB(`data/database.sqlite`)는 실행마다 `pipeline-state` 브랜치에 저장되어 다음 실행이 이어 씁니다.
  - ⚠️ **공개 저장소**이므로 이 DB 는 누구나 내려받을 수 있습니다. 성과 수집을 켜면 평균 시청 비율·구독 증감 같은 **비공개 Analytics 지표**가 DB 에 들어가므로 `STATE_DB_KEY` Secret 을 등록하세요 → 다음 실행부터 `database.sqlite.enc`(AES-256, openssl)로 저장하고 평문 파일은 브랜치에서 지웁니다 (이전 커밋 이력의 평문 DB 에는 성과 데이터가 없음). Secret 을 지우면 다시 평문으로 저장합니다. 키를 잃어버리면 DB 를 복원할 수 없습니다.
- 실행 순서: DB 복원 → **성과 수집(`analytics sync`, 실패해도 계속)** → `today`(트렌드·성과를 반영한 주제 선정 → 제작 → 검수 → 업로드) → DB 저장. 성과 수치는 Actions 로그에 출력하지 않습니다(건수만).
- 실패하면 같은 서버에서 5분/10분 뒤 실패한 단계부터 최대 3회 실행합니다. 완성 영상은 Actions 실행 화면의 Artifacts 에 3일 보관됩니다.
- YouTube 토큰을 다시 받아야 할 때(권한 철회, 비밀번호 변경 등): 로컬에서 `token.json` 삭제 → `auth` → 새 `token.json` 내용으로 `YOUTUBE_TOKEN_JSON` 갱신
- 같은 날 이미 업로드했으면(수동 실행 포함) 22:07 실행은 바로 종료됩니다.
- 공개 저장소는 60일 동안 활동이 없으면 예약 워크플로가 비활성화될 수 있습니다 (Actions 화면에서 다시 Enable).
- 클라우드와 Windows 작업 스케줄러를 동시에 쓰면 DB 가 달라 하루 2개가 올라가므로 하나만 사용합니다.

### Windows 작업 스케줄러 (PC 에서 실행할 때)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register_windows_task.ps1                       # 매일 22:00 (+23:00 재시도)
powershell -ExecutionPolicy Bypass -File scripts\register_windows_task.ps1 -Time 21:30 -RetryTime ""
```

- `run_daily.bat` 은 자기 위치를 프로젝트 루트로 사용하므로 폴더를 옮겨도 동작합니다.
- 22:00 에 PC 가 꺼져 있었다면 켜진 직후 실행(StartWhenAvailable), 절전 상태면 깨워서 실행(WakeToRun), 네트워크 연결 후 실행(RunOnlyIfNetworkAvailable)합니다. `run_daily.bat` 도 DNS 가 붙을 때까지 최대 10분 기다립니다.
- 23:00 재시도 실행은 하루 1개 정책 덕분에 이미 업로드했으면 바로 끝나고, 실패했으면 실패 단계부터 이어서 실행합니다.
- PC 가 켜져 있고 Windows 에 로그인된 상태여야 실행됩니다.
- 실행 로그: `logs/task_scheduler.log`, 일자별 상세 로그: `logs/YYYY-MM-DD.log`

### macOS (launchd)

```bash
bash scripts/install_macos_launchd.sh 10 0
```

### 보조: Python 스케줄러

`python -m app.main schedule` — 프로세스가 켜져 있는 동안 매일 `schedule.generate_time` 에 실행합니다.

## 8. 프로젝트 구조

```text
app/
├── main.py                    # CLI (analytics / trends 하위 명령 포함)
├── schemas.py                 # 콘텐츠 데이터 구조 (Gemini JSON 검증)
├── config/  settings.py, config.yaml
├── ai/      gemini_client.py, topic_generator.py, script_generator.py, scene_generator.py,
│            metadata_generator.py, validator.py, json_parser.py, prompts/*.txt
│            script_quality.py (대본 품질·루프 엔딩), title_selector.py (제목 후보 평가)
├── topics/  duplicate_checker.py, selector.py, scoring.py (주제 점수)
├── trends/  base.py (TrendProvider), wikipedia.py, youtube.py, google_trends.py (알파, 비활성), collector.py
├── analytics/ youtube_analytics.py (API), sync.py (수집), report.py (보고서), insights.py (제작 반영), studio_import.py
├── research/researcher.py     # Wikipedia 자료 수집
├── voice/   base.py, edge_tts.py, narration.py
├── assets/  base.py, pexels.py, pixabay.py, local.py, image_generator.py, asset_manager.py,
│            audio_library.py, synth_audio.py
├── subtitle/whisper.py, chunker.py
├── video/   ffmpeg.py, scenes.py, subtitle_renderer.py, renderer.py, hook_effect.py (첫 3초 효과)
├── video_motion/ base.py (Provider 인터페이스·오류 분류), providers.py (CogVideoX-Flash Adapter), provider_factory.py (조사 기록),
│            free_quota_guard.py (무료 한도 가드), motion_service.py (장면별 소스 결정), cache.py, result_validator.py,
│            ffmpeg_fallback.py (움직임 효과 / AI 영상 길이 맞춤)
├── quality/ checker.py (영상 규격), content_checker.py (콘텐츠 검수/업로드 게이트), ad_suitability.py (광고 적합성)
├── youtube/ auth.py, uploader.py, comments.py (자동 댓글·고정 상태)
├── branding/channel.py         # 브랜드 자산 생성 / 일관성 점검 / 채널 메타데이터 미리보기·적용
├── ai/      comment_generator.py (댓글 문안·검수), story_format.py (영상별 전개 형식)
├── video/   info_cards.py (정보 카드), voice/rights.py (음성 권리 상태)
├── database/db.py, models.py, repositories.py
├── scheduler/scheduler.py
├── pipeline/daily_pipeline.py
└── utils/   logger.py, files.py, retry.py
assets/  music/ (BGM + music.json)  sfx/  images/  videos/  fonts/  branding/ (brand.yaml 원본 + output/ 업로드용 PNG)
data/    database.sqlite  topics/  scripts/  metadata/  sources/  reports/
output/  audio/  videos/  subtitles/  thumbnails/  work/
```

- 모든 외부 서비스는 Adapter 구조 (`VoiceProvider`, `AssetProvider`)
- 긴 프롬프트는 코드가 아닌 `app/ai/prompts/*.txt` 에서 관리 (`{{변수}}` 치환)
- 재시도: 1차 실패 → 5~10초 → 2차 → 15~30초 → 3차 → 실패 기록 (무한 재시도 없음). 잘못된 API Key 등은 즉시 실패
- Gemini JSON 파싱/검증 실패: 오류 내용을 피드백으로 붙여 최대 2회 재요청 후 작업 실패 처리

## 9. 콘텐츠 원칙

- 카테고리 5개를 순환(가장 오래 안 쓴 카테고리 우선): 미제 사건 / 기묘한 현상·도시전설 / 희귀·전설의 생물 / 두뇌 자극 / 역사의 은밀한 이야기
- 모든 콘텐츠에 `FACT / UNCONFIRMED / LEGEND / URBAN_LEGEND / FICTION` 표시. FACT 가 아니면 설명란에 고지 문구가 자동으로 들어갑니다.
- 과도한 낚시 표현 금지 목록: `script.banned_phrases`
- 같은 Pexels/Pixabay 자료는 다른 영상에서 재사용하지 않습니다.

## 10. 저작권 / 라이선스

| 자료 | 라이선스 | 비고 |
|---|---|---|
| Pexels | [Pexels License](https://www.pexels.com/license/) | 상업적 사용 무료, 출처 표기 권장 → 설명란 자동 표기 |
| Pixabay | [Pixabay Content License](https://pixabay.com/service/license-summary/) | 상업적 사용 무료, 핫링크 금지 → 다운로드 후 사용 |
| Wikipedia | CC BY-SA | 사실 확인용 참고자료, 설명란에 출처 링크 표기 |
| 기본 BGM/효과음 | CC0 | `synth_audio.py` 로 직접 합성 (외부 음원 아님) |
| Edge TTS 음성 | **상업적 이용 권리 미확인 (`unverified`)** | edge-tts 코드 라이선스 ≠ Microsoft 음성 출력물 이용 허락. 15.5절 |
| Gemini 생성 텍스트/이미지 | Google 약관 | AI 생성 사실 고지 (`containsSyntheticMedia`, 설명란) |

- 직접 BGM 을 추가할 때는 `assets/music/` 에 파일을 넣고 `music.json` 에 `title, artist, source, license, url, mood` 를 기록하세요. **license 가 없는 음원은 사용하지 않습니다.**
- 로컬 이미지/영상(`assets/images`, `assets/videos`)은 같은 이름의 `.json` 에 `tags, author, license, source_url` 을 적을 수 있습니다.
- 각 서비스의 약관/정책은 변경될 수 있으므로 실제 운영 전에 반드시 최신 정책을 확인하세요.

## 11. 테스트

```bash
.venv/bin/python -m pytest -q        # 단위 + FFmpeg 렌더링 통합 테스트
python -m app.main test --offline    # 실제 TTS/Whisper/FFmpeg 로 59초 샘플 Shorts 제작
```

## 12. 완료 체크리스트

| 항목 | 상태 |
|---|---|
| Gemini API 연동 / 대본 / Hook / Scene / 제목 / 설명 / 해시태그 | ✅ 실제 Gemini 호출로 제작 확인 |
| 주제 DB / 주제 중복 방지 | ✅ 구현 + 테스트 (실제 후보 8개 중복 검사 통과) |
| Edge TTS / Whisper / 자막 / FFmpeg / BGM / 효과음 / 1080x1920 출력 | ✅ 실제 제작 검증 |
| 품질 검사 / SQLite 기록 / Retry / Error Log | ✅ 실제 실패 → `retry` 로 재개 검증 |
| YouTube OAuth / Upload | ✅ OAuth 앱 프로덕션 게시, Windows PC 에서 재인증 (채널: 팬텀로즥) |
| YouTube 자동 공개 (`publish_mode: public`) | ✅ 클라우드 업로드 영상이 YouTube API 조회 결과 `privacyStatus: public` (2026-10-10) |
| GitHub Actions 클라우드 자동 실행 | ✅ 수동 실행으로 제작 → 업로드 → DB 저장 성공 (약 5분), 매일 22:07 KST 예약 |
| AI 장면 이미지 (Gemini) | ⏳ 구현 + 테스트 완료, Gemini 결제 연결 후 확인 필요 (무료 티어는 이미지 모델 한도 0) |
| AI 장면 이미지 대체 (Cloudflare Workers AI SDXL, 무료) | ✅ 실제 제작 확인: 8개 Scene 모두 768x1344 AI 이미지 (장당 약 11초), GitHub Secrets 등록 완료 |
| AI 장면 이미지 대체 (Hugging Face SDXL) | ⏳ 구현 + 테스트 완료, 토큰 권한 OK → 계정 크레딧 0 이라 `402 Payment Required` (fal-ai, hf-inference 모두). 크레딧 충전 시 동작, 그 전에는 자동으로 다음 provider 사용 |
| Pexels / Pixabay | ⏳ 구현 완료, API Key 입력 후 확인 필요 (현재는 자체 그래픽으로 대체) |
| Windows Scheduler / run_daily.bat | ✅ 등록 (매일 22:00 + 23:00 재시도), 현재는 클라우드 사용 중이라 **비활성화** (중복 업로드 방지) |
| Windows 실제 제작 | ✅ Windows 11 / Python 3.14 에서 `today` 로 영상 제작·품질 검사 통과 (2026-10-08) |
| README / .env.example | ✅ |
| 외부 트렌드 탐색 (위키백과 Pageviews) | ✅ 구현 + 실제 확인 (2026-10-15, 로컬: 후보 8개 조회수/증가 속도 수집, 429 대응) |
| 외부 트렌드 탐색 (YouTube Data API mostPopular/search) | ⏳ 구현 + 테스트 완료 / 실제 연결 검증 필요 (로컬 token.json 만료) |
| Google Trends 공식 API | ⏸ 알파(승인자만, 엔드포인트 비공개) → 어댑터 자리만, 비활성 |
| 주제 점수 / 선정 근거 기록 (topic_scores) | ✅ 구현 + 테스트 + 실제 Gemini 후보로 확인 |
| YouTube Analytics 성과 수집 | ⏳ 구현 + 테스트 완료 / 실제 연결 검증 필요 (`yt-analytics.readonly` 재인증 + API 사용 설정 필요) |
| 성과 보고서 / 제작 반영 (카테고리·주제 점수·대본 프롬프트) | ✅ 구현 + 테스트 (실제 데이터는 수집 후) |
| 대본 품질·자연스러운 루프 엔딩 / 제목 후보 3개 / 콘텐츠 검수 게이트 | ✅ 구현 + 테스트 + `test --offline` 통과 |
| 첫 3초 Hook 효과 (FFmpeg) | ✅ 실제 렌더링 확인 (imageio-ffmpeg 7.1, 프리셋 4종, 필터 미지원/실패 시 효과 없이 렌더링) / GitHub Actions FFmpeg 는 첫 실행에서 확인 필요 |
| 상태 DB 암호화 (`STATE_DB_KEY`) | ⏳ 구현 (openssl 왕복 로컬 확인) / Secret 등록 후 Actions 실행 확인 필요 |
| 주제 맞춤 자동 댓글 (commentThreads.insert) | 🧪 구현 + Mock 테스트 / **미검증**: 재인증(force-ssl) 전이라 실제 등록 안 함, 기본 `comments.enabled: false` |
| 댓글 고정 | ✋ **수동 처리 필요** (공식 API 미지원 확인) → `comments pins` / `mark-pinned` |
| 채널 브랜딩 자산 / 일관성 점검 | ✅ 아이콘·배너·워터마크 생성, `branding check` 통과 (docs 개인정보처리방침 수정 후) |
| 채널 메타데이터 적용 (channels.update / channelBanners) | 🧪 구현 + Mock 테스트 / **미검증**: 실제 채널에 적용하지 않음 (`branding apply --yes` 필요) |
| 프로필 사진·워터마크 적용 | ✋ 수동 처리 필요 (Studio) |
| 전개 형식 6종 / 고유 설명(explainer) / 정보 카드 / AI 이미지 표시 | ✅ 구현 + 테스트 + `test --offline` 렌더링 확인 / 실제 Gemini 대본으로는 미검증 |
| 사실·출처·장면 일치·라이선스·광고 적합성 검수 | ✅ 구현 + 테스트 (규칙 기반 보조 검사, 수익화 보장 아님) |
| Edge TTS 상업적 이용 권리 | ⚠️ `unverified` 표시 + 검수 경고 (확인은 사람이 해야 함) |
| 이미지→영상: FFmpeg 움직임 효과 9종 / 우선순위 / 품질 검사 | ✅ 구현 + 테스트 + 실제 FFmpeg 렌더링 확인 (Windows, imageio-ffmpeg 7.1) / GitHub Actions FFmpeg 는 첫 실행에서 확인 필요 |
| 이미지→영상: 무료 한도 가드·캐시·재사용·오류 분류 | 🧪 구현 + Mock 테스트 (실제 API 호출 없음) |
| 이미지→영상: 무료 Image-to-Video API | ⏸ **검증된 무료 API 없음** (2026-10-17 조사) → 외부 호출 비활성. CogVideoX-Flash 는 Adapter 만 구현, 미검증 |

## 13. 작업 내역

### 2026-10-05 — 1차 구축 (`32d5be9`)

**환경 분석**
- macOS 15.6 (arm64), 기존 Python 3.9 / FFmpeg 없음 / 빈 프로젝트 폴더
- `uv` 로 Python 3.11 설치 → `.venv` 생성, `requirements.txt` 설치
- FFmpeg: 시스템 설치 대신 `imageio-ffmpeg` 내장 FFmpeg 7.1 사용 (libx264, AAC, zoompan, xfade 지원 확인), ffprobe 대신 PyAV 로 분석

**구현 (마스터 프롬프트 Phase 1~11)**
- Phase 1: 폴더 구조, `config.yaml` + `.env` 설정, SQLite(topics/jobs/assets/step_logs), 로그, CLI
- Phase 2: `GeminiClient` (JSON 모드, 파싱 실패 시 피드백 붙여 최대 2회 재요청), 주제/대본/Scene/메타데이터 생성기, 프롬프트 파일 분리
- Phase 3: 중복 검사 = 키워드(조사 제거·동의어 정규화) + 문자 bigram 유사도 + Gemini 의미 판단
- 자료 조사: Wikipedia API(ko/en) → Gemini 가 FACT/UNCONFIRMED/LEGEND 등으로 정리
- Phase 4: Edge TTS 를 Scene 별로 합성 → 실제 Scene 시간 확정, 길이 초과 시 말하기 속도 자동 상향
- Phase 5: Pexels → Pixabay → 로컬 → (Gemini 이미지) → 자체 그래픽 순 Asset Manager, 출처/라이선스 기록, 다른 영상에서 쓴 자료 재사용 방지
- Phase 6: faster-whisper 단어 타이밍 + 대본 텍스트 정렬로 오탈자 없는 Shorts 자막
- Phase 7: Scene 클립(9:16 crop, Ken Burns) → xfade 전환 → Pillow 자막(강조 단어 색상) + BGM 덕킹 + 효과음 + -14 LUFS
- 저작권 없는 기본 BGM 3곡 / 효과음 6종을 FFmpeg 로 직접 합성 (CC0)
- Phase 8: 품질 검사 (길이, 해상도, fps, 코덱, 오디오, 자막, 파일 크기, 검은 화면) 실패 시 업로드 금지
- Phase 9: YouTube OAuth / 업로드 / 12:00 예약 공개(늦으면 late_policy)
- Phase 10~11: `run_daily.bat`, 작업 스케줄러 등록 PowerShell, macOS launchd, 하루 1개 정책, 실패 단계부터 재개

**테스트 중 발견해 수정한 문제**
- 대본 첫 문장을 Whisper `initial_prompt` 로 주면 그 문장을 건너뛰어 첫 자막이 6초에 나옴 → 제거, Scene 경계 겹침 수정, 인식 누락 시 음성 길이 비율 배분
- 실측 TTS 속도 4.9자/초 (예상 6.8자/초) → `chars_per_second` 보정
- 한 글자 강조어("한")가 "한가운데서"에도 칠해짐 → 정확히 일치할 때만 강조
- 출처 표기 중복 줄 제거

### 2026-10-05 — Gemini 실제 연동 (`ae06ff2`)

- Gemini API Key 등록 후 실제 제작: 주제 "디아틀로프 고개의 비극" 선정 → 위키백과 4건 조사 → 237자 대본 → 8개 Scene → 49.8초 영상, 품질 검사 통과
- `gemini-2.5-flash` 503(과부하) 반복 → `fallback_models` 자동 전환 기능 추가
- Gemini 가 한국어 글자 수를 잘 못 맞춤(389자 → 199자 → 341자로 3회 실패) → 문장 수(8~11문장, 문장당 20~30자)로 지시, 줄이거나 늘릴 분량을 구체적으로 피드백, 허용 범위를 목표의 0.85~1.2배로 완화
- 실패한 작업을 `retry` 로 대본 단계부터 재개해 완성 (주제/자료 조사는 저장된 결과 재사용)

### 2026-10-05 — YouTube 연동 (`2095145`)

- 기본 Gemini 모델 `gemini-3.5-flash` 로 변경 (호출 확인), 대체 모델 3개 설정
- YouTube OAuth 클라이언트 등록 → "액세스 차단됨" 오류는 테스트 사용자 추가로 해결 → 인증 완료, `token.json` 저장
- `upload --privacy` 옵션 추가 → 디아틀로프 고개 영상 **비공개 업로드 성공** (https://youtube.com/shorts/cdllfZBnxa0), API 로 상태 확인
- 자동 테스트 36개 통과

### 2026-10-08 — AI 장면 이미지 / 자동 공개 / 매일 자동 실행 수정 (`af884bb`)

**Windows PC 환경 구성**
- Windows 11 Home, Python 3.14.8 (3.11 미설치 → 3.14 로 `.venv` 생성, `requirements-dev.txt` 설치 정상)
- FFmpeg: `imageio-ffmpeg` 내장 7.1 사용, 한글 폰트: `C:/Windows/Fonts/malgunbd.ttf` 자동 탐색
- `setup` 실행 → `.env` 생성(빈 템플릿), DB 생성, 기본 BGM 3곡 확인
- `GEMINI_API_KEY` 는 Windows 사용자 환경변수에 이미 설정되어 있어 `.env` 없이도 동작 (`load_dotenv(override=False)`)
- `doctor` 결과: YouTube client / token 없음 → 업로드 전 `auth` 필요

**문제와 원인**
- 영상 배경이 어두운 단색만 나옴 → Pexels/Pixabay Key 없음 + `ai_image.enabled: false` 라서 매번 마지막 fallback(자체 그래픽 그라디언트)이 사용됨
- 업로드 영상이 비공개 → 테스트 업로드를 `--privacy private` 로 했고, 미인증(감사 전) API 프로젝트는 YouTube 가 비공개로 잠금
- 하루 1개 자동 실행이 안 됨 → `app.mode: "development"` 라 실행돼도 업로드를 건너뜀, Windows PC 에 작업 스케줄러/`.venv`/`.env`/`token.json` 이 없었음

**수정**
- AI 장면 이미지: Scene 의 `visual_prompt`(대본 기반) + 영상 제목/주제 + 공통 화풍(`ai_image.style`) + 분위기로 Gemini 이미지 모델이 9:16 장면 이미지를 생성, provider 순서 1순위로 변경
  - 이미지 모델 fallback(`ai_image.fallback_models`), 한도/권한 오류 시 즉시 다음 provider 로 전환(Scene 마다 재시도하지 않음)
  - 무료 티어 한도 0(`limit: 0`)은 재시도 없이 "결제 연결 필요" 안내 후 Pexels → … → 자체 그래픽으로 계속 제작
  - Scene 프롬프트에 나레이션 내용을 구체적으로 그리고 장면 간 시대/장소가 이어지도록 지시 추가
- `publish_mode: "public"` (업로드 즉시 공개), 업로드 직후 실제 공개 상태를 조회해 비공개로 잠기면 감사 신청 안내 경고
- `app.mode: "production"`, Windows 작업 스케줄러 등록(매일 10:00), 등록 스크립트가 `.venv`/`.env`/`token.json`/production 모드 누락을 경고
- 무료 이미지 서비스(Pollinations)는 2026-10 현재 결제 필요(402)로 확인되어 사용하지 않음
- 자동 테스트 40개 통과 (Python 3.14.8, Windows 11)

**실제 확인 결과**
- `models` 로 확인한 이미지 모델: `gemini-3.1-flash-image`, `gemini-3.1-flash-lite-image`, `gemini-2.5-flash-image`, `gemini-3-pro-image(-preview)`, `gemini-3.1-flash-image-preview`
  → 기본 `gemini-3.1-flash-image`, 대체 `gemini-2.5-flash-image` 로 설정
- 현재 API Key 로 이미지 모델 3종 호출 → 모두 `429 RESOURCE_EXHAUSTED ... free_tier ... limit: 0` (무료 티어에서는 이미지 생성 불가, 결제 연결 필요)
  → 이 경우 0.6초 만에 안내 후 대체 자료로 넘어가도록 처리
- Windows 작업 스케줄러 `StrangeRecords Daily Shorts` 등록 완료 (다음 실행 2026-10-09 10:00)

**영상 제작 (`today --no-upload`, job `20261008-173014`)**
- 주제: 미제 사건 카테고리 → "하늘에서 2억과 함께 증발한 남자, DB 쿠퍼 미스터리 🪂" (FACT)
- 단계별 시간: 주제 21초 → 조사 18초 → 대본 10초 → Scene 19초 → 음성 7초 → 자료 2초 → 자막 120초(Whisper 모델 첫 다운로드 포함) → 렌더 90초 → 품질 검사 통과 → 메타데이터 8초, 총 296초
- 결과: `output/videos/20261008-173014.mp4` (14.7MB), 상태 `rendered` (YouTube 인증 전이라 업로드 대기)
- 시각자료: AI 이미지가 무료 티어 한도 0 으로 첫 Scene 에서 제외 → Pexels Key 도 없어 자체 그래픽(어두운 그라디언트) 배경 사용
- 이 PC 의 DB 는 새로 만들어져 기존 주제 0개 → macOS 에서 만든 영상(디아틀로프 고개 등)과의 중복 검사가 되지 않음

### 2026-10-10 — Hugging Face 대체 AI 이미지 / 매일 22:00 자동 실행

**Hugging Face 대체 이미지 (`HuggingFaceImageProvider`)**
- provider 순서 `ai_image → hf_image → pexels → pixabay → local → procedural`. Gemini 가 NonRetryableError 로 제외되면 AssetManager 가 같은 Scene 에서 곧바로 `hf_image` 를 호출
- Gemini 이미지 429 는 재시도 대기 없이 다음 모델 → 모두 막히면 다음 provider 로 (기존: 5~10초 대기 후 재시도)
- `huggingface_hub.InferenceClient(provider="auto", api_key=HF_API_KEY)` 의 `text_to_image` 로 SDXL 768x1344 생성 (요청 헤더 `Authorization: Bearer`)
- 확인 결과: 예전 `api-inference.huggingface.co` 는 DNS 가 없어졌고, SDXL 은 `hf-inference` 에서 내려가 현재 **fal-ai** provider 로만 서비스 → 직접 URL 대신 `provider="auto"` 라우팅 사용
- 등록한 토큰은 유효(whoami 200)하지만 `403 ... does not have sufficient permissions to call Inference Providers` → 토큰 권한 추가 필요. 이 경우 0.5초 만에 제외하고 다음 provider 로 진행되는 것 확인
- offline 테스트(`test --offline`)와 pytest 는 HF 를 호출하지 않음, 자동 테스트 44개 통과

**매일 자동 실행 22:00**
- 10/10 오전 실행 실패 원인: 절전 해제 직후 DNS 미연결(`getaddrinfo failed`)로 Gemini 3회 실패 → `run_daily.bat` 에 네트워크 대기(최대 10분), 작업에 `RunOnlyIfNetworkAvailable` 추가
- 작업 스케줄러 `StrangeRecords Daily Shorts` 를 매일 22:00 + 23:00(재시도)로 다시 등록, `schedule.generate_time: "22:00"`

**YouTube 인증 + 클라우드 자동 실행**
- Windows PC `.env` 에 OAuth 클라이언트 등록 → `auth` 성공 (채널: 팬텀로즥), `token.json` 저장
- PC 를 켜지 않아도 되도록 GitHub Actions 워크플로 추가 (매일 22:07 KST), DB 는 `pipeline-state` 브랜치로 이어 씀 → 중복 업로드 방지를 위해 Windows 작업 스케줄러 작업은 비활성화
- GitHub Secrets 등록: `GEMINI_API_KEY`, `YOUTUBE_TOKEN_JSON`, `HF_API_KEY`

**OAuth 앱 프로덕션 게시**
- 프로덕션 전환에 홈페이지/개인정보처리방침 URL 이 필수 → `docs/index.html`, `privacy.html`, `terms.html` 작성, GitHub Pages(main `/docs`) 로 공개
- 브랜딩: 앱 이름 `기묘한 기록`, 지원/개발자 이메일, 위 URL 3개, 승인된 도메인 `logic-phantom.github.io`, 로고 없음 → 앱 게시 완료
- 테스트 상태에서 받은 토큰(7일 만료)을 버리고 `auth` 로 재인증 → `YOUTUBE_TOKEN_JSON` 갱신

**클라우드 첫 실행 결과 (Actions run `38015678898`, workflow_dispatch)**
- 4분 43초 만에 성공. 오전에 네트워크 오류로 실패했던 job `20261010-094711` 을 주제 단계부터 이어서 완성
- 주제: "주민 2%에게만 들리는 마을의 정체불명 저주파 괴소음 🔊" → https://youtube.com/shorts/NV0XwJyq8Wo
- YouTube API 로 확인: `privacyStatus: public`, `uploadStatus: processed` → **업로드 즉시 공개 동작 확인**
- 시각자료 7개 모두 자체 그래픽(procedural): Gemini 이미지 한도 0 + HF 토큰 권한 없음 + Pexels Key 없음
- 실행 경고 정리: Node.js 20 지원 종료 → `checkout@v5`, `setup-python@v6`, `cache@v5`, `upload-artifact@v6` (Node 24), `ubuntu-latest` 가 10/19 부터 Ubuntu 26 으로 바뀌므로 `ubuntu-24.04` 고정

**무료 AI 이미지: Cloudflare Workers AI (`CloudflareImageProvider`)**
- HF 토큰 권한 추가 후 재확인 → 403 은 해결됐지만 계정 크레딧 0 으로 `402 Payment Required` (fal-ai SDXL, hf-inference SD3 모두), Pexels 는 신규 API Key 발급 중단 상태
- 무료 대안으로 Cloudflare Workers AI 추가: 무료 플랜 하루 10,000 Neurons, `@cf/stabilityai/stable-diffusion-xl-base-1.0` 은 단가 0원, 768x1344 지정 가능
- provider 순서 `ai_image → cf_image → hf_image → pexels → pixabay → local → procedural`, 응답이 PNG 바이너리/ base64 JSON 둘 다 처리
- 401/403/429/400/404 는 이번 작업에서 제외, 5xx 는 1회 재시도. 자동 테스트 47개 통과
- Cloudflare 가입 후 API 토큰 생성이 계속 실패 → 원인은 계정 이메일 미인증 (인증 메일 확인 후 생성 성공)
- 실제 확인: 단일 이미지 768x1344 11초 → `test` 전체 제작(job `20261010-130140`, "1518년 춤의 전염병")에서 **8개 Scene 모두 `cf_image`**, 369초에 완성. 같은 실행에서 Gemini 텍스트 모델 503 과부하 → `gemini-flash-latest` 로 자동 전환되는 것도 확인

**Whisper 자막 타이밍 오류 수정**
- `TypeError: open() got an unexpected keyword argument 'metadata_errors'` → PyAV 17+ 에서 `av.open(metadata_errors=)` 가 없어져 faster-whisper 1.2.1(최신)의 오디오 디코딩이 실패, 자막이 음성 길이 비율 배분으로 대체되고 있었음 (클라우드도 최신 PyAV 설치라 같은 문제)
- `requirements.txt` 에 `av>=11,<17` 고정 → PyAV 16.1.0 에서 Whisper 단어 76개 타이밍 정상 인식 확인

### 2026-10-13 — Edge TTS 403 장애 수정 / 영어 자막·설명 / 자막 개선

**10/13 영상 미등록 원인**
- Actions run `38312255411` (예약 13:07 UTC → GitHub 지연으로 10/13 06:42 KST 실행)이 `voice` 단계에서 3회 모두 실패 (job `20261013-064242`, 주제 "풍선 탱크로 나치를 속인 유령 부대")
- 오류: `WSServerHandshakeError: 403` — Microsoft 가 2026-10 부터 edge-tts 가 쓰는 합성 주소 `speech.platform.bing.com` 을 막음 ([rany2/edge-tts#490](https://github.com/rany2/edge-tts/issues/490)). 최신 edge-tts 7.2.8 도 동일, 로컬 macOS 에서도 재현
- 이미지(Gemini) 문제는 아니었음: 10/11·10/12 영상은 Cloudflare 이미지로 정상 제작

**수정**
- Edge TTS: Edge 155 가 쓰는 `wss://api.msedgeservices.com/tts/cognitiveservices/websocket/v1` (같은 토큰을 `Ocp-Apim-Subscription-Key` 로 전달)를 먼저 쓰고, 403 이면 기존 주소로 전환 (`voice.endpoints`)
- 새 주소는 연속 요청에 `429` → 요청 간격 2.5초(`voice.min_interval`), 429 시 5/10/20/30/45초 대기 후 재요청(`voice.rate_limit_waits`). 실측: 2초 이상 간격이면 통과
- Gemini 이미지 비활성 (`assets.ai_image.enabled: false`, 무료 티어 한도 0) → Cloudflare → Hugging Face → … 순
- 영어 자막: Scene 생성 시 `narration_en`(영어 번역)도 함께 생성, 없으면(이전 작업 재개 등) 자막 단계에서 Gemini 로 번역. 한국어 자막 아래에 작게 표시, SRT 에도 두 줄로 기록 (`subtitle.english`)
- 영어 설명: 메타데이터에 `title_en` / `description_en` 추가 → 설명란에 한국어 요약 다음 `🌏 English` 블록, 출처 제목·AI 고지도 영어 병기, 영어 해시태그 1~2개
- 자막 크기 축소 (한국어 92 → 72px, 영어 38px), 자막 덩어리를 문장/쉼표 단위 안에서 길이가 고르게 나뉘도록 변경(끝에 한 어절만 남는 깜빡임 방지, "한 / 명도" 처럼 수사 뒤에서 끊지 않음), 같은 Scene 안에서는 자막 사이 빈 화면 없이 이어짐, 장면 전환 크로스페이드 0.3 → 0.5초
- 자동 테스트 51개 통과, `test --offline` 로 실제 TTS(새 주소)·Whisper·렌더링 확인

**10/13 영상 재제작 (Actions run `38391291911`, workflow_dispatch `today`)**
- 실패했던 job `20261013-064242` 를 `voice` 단계부터 이어서 완성, 19분 소요 → https://youtube.com/shorts/8KUWyvwXjqI (public)
- 음성: 새 주소로 8개 Scene 합성 성공 (500 1회 → 재시도로 통과), 영어 자막 번역 8개 Scene, 메타데이터는 Gemini 503 과부하로 `gemini-2.5-flash` 로 자동 전환
- 시각자료: Cloudflare 가 느려(장당 53~102초) 2개 Scene 이 120초 타임아웃 → 오류 횟수가 "연속"이 아니라 누적으로 세어져 Cloudflare 가 중간에 제외됨 → **8개 중 4개만 AI 이미지**, 나머지는 자체 그래픽
  - 수정: 성공하면 오류 횟수 초기화(연속 2회 실패일 때만 제외), Cloudflare `timeout` 120 → 180초. 자동 테스트 52개 통과

**AI 이미지 누락(자체 그래픽 4장) 정확한 원인과 재발 방지**
- 경과 (job `20261013-064242`, 로그 기준)
  - Scene 1: Cloudflare SDXL 120초 타임아웃 2회(약 4분) → Hugging Face 402(크레딧 0) 로 HF 는 작업 내내 제외 → Pexels/Pixabay 키 없음 → **자체 그래픽**
  - Scene 2~5: Cloudflare 성공 (장당 53~102초, 평소 11초보다 5~10배 느림)
  - Scene 6: 다시 120초 타임아웃 2회 → 오류 횟수가 Scene 1 과 **누적**되어 2회 → Cloudflare 를 "이번 작업에서 제외"
  - Scene 7·8: Cloudflare 가 이미 제외되어 **시도조차 하지 않고** 자체 그래픽
- 근본 원인: ① Cloudflare SDXL 이 혼잡해 응답이 느렸는데 제한 120초 + 대체 모델 없음 ② "연속 오류" 제외 기준이 실제로는 누적이라 간헐적 타임아웃 2번으로 제외 ③ 대체할 AI 수단(HF)이 크레딧 0 ④ 최종 대체가 어두운 자체 그래픽이고, 품질 검사는 완전한 검은 화면만 걸러서 그대로 업로드됨
- 재발 방지: 위 "AI 이미지 필수" 규칙 (Cloudflare 모델 전환 → 재시도 라운드 → 그래도 없으면 업로드 중단 후 재실행). 자동 테스트 56개 통과
- Cloudflare 3개 모델 실제 비교 (같은 프롬프트 "풍선 고무 탱크"): FLUX.1 schnell 2.4초·풍선 탱크를 정확히 그림(1024x1024, 크기 지정 불가) / SDXL-Lightning 4.0초·768x1344·일반 탱크 / SDXL base 103.8초·일반 탱크
  → **1순위 FLUX** (자막 내용에 가장 맞음) → Lightning → SDXL base
- 오늘 영상(8KUWyvwXjqI)은 삭제 후 `remake` 명령(워크플로 `command: remake`)으로 음성 단계부터 다시 제작·업로드

### 남은 작업

0. 성장 엔진 활성화 (2026-10-15 추가, 14절)
   - YouTube Analytics API 사용 설정 + OAuth 데이터 액세스에 `yt-analytics.readonly` 추가 → `auth` 재인증 → `YOUTUBE_TOKEN_JSON` 갱신 (안 하면 공개 통계만 수집)
   - GitHub Secret `STATE_DB_KEY` 등록 (공개 저장소의 DB 에 비공개 성과 지표가 들어가므로)
   - 첫 클라우드 실행에서 확인: `Collect YouTube performance` 단계 결과, Hook 효과 로그(`첫 장면 Hook 효과 적용`), `topic_scores`/`production_metadata` 기록

0-1. 수익화 준비 기능 활성화 (2026-10-16 추가, 15절)
   - OAuth 데이터 액세스에 `youtube.force-ssl` 추가 → `auth` 재인증 → `YOUTUBE_TOKEN_JSON` 갱신 → `comments.enabled: true`
   - `branding apply` 로 채널 소개 미리보기 확인 후 `--yes` (선택 `--banner`), 프로필 사진·워터마크는 Studio 에서 수동 업로드
   - Edge TTS 상업적 이용 조건 확인 (확인 전에는 `unverified`)

1. 시각자료 보조 수단(선택): Cloudflare 무료 한도(하루 10,000 Neurons)나 장애에 대비
   - Pixabay API Key (무료, 이미 구현됨) → GitHub Secret `PIXABAY_API_KEY`
   - Hugging Face: 토큰 권한은 추가 완료, 계정 크레딧 0 이라 402 → https://huggingface.co/settings/billing 에서 충전 시 동작
   - Pexels API Key → 2026-10-10 현재 신규 발급 중단
   - Google AI Studio 에서 Gemini API 프로젝트에 결제 연결 (장당 약 $0.04, 1순위로 사용됨)
2. 앱 로고를 넣고 싶으면 브랜드 인증 심사 필요 (안 해도 동작에 문제 없음)
3. macOS 의 `data/database.sqlite` 에 있는 주제(디아틀로프 고개 등)는 클라우드 DB 에 없음 → 필요하면 `pipeline-state` 브랜치 DB 에 병합
4. 로컬에 남은 DB 쿠퍼 영상(job `20261008-173014`, rendered)은 업로드 안 됨. 올리려면 로컬에서 `upload` (클라우드 DB 에는 기록되지 않음, 그날 영상이 2개가 됨)

## 14. 성장 엔진 (트렌드 · 성과 분석 · 자동 개선)

목표: 영상 제작 횟수가 아니라 **실제 YouTube 성과를 저장·분석해 다음 영상의 주제·대본·편집을 조금씩 개선**하는 것.
초기에는 설명 가능한 규칙 기반이며, 데이터가 적을 때는 아무것도 바꾸지 않습니다.

```text
외부 트렌드 탐색 → 주제 후보 → 중복·성과 분석 → 점수 → 대본(도입부·루프) → 시각 연출(첫 3초)
→ TTS·이미지·자막·렌더링 → 품질·독창성 검수 → 업로드 → Analytics 수집 → 보고서 → 다음 제작에 반영
```

### 14.1 외부 트렌드 탐색 (`app/trends/`, `trends:`)

| Provider | 데이터 | 비용/인증 | 상태 |
|---|---|---|---|
| `wikipedia` | ko 위키백과 전날 인기 문서(힌트), 후보 문서의 최근 30일 일별 조회수 → 관심도 + 증가 속도(최근 7일 ÷ 이전 기간) + 문서 존재(사실 확인 가능성) | 무료, Key 없음 | 기본 사용 |
| `youtube` | `videos.list chart=mostPopular`(지역 인기 영상, 1 unit), `search.list`(최근 30일 같은 주제 영상, 100 units) → 시간당 조회수, 과밀도 | Data API 할당량 (하루 search 3회 = 300 units) | `YOUTUBE_API_KEY` 또는 token.json |
| `google_trends` | 공식 API (알파) | 승인 필요 | 비활성 (엔드포인트·응답 형식 비공개) |

- mostPopular 차트는 "지역 인기 영상 목록"일 뿐 모든 분야의 실시간 트렌드가 아닙니다. 조회수는 게시 후 경과 시간으로 나눈 **시간당 조회수**로만 봅니다.
- 화제라도 채널 단어(`trends.channel_keywords` + 카테고리 예시)와 관련 없으면 버리고, 실제 피해자가 있는 강력 범죄·참사·정치 화제(`trends.exclude_keywords`)는 힌트로 쓰지 않습니다. 일반 인기 문서는 연예/정치 위주라 힌트가 0개인 날이 많은데 정상입니다.
- 공개 API 로 알 수 없는 검색량·CTR·경쟁 채널 내부 지표는 만들지 않습니다(값이 없으면 NULL).
- 모든 신호는 `trend_signals` 에 출처 URL·수집 시각·데이터 기간과 함께 저장되고, 같은 날 같은 요청은 캐시를 씁니다.
- 실패 격리: provider 별로 오류를 잡아 그 provider 만 이번 실행에서 제외합니다(403/429/토큰 만료 즉시 제외, 위키백과는 요청 간격 1초).

### 14.2 주제 점수 (`app/topics/scoring.py`, `trends.scoring`)

모든 요소를 0~1 로 정규화한 뒤 가중 평균 − 패널티:

```text
base  = Σ(weight × component) / Σ(weight)        (값이 없는 요소는 빠지고 나머지로 재정규화)
final = base − 0.15 × saturation − 0.20 × near_duplicate
```

| 요소 | 계산 | 기본 가중치 |
|---|---|---|
| trend | 위키백과 일평균 조회수 / YouTube 시간당 조회수 (log 정규화) × 신선도(반감기 48시간) | 0.15 |
| velocity | 위키백과 최근 7일 ÷ 이전 기간 (1배=0, 2배 이상=1) | 0.10 |
| channel_fit | 0.6 + 0.4 × 채널 단어 일치 | 0.15 |
| performance | 같은 카테고리의 과거 성과 지수 (표본 부족 시 없음) | 0.10 |
| novelty | 1 − 기존 주제와의 최대 유사도 | 0.20 |
| story | 후보의 전개 단계(의문→배경→반전→회수) 수 | 0.15 |
| evidence | truth_status(FACT 1.0 … FICTION 0.2) × 위키백과 문서 존재 | 0.15 |

- 같은 회차에서 일부 후보만 외부 신호가 있으면, 신호가 없는 후보는 **회차 중앙값**으로 채웁니다 (결측이 유리해지지 않도록).
- 사실 확인 가능성 < 0.5(FICTION 등) 또는 채널 관련성 < 0.35 이면 trend/velocity 를 반영하지 않습니다 → 조회수만 높은 허구/무관한 주제는 채택되지 않습니다.
- 외부 신호가 하나도 없으면 **기존 규칙(가장 덜 비슷하고 FACT 우선)** 으로 고릅니다(`strategy: legacy`).
- 카테고리: `topic.category_strategy: rotate_performance` — 최근 2개 카테고리는 쉬고 "오래 안 쓴 정도 60% + 성과 지수 40%". 성과 표본이 부족하면 기존 순환과 동일.
- 선정 근거: `topic_scores` 테이블, `data/topics/<job>.json` (`selection_reasons`, `scores`, `trend_status`), 최근 사건에서 착안한 후보는 대본 프롬프트에 "날짜·출처 확인된 내용만 단정" 주의가 붙습니다.

### 14.3 YouTube 성과 수집 (`app/analytics/`, `analytics:`)

**권한 (필수 설정)**: 현재 token.json 은 `youtube.upload` + `youtube.readonly` 만 있어 Analytics 를 조회할 수 없습니다. 업로드는 그대로 동작합니다.

1. [Google Cloud Console](https://console.cloud.google.com/apis/library/youtubeanalytics.googleapis.com) 에서 **YouTube Analytics API** 사용 설정
2. Google 인증 플랫폼 → **데이터 액세스** 에 `https://www.googleapis.com/auth/yt-analytics.readonly` 범위 추가
3. 로컬에서 `python -m app.main auth` → 업로드 + 읽기 + Analytics 읽기 권한을 한 번에 승인 (기존 token.json 은 `token.json.bak` 으로 백업)
4. 새 `token.json` 내용으로 GitHub Secret `YOUTUBE_TOKEN_JSON` 갱신 → `python -m app.main analytics status` 로 확인

- 토큰은 기록된 범위 그대로 갱신합니다(업로드 범위를 덮어쓰거나 없는 범위를 요청하지 않음). 권한이 없으면 Data API 공개 통계(조회/좋아요/댓글)만 저장하고(`collection_status=no_scope`) 안내를 남깁니다.
- 수집 지표(Analytics, 게시일 하루 전~오늘 누적): views, engagedViews, estimatedMinutesWatched, averageViewDuration, averageViewPercentage, likes, comments, shares, subscribersGained, subscribersLost. `filters=video==ID` 로 영상별 조회.
  지표 조합이 거부되면(400) 묶음별로 다시 요청하고, 그래도 실패한 지표는 `missing_metrics` 에 기록합니다.
- Data API 공개 수치는 `public_view_count` 등 **별도 컬럼**에 저장합니다 (Analytics 값과 섞지 않음).
- 하루 1번 수집이므로 "24시간 값"을 만들지 않습니다. `hours_since_publish` 에 실제 경과 시간을 기록하고, 보고서는 "24h 이후 첫 수집(실제 31h)"처럼 표시합니다. Analytics 는 집계가 끝난 날까지만 반환하므로 1~3일 늦을 수 있습니다.
- 같은 영상·같은 수집일(KST)은 UPSERT → 중복 저장 없음. 영상별 실패는 격리되고 다음 실행에서 다시 수집합니다.
- **Studio 전용 지표**(Shorts 피드 노출 수, 시청함 대 스와이프 비율)는 Analytics API 에 없습니다. Studio 에서 내보낸 CSV 를 `analytics import-csv` 로 가져온 경우에만 `studio_metrics` 에 출처(`manual_csv:파일명`)와 함께 저장·사용합니다.

### 14.4 보고서와 자동 개선 (`analytics report`, `insights.py`)

보고서: 영상별 성과(경과 시간, 시점별 조회수), 카테고리 / 길이 / 도입 유형 / 루프 / Hook 효과 / 제목 전략 / 시각 자료별 비교, 공유·구독이 좋았던 영상, 판단할 수 없는 항목, 개선 제안. `--format json|csv` 로 `data/reports/` 에 저장.

- 비교는 같은 시점(72h 이후 첫 수집값)의 조회수와 **같은 카테고리 중앙값 대비** 평균 시청 비율로 합니다. 그룹마다 표본 수와 신뢰도(3개 미만 "판단 불가", 10개 미만 "낮음")를 표시합니다.
- 진단: 데이터 부족 / 낮은 노출(Studio CSV 필요) / 낮은 시청 선택 비율(Studio CSV 필요) / 조회수 낮음(API 로는 노출·선택 구분 불가) / 시청 지속 낮음 / 지속 양호·공유·구독 전환 낮음 / 공유 상대적으로 높음 — 각각 개선 제안이 붙습니다.
- 제작 반영: 판단 가능한 영상이 `analytics.min_samples`(3) 이상일 때만 ① 카테고리 성과 지수(채널 평균 쪽으로 축소, k=5) → 카테고리 선택·주제 점수 ② 도입 유형/루프/길이별 상대 성과와 신뢰도 → 주제·대본 프롬프트에 "참고용"으로 전달. 부족하면 "데이터 부족 → 기본 원칙" 으로만 전달합니다.

### 14.5 대본 · 루프 엔딩 · 제목 · 검수

- 대본 구조: 첫 1~3초 의문/이상 현상 → 최소 배경 → 새 단서/반전 → 처음 질문 회수. `hook_type`, `story_beats` 를 함께 생성해 성과 비교에 씁니다.
- 추가 검증(재생성, 최대 2회): 미완성 결말(콜론/말줄임/"다음 편"), 문장 반복, FACT 가 아닌데 단정, 최근 30개 대본과 유사(≥0.55), 첫 문장 반복(≥0.7). 기존 분량/금지 표현/사실 고지 검증은 그대로입니다.
- **자연스러운 루프 엔딩** (`script.loop_ending`): loop=true 면 마지막 문장(ending)이 사건의 핵심을 회수하면서 첫 문장과 같은 대상·시간·장소를 공유해야 합니다. 품질 점수(완결 문장 필수, 핵심어 연결 50% + 단순 반복 아님 30% + 질문으로 끝나지 않음 20%)가 0.75 미만이면 루프를 쓰지 않은 것으로 기록합니다. 루프 효과는 가정하지 않고 Analytics 로 비교합니다.
- 제목: 핵심 미스터리 / 증거·발견 / 역설·의문 3개 후보 → 내용 일치·첫 문장 일치·길이·핵심 검색어·과장 여부·최근 제목 유사도·영어 제목 숫자 일치로 평가(CTR·검색량 추정 없음). 후보와 점수는 메타데이터 `title_selection` 에 기록. 한/영 설명의 연도가 다르면 재생성.
- **콘텐츠 검수** (`review:`, 메타데이터 다음·업로드 전): critical = 출처 누락, 근거 없는 단정, 사실 여부 고지 누락, 장면 불일치/누락, AI 이미지 필수 위반, 최근 영상과 같은 구성(대본 유사도 ≥0.6), 미완성 결말, 라이선스 누락, AI 고지 누락 → **업로드 금지, 작업 상태 `review_needed`** (다음 `today` 는 새 주제로 진행). 사람이 확인했다면 `upload JOB --approve-review`. minor(낚시성 제목, 최근 제목과 유사)는 다른 제목 후보로 1회 자동 교체. originality_score 는 보조 지표이며 수익화 승인 예측이 아닙니다.

### 14.6 첫 3초 시각 Hook (`video.hook_effect`)

- 첫 Scene 클립에만 적용(자막은 나중에 합성되므로 흔들리거나 번지지 않음). 이후 장면·전환·자막·BGM 덕킹·음량 정규화는 그대로입니다.
- 프리셋: `subtle`(약한 흔들림+대비+노이즈) / `vhs`(대비+RGB 분리+노이즈) / `glitch`(흔들림+대비+0.12초 RGB 글리치 최대 3회) / `impact`(강한 흔들림+대비) / `none`. `allow_glitch/noise/shake` 로 개별 차단.
- 흔들림은 프레임을 약 1% 키운 뒤 잘라 움직여 테두리가 보이지 않습니다(흔들림 프리셋은 첫 장면 전체가 약 1% 확대). 빠른 점멸 반복 없음. 세기는 시간에 따라 줄어 2.5초 뒤 원래 화면.
- 사용 중인 FFmpeg 의 `-filters` 를 확인해 없는 필터의 효과만 빼고, 렌더링이 실패하면 효과 없이 첫 장면을 다시 만듭니다. 적용 결과는 `render.hook_effect` / `production_metadata.hook_effect_preset` 에 기록.

### 14.7 SQLite 스키마 추가 (마이그레이션 v1, 기존 테이블 변경 없음)

| 테이블 | 용도 | 중복 방지 |
|---|---|---|
| `schema_migrations` | 적용된 마이그레이션 버전 | PK |
| `video_performance_snapshots` | 영상별 성과 스냅샷 (Analytics 값 / 공개 통계 / 경과 시간 / 집계 기간 / 출처 / 상태 / 누락 지표) | UNIQUE(video, 수집일) |
| `studio_metrics` | Studio CSV 지표 + 출처 | UNIQUE(video, metric, 기간) |
| `trend_signals` | 트렌드 신호 + 출처/기간/신뢰도 | UNIQUE(provider, kind, keyword, region, 수집일) |
| `topic_scores` | 후보별 점수 구성·가중치·근거 | - |
| `production_metadata` | 영상별 제작 조건 (topic_category, topic_score, trend_sources, hook_type, story_structure, loop_ending_enabled, loop_quality_score, visual_style, hook_effect_preset, video_duration, subtitle_mode, title_strategy, script_similarity_score, originality_score) | PK(job_id) |

이전 버전 DB 를 열면 자동으로 새 테이블만 추가됩니다. jobs 상태에 `review_needed` 가 추가되었습니다.

### 14.8 2026-10-15 작업 내역

- 실제 확인: 기존 테스트 56개 통과 확인 후 구현 → 테스트 124개 통과 (Python 3.11.17, macOS, imageio-ffmpeg 7.1), `test --offline` 으로 TTS·Whisper·렌더링(Hook 효과 subtle 적용)·품질 검사·콘텐츠 검수까지 통과
- 실제 Gemini + 위키백과로 `topic` 실행: 처음에는 후보 문서 검색을 연달아 보내 위키백과가 429 → 요청 간격 1초, 429 시 이번 실행에서 중단, 한국어 문서 우선/후보 5개 제한으로 수정 후 429 없이 수집
- 실제 실행에서 "외부 신호가 없는 후보가 더 높은 점수"를 받는 결측 편향 발견 → 회차 중앙값 보정
- 위키백과 인기 문서에서 "사건" 같은 일반어 하나로 민감한 실제 사건이 관련 있다고 잡히는 문제 → 일반어 제외, `exclude_keywords` 추가
- 로컬 macOS 의 token.json 은 만료/철회(`invalid_grant`) 상태 → YouTube Data/Analytics 실제 조회는 검증하지 못함 (영상별 실패 격리·안내는 확인)

## 15. 수익화 준비 (댓글 · 브랜딩 · 검수 · 권리)

2026-10-16 추가. 성장 엔진(`app/topics/`, `app/trends/`, `app/analytics/`)은 변경하지 않았습니다.
상태 표기: ✅ 구현+테스트 / 🧪 구현+Mock 테스트, 실제 서비스 미검증 / ✋ 수동 처리 필요.

### 15.1 주제 맞춤 자동 댓글 (`comments:`, `app/youtube/comments.py`) 🧪

- 영상이 **실제로 public 으로 확인된 뒤** 채널 계정으로 최상위 댓글 1개를 등록합니다 (`commentThreads.insert`, 50 units).
  비공개·예약 영상은 `waiting_publish` 로 두고, 공개된 뒤 `comments retry`(GitHub Actions 가 매 실행 시작 시 자동 실행)에서 등록합니다.
- 문안: 영상 제목 + 최종 대본 + 자료 조사의 확인된 사실/미확인 정보 + 대본의 `explainer`(핵심 단서·가설·남은 질문)로 Gemini 가 작성
  (유형: 단서 해석 질문 / 두 가설 비교 / 미해결 의문 / 추가 관점). Gemini 를 쓰지 않거나 실패하면 대본의 댓글 질문으로 구성합니다.
- 등록 전 검수(통과 못 하면 등록 안 함, `rejected`): 빈 댓글, 길이, 대괄호·빈칸 등 미완성 문구, 링크·해시태그·멘션·이모지, 구독/좋아요 요청·홍보,
  존재하지 않는 후속편·이벤트, 허위 경험·시청자인 척, 낚시 표현, 질문 없음, 대본 핵심어와 겹치는 단어 2개 미만(영상과 무관), 대본에 없는 연도,
  이전 자동 댓글과 유사(반복 댓글).
- 중복 방지: `video_comments` 테이블(마이그레이션 v2, 영상당 1행 UNIQUE). 본문은 처음 한 번만 만들어 저장하고 재시도는 같은 본문을 씁니다.
  등록 전에 항상 그 영상의 최근 댓글 중 **채널 계정이 쓴 같은 본문**을 찾아, 타임아웃으로 실제로는 등록됐던 경우 새로 등록하지 않고 `posted` 로 정리합니다.
- 재시도: 타임아웃·5xx·429 → `failed` (영상당 최대 `max_attempts`=5회), 권한 부족 → `permission_denied`(API 호출 안 함, 재인증 후 재시도),
  댓글 사용 중지·영상 없음·400 → `rejected`(자동 재시도 안 함), 업로드 후 `retry_window_days`(7일) 지나면 `skipped`.
- 실패 격리: 파이프라인 `comment` 단계(upload 와 record 사이)는 예외를 올리지 않습니다. 댓글이 실패해도 작업은 `success`, 하루 1개 정책·업로드 결과는 그대로입니다.
  업로드 후 단계에서 실패한 작업을 다시 실행해도 이미 올라간 영상(state 에 video_id 있음)은 **다시 업로드하지 않도록** 수정했습니다.
- 기록 항목: job_id, 영상 ID, 본문, 유형, 생성기(gemini/fallback), 댓글 ID, 상태, 시도 횟수, 오류 코드/사유, 생성·등록·마지막 시도 시각, 고정 상태·기록 시각·메모.

**OAuth 권한 / 재인증 (필수, 사람이 해야 함)**

공식 문서상 `commentThreads.insert` 는 `https://www.googleapis.com/auth/youtube.force-ssl` 범위만 허용합니다. 현재 token.json 에는 없습니다.

1. [Google 인증 플랫폼 → 데이터 액세스](https://console.cloud.google.com/auth/scopes) 에 `.../auth/youtube.force-ssl` 추가 (민감 범위: 동의 화면에 "YouTube 계정 관리" 로 표시됨)
2. 로컬에서 `python -m app.main auth` → 업로드·읽기·Analytics·댓글 권한을 한 번에 승인 (기존 토큰은 `token.json.bak` 로 백업, `.gitignore` 에 추가됨). 끝에 실제 승인된 범위를 출력합니다
3. **GitHub Secret `YOUTUBE_TOKEN_JSON` 을 새 token.json 내용으로 갱신** (안 하면 클라우드 실행은 `permission_denied` 로 기록만 하고 업로드는 정상)
4. `config.yaml` → `comments.enabled: true` → `python -m app.main comments preview` 로 문안 확인 → 다음 실행부터 자동 등록
5. 권한 점검: `python -m app.main comments status` / `doctor`

### 15.2 댓글 고정 ✋ 수동 처리

YouTube Data API 의 `comments`/`commentThreads` 리소스에는 고정 속성·메서드가 없습니다 (2026-10-16 공식 문서 확인).
브라우저 자동화·비공개 엔드포인트로 고정하지 않습니다. 등록 성공은 고정 성공이 아니므로 `pin_status` 를 따로 둡니다:
`not_applicable`(등록 전) → `manual_required`(등록됨, 고정 필요) → `pinned_manual`(사람이 고정했다고 기록, API 로 검증한 값 아님) / `unknown`(댓글이 조회되지 않음).

1. `python -m app.main comments guide` → `data/reports/pin_guide.md` (영상 제목·URL, 댓글 ID, 본문, 절차)
2. YouTube 에서 해당 댓글 ⋮ → 고정 (영상당 1개, 기존 고정 댓글은 교체됨)
3. `python -m app.main comments mark-pinned <영상ID> --note "Studio 에서 고정"` (취소: `--undo`)

### 15.3 채널 브랜딩 (`assets/branding/`, `branding:`)

| 파일 | 용도 | 적용 |
|---|---|---|
| `assets/branding/brand.yaml` | **원본**: 채널 소개(한/영, AI 고지 포함), 키워드, 언어/국가, 영상 설명란 채널 소개 한 줄, 색상, 시각 가이드 | - |
| `output/banner_2560x1440.png` | 배너 (16:9, 최소 2048x1152, 6MB 이하, 글자는 안전 영역 안) | `branding apply --yes --banner` (API) 🧪 |
| `output/icon_800.png` | 프로필 사진 (98x98 로 표시, 15MB 이하) | ✋ Studio > 맞춤설정 > 브랜딩 (API 미지원) |
| `output/watermark_150.png` | 동영상 워터마크 (정사각형 150px 이상, 1MB 미만) | ✋ Studio (이 도구는 watermarks.set 미구현) |

- 출력 PNG 는 `branding assets` 로 다시 만들 수 있습니다 (원본 = brand.yaml + `app/branding/channel.py`).
- `branding apply`: 현재 `brandingSettings` 를 먼저 읽고, 바꿀 필드(설명·키워드·언어·국가)만 덮어쓴 **전체** 값을 보냅니다
  (공식 문서: 요청에 빠진 속성은 삭제됨). 채널 제목은 현재 값 유지. 기본은 미리보기(`--dry-run` 과 같음), `--yes` 일 때만 적용,
  적용 후 다시 읽어 실제 반영을 확인하고 결과를 `data/reports/branding_apply_*.json` 에 기록합니다. 권한: `youtube.force-ssl` (15.1 재인증).
- 영상 설명란 끝(해시태그 앞)에 brand.yaml 의 `video_footer` 한 줄이 붙습니다 (`branding.video_footer: false` 로 끔).

**기존 홈페이지 점검 결과** (`branding check`, URL·파일명 변경 없음)
- `docs/index.html`, `privacy.html`, `terms.html` 존재·상호 링크 정상.
- 불일치 발견 → 수정: `auth` 가 이미 요청하던 `yt-analytics.readonly` 와 성과 지표 저장이 개인정보처리방침에 없었음, 이미지 제공자가 Hugging Face 만 적혀 있었음(실제 1순위 Cloudflare).
  → Analytics·댓글(`youtube.force-ssl`) 범위, 저장 데이터(성과 지표, 댓글 기록), pipeline-state DB 암호화, Cloudflare/Wikipedia 를 기재. 약관에 자동 댓글 고지 추가.
- OAuth 동의 화면의 앱 정보(이름 `기묘한 기록`, 홈페이지/정책 URL)는 그대로 유효합니다. 새 범위를 추가하면 Google 이 앱 확인을 다시 요구할 수 있습니다(개인 사용 앱은 "확인되지 않은 앱" 경고 후 진행 가능).

### 15.4 영상별 독창성 · 사실성 · 광고 적합성 검수 ✅

**대본 (영상마다 다른 설명)**
- 전개 형식 6종(`app/ai/story_format.py`): 의문-배경-반전-회수(classic) / 사건 타임라인 재구성 / 증거 비교 / 두 가설 검토 / 역사 기록 해석 / 과학적 설명과 전설 비교.
  카테고리·사실 여부에 맞는 형식 중 **최근 2개 영상에서 쓰지 않은 것**을 작업 ID 로 결정적으로 고르고, 형식마다 대본이 담아야 할 정보가 다릅니다 (심리테스트는 classic 만).
- 대본과 함께 `explainer`(확인된 사실, 미확인 주장, 핵심 단서, 가설별 근거·약점, 확정할 수 없는 부분, 타임라인, 출처 간 일치 여부, 이 영상만의 고유한 설명)를 생성합니다.
  누락은 생성을 막지 않고 검수 warning 으로 기록합니다.

**정보 카드 (`video.info_cards`)**: 확인된 사실 vs 미확인 주장 / 두 가설(근거·약점) / 타임라인(시점 3개 이상) / 핵심 단서 카드를 장면 위에 표시 (영상당 최대 2개, 첫·마지막 장면 제외).
나레이션에 실제로 나온 항목만, '확인' 라벨은 자료 조사 FACT 와 겹치는 항목만 씁니다. 자막 트랙(Pillow)에 함께 그려 추가 비용·API 가 없습니다.
지도·인물 관계도는 정확한 위치/관계 데이터가 없어 만들지 않았습니다(잘못된 정보를 그리지 않기 위해).

**AI 이미지 표시 (`video.ai_label`)**: AI 이미지를 쓴 영상은 왼쪽 위에 "AI 생성 이미지 · 재현" 을 작게 표시해 실제 촬영 기록으로 오해되지 않게 합니다.

**검수 항목 추가** (기존 항목은 그대로, `review:`)

| 코드 | 내용 | 기본 | `strict: true` |
|---|---|---|---|
| fact_unlinked | FACT 사실이 수집 자료(source_index)에 연결되지 않음 | warning | critical |
| unsourced_claim | 대본의 연도가 수집 자료에 없음 | warning | critical |
| unconfirmed_as_fact | 미확인/전설 항목과 같은 내용을 단정적으로 서술 | warning | critical |
| source_link_missing / invalid | 설명란에 출처 링크 없음 / 형식 이상 | warning | critical / warning |
| description_unsupported | 설명란에만 있는 연도 | warning | warning |
| visual_scene_mismatch | AI 이미지가 다른 장면의 프롬프트로 그려짐 | critical | critical |
| visual_prompt_unrelated | 장면 이미지 설명과 나레이션(영어)이 겹치지 않음 | warning | warning |
| license_unverified | 자료/BGM 라이선스가 unknown·미확인 | critical | critical |
| voice_rights_unverified | Edge TTS 상업적 이용 권리 미확인 | warning | critical |
| explainer_* / unique_insight_missing / format_repetition | 고유한 설명 부족, 같은 형식 반복 | warning | warning |
| ad_graphic_visual | 이미지 프롬프트에 유혈·시신·훼손 묘사 (광고 적합성 `block`) | critical | critical |
| ad_graphic_narration | 잔혹 묘사 (실제 피해 사건이면 critical) | warning | critical |
| unfounded_accusation | 범죄 혐의 단정 (사실이 아닌 주제) | critical | critical |
| misleading_true_story | 사실이 아닌 주제를 '실화'로 소개 | critical | critical |
| ad_shock_title / ad_sensitive_topic | 충격 위주 제목, 실제 피해 사건 | warning | warning |

- critical 은 기존과 같이 업로드 금지 → `review_needed`. 광고 적합성은 `pass | warning | review_needed | block` 으로 `review.ad_suitability` 에 기록되며 **수익화 승인·광고 수익을 보장하지 않는 보조 지표**입니다.
- `upload JOB --approve-review` 로 진행해도 critical 항목은 해결된 것으로 바뀌지 않고 `review.approved_unresolved` 에 남습니다. `python -m app.main review JOB` 로 확인.
- Gemini 가 대본을 만들었다는 사실만으로 사실 검증이 끝난 것으로 보지 않습니다 (연도·사실-출처 연결을 규칙으로 다시 확인).

### 15.5 Edge TTS 권리 상태 ⚠️

- edge-tts 라이브러리의 오픈소스 라이선스는 **코드** 이용 조건이고, 그 라이브러리가 접속하는 Microsoft Edge '소리 내어 읽기' 서비스(비공개 엔드포인트)의 **음성 출력물을 상업적으로 써도 된다는 허락이 아닙니다**.
  수익화 영상 사용을 명시적으로 허용하는 공개 약관/서면 답변은 확인하지 못했습니다 → `voice.rights_status: "unverified"`.
- 일반 모드: 검수 warning (`voice_rights_unverified`), `doctor`·`rights` 에 표시. 엄격 모드(`review.strict: true` 또는 `review.unverified_rights_policy: review_needed`): 업로드 보류.
- 코드는 자동으로 `verified` 로 바꾸지 않습니다. 확인되면 사람이 `rights_status: verified` + `rights_evidence`(약관 URL·서면 답변 보관 위치) + `rights_checked_at` 을 적어야 인정됩니다 (근거 없으면 unverified 로 처리).
- 이번 작업에서 음성 녹음·음성 복제·새 TTS 엔진은 추가하지 않았습니다.

### 15.6 보안 · 자동 실행 점검 결과

| 항목 | 결과 / 조치 |
|---|---|
| `STATE_DB_KEY` 암호화 | 키가 있으면 AES-256 저장, 복호화·암호화 실패 시 단계가 실패(bash -e)하고 평문으로 조용히 바뀌지 않음. 키가 없으면 평문 저장 → **Actions 경고 추가**. 댓글 기록도 이 DB 에 들어갑니다. Secret 등록 여부는 저장소 설정에서 확인 필요 |
| 토큰/비밀값 노출 | 토큰 값은 로그에 출력하지 않음(댓글 오류는 코드/사유만 기록). Artifact 는 mp4 만. **`token.json.bak` 이 `.gitignore` 에 없던 문제 수정** (`auth` 가 만드는 백업) |
| 댓글 재시도 ↔ 업로드 재시도 | 댓글은 영상당 1행 + 등록 전 기존 댓글 확인. 업로드 후 단계 실패 시 재실행해도 재업로드하지 않도록 수정 |
| Actions ↔ Windows 작업 스케줄러 | DB 가 달라 하루 1개 정책이 서로를 모름 → 동시에 쓰면 하루 2개 업로드 가능. 현재 Windows 작업은 비활성 상태 유지 필요 (코드로 막지 않음) |
| 하루 1개 정책 | 변경 없음 (`comment` 단계 추가 후에도 작업 상태 `success` 기준 동일) |

### 15.7 2026-10-16 작업 내역

- 기존 테스트 126개 통과 확인 후 구현 → 테스트 **173개 통과** (Windows 11, Python 3.14.8, `PYTHONUTF8=1`).
  `PYTHONUTF8` 없이 Windows 에서 실행하면 기존 `test_analytics.py::test_report_groups_diagnosis_and_export` 가 cp949 인코딩으로 실패합니다 (이번 변경과 무관, 성장 엔진 범위라 수정하지 않음).
- `test --offline`: TTS·Whisper·렌더링(정보 카드 2개)·품질 검사·콘텐츠 검수(광고 적합성 pass) 통과, 업로드/댓글 단계는 건너뜀.
- 실제 Gemini 호출, 업로드, 댓글 등록, 채널 메타데이터 변경은 실행하지 않았습니다 (Mock 테스트만).

## 16. 이미지 → 영상 (무료 Image-to-Video + FFmpeg 움직임 효과)

2026-10-17 추가. 목표는 **모든 장면을 AI 영상으로 강제하는 것이 아니라**, 추가 비용 0원 원칙으로 무료 사용량 안에서 만들 수 있는 장면만 AI 영상으로 바꾸고 나머지는 FFmpeg 로 완성하는 것입니다.
기존 단계(대본·이미지 생성·음성·Whisper 자막·BGM/효과음·업로드·하루 1개 정책·OAuth)는 바꾸지 않았고, `assets` 와 `subtitles` 사이에 `motion` 단계 하나를 추가했습니다.

### 16.1 구조

```text
[10] 기존 AI 장면 이미지 (Cloudflare 등, 원본은 덮어쓰지 않음)
→ [10-1] motion 단계 (app/video_motion/)
   1) 장면 우선순위: 첫 장면 Hook(1) → 핵심 단서·증거(2, 대본 explainer.key_clues 와 나레이션) → 반전·결론(3) → 배경(4)
      + 장면별 FFmpeg 움직임 종류 (visual_prompt·나레이션 단어로 결정, Gemini 추가 호출 없음, 같은 효과 연속 금지)
   2) 재사용: 같은 작업의 이전 실행 결과(SQLite video_motion_clips) → 캐시 파일 (검증 통과한 것만)
   3) FreeQuotaGuard 가 허락할 때만, 우선순위 순서로 외부 Image-to-Video 요청
   4) 결과 검증 (영상 시그니처·손상·길이·해상도·검은 화면) → 실패하면 정상 영상으로 쓰지 않음
→ [12] 렌더링: AI 영상 장면 = 9:16 crop + 장면 길이에 맞춤(최대 1.6배 느리게 + 마지막 프레임 유지)
               나머지 장면 = FFmpeg 움직임 효과 → 그래도 실패하면 기존 Ken Burns
               첫 장면 Hook 효과·xfade 전환·자막·BGM 덕킹·효과음·-14 LUFS 는 그대로
→ [13] 품질 검사 (기존 항목 + 아래 16.6)
```

| 파일 | 책임 |
|---|---|
| `base.py` | `ImageToVideoProvider` 인터페이스, `FreeStatus`(verified_free / unverified / paid), HTTP 오류 분류 `classify_http_error` |
| `providers.py` | `ZhipuCogVideoFlashProvider` (비동기 생성 → 제한 시간 내 상태 확인 → 다운로드). 현재 `unverified` |
| `provider_factory.py` | 설정 → provider 목록, 후보 서비스 조사 기록(`RESEARCH`). 유료 provider 는 만들지 않음 |
| `free_quota_guard.py` | `FreeQuotaGuard`: 무료 검증·작업당/하루 요청 수·길이 한도, 비용 오류 시 중단, 유료 설정 강제 무시 |
| `motion_service.py` | `MotionService`: 우선순위·재사용·요청·오류 처리·DB 기록·요약 로그 |
| `cache.py` | 캐시 키(이미지 SHA-256 + 프롬프트 + provider/모델 + 길이 + 해상도/fps + 모델 파라미터), `output/motion_cache/` |
| `result_validator.py` | 결과 영상 검증 |
| `ffmpeg_fallback.py` | 움직임 효과 9종, AI 영상 길이 맞춤 |

### 16.2 무료 Image-to-Video API 조사 (기준일 2026-10-17, 공식 문서)

| 후보 | 상태 | 확인 내용 | 공식 URL |
|---|---|---|---|
| 智谱 BigModel **CogVideoX-Flash** | **미검증 (unverified)** → 호출 안 함 | "免费视频生成模型"(무료 영상 생성 모델) 표기. 이미지(URL/base64)+프롬프트, `POST /api/paas/v4/videos/generations` → `GET /api/paas/v4/async-result/{id}`. **UNKNOWN**: 무료 요청 한도(일/분), 한도 초과 시 동작, 해외 전화번호 가입·실명 인증, 결과물 상업적 이용 조건, 영상 길이(flash 는 duration 파라미터 없음), 영상 URL 보관 기간, 중국 외 서버(GitHub Actions) 호출 허용 | [모델](https://docs.bigmodel.cn/cn/guide/models/free/cogvideox-flash) · [생성 API](https://docs.bigmodel.cn/api-reference/模型-api/视频生成异步) · [결과 조회](https://docs.bigmodel.cn/api-reference/模型-api/查询异步结果) |
| Hugging Face Inference Providers (`image_to_video`) | **유료** | 무료 사용자 월 크레딧 **None**, 크레딧 구매 후 종량제 (PRO $2/월). 현재 토큰은 이미 402. Custom Provider Key 는 provider 가 직접 과금 → 사용 금지 | [pricing](https://huggingface.co/docs/inference-providers/pricing) |
| Cloudflare Workers AI | **비활성 (영상 모델 없음)** | 모델 카탈로그에 image-to-video/영상 생성 없음. (무료 한도 초과 시 오류로 거절, 00:00 UTC 초기화) | [models](https://developers.cloudflare.com/workers-ai/models/) · [pricing](https://developers.cloudflare.com/workers-ai/platform/pricing/) |
| Google Gemini API **Veo 3.1** | **유료** | 무료 티어 "Not available", 초당 $0.05~$0.40 | [pricing](https://ai.google.dev/gemini-api/docs/pricing) |
| Alibaba Model Studio Wan I2V | **유료 (제외)** | 모델당 1회성 무료 50초(90일)지만 활성화에 결제 정보 등록 필요, "Free Quota Only" 기본 꺼짐 | [free quota](https://www.alibabacloud.com/help/en/model-studio/new-free-quota) |
| Replicate / fal.ai / Runway / Luma / Pollinations / SiliconFlow / Together | **유료** | 공식 무료 사용량 없음 또는 금액 미기재, 결제/크레딧 구매 필요 | [Replicate](https://replicate.com/pricing) · [fal](https://fal.ai/pricing) |
| NVIDIA API Catalog (Stable Video Diffusion) | **미검증** | 체험(trial) 약관이 테스트·평가 용도로 제한된다는 비공식 자료만 있고 공식 약관은 확인 못 함 → 수익화 채널에 부적합 | - |

- **결론: 무료 조건(공식 무료 사용량 / 무료 범위로 제한 가능 / 초과 시 과금 없이 거절 / 결제 정보 불필요)을 모두 공식 자료로 확인한 API 는 없습니다.** 그래서 외부 호출은 비활성 상태이고, 모든 장면이 FFmpeg 움직임 효과로 만들어집니다.
- 웹 UI 무료 이용은 API 무료 이용과 같지 않습니다. 위 표는 API 기준입니다.

**무료 provider 를 나중에 켜는 방법** (사람이 확인해야 함): 위 UNKNOWN 항목을 공식 자료(약관·가격·한도 문서, 서면 답변)로 확인 → `app/video_motion/providers.py` 의 해당 클래스 `free_status = FreeStatus.VERIFIED_FREE`, `checked_at`, `official_urls`, 공식 하루 한도(`official_free_requests_per_day`, 알면 가드가 그 절반만 사용)를 코드로 수정 → `config.yaml` 의 provider `enabled: true` + GitHub Secret 등록. **설정 파일만으로는 미검증·유료 provider 를 켤 수 없습니다** (실수로 과금되지 않도록).

### 16.3 설정 (`config.yaml` → `video_motion`)

| 키 | 기본 | 의미 |
|---|---|---|
| `enabled` | true | false 면 이 단계 전체를 끄고 예전 Ken Burns 렌더링 (외부 호출 없음) |
| `free_only` / `paid_fallback_enabled` | true / false | **코드에서 강제** (다른 값을 넣어도 무시하고 경고) |
| `max_requests_per_job` / `max_requests_per_day` | 8 / 8 | 실제로 보낸 요청 수(실패 포함). 하루 수는 `video_motion_requests` 테이블(KST)로 계산 → pipeline-state DB 로 클라우드 실행 사이에도 유지. 8회가 무료 8회를 보장한다는 뜻이 아님 |
| `max_clip_seconds` / `max_total_video_seconds_per_job` | 4 / 32 | 장면당 요청 길이 / 작업당 합계 |
| `max_consecutive_transient_failures` | 2 | 일시 오류 연속 횟수 → provider 비활성 |
| `max_retries_per_scene` / `retry_waits` | 1 / [10, 30] | 일시 오류·명시적 속도 제한만 재시도 |
| `quota_stop_fallback` | true | 무료 한도/크레딧/결제 오류 → 다른 provider 로 넘어가지 않고 이번 작업 외부 호출 **전체** 중단 |
| `cache_enabled` | true | 같은 입력이면 다시 요청하지 않음 |
| `fallback_motion` | true | AI 영상 없는 장면에 움직임 효과 (false: 기존 Ken Burns) |
| `providers` | CogVideoX-Flash (`enabled: false`) | provider 별 모델·타임아웃·상태 확인 시간·파라미터 |

GitHub Secrets: 현재 **추가할 필수 Secret 없음**. `ZHIPU_API_KEY` 는 선택이며 미검증 상태라 등록해도 호출하지 않습니다. 비밀 키는 코드·`config.yaml` 에 넣지 않고 Secrets/`.env` 로만 주입합니다.

**안전하게 끄는 방법**: ① 외부 API 만 끄기 → provider `enabled: false` (기본값) 또는 GitHub Secret 삭제 ② 단계 전체 끄기 → `video_motion.enabled: false`. 어느 쪽이든 제작·업로드는 그대로입니다.

### 16.4 무료 한도 도달 시 동작 (예: 장면 8개, 4번째 요청에서 한도 초과)

```text
Scene 1~3: AI 영상 생성 성공 → output/motion_cache 에 저장 + SQLite 기록
Scene 4  : 429(할당량) → 즉시 외부 호출 중단, quota_limited 기록 (같은 요청 재시도 없음, 다른 provider 탐색 없음)
Scene 5~8: 요청 안 함 → 기존 AI 이미지 + FFmpeg 움직임 효과
→ 8개 장면으로 렌더링 → 기존 품질 검사 → 기존 업로드 정책대로 진행
```

| 응답 | 분류 | 동작 |
|---|---|---|
| 429 + 할당량/크레딧 단어, 또는 **무엇인지 불명확한 429** | quota_exceeded | 이번 작업 외부 호출 전체 중단 |
| 429 + 속도 제한 단어(또는 Retry-After)만 | rate_limited | 요청 수 한도 안에서 1회 재시도 |
| 402, 크레딧 소진·결제 필요·잔액 부족(余额/欠费 포함) 본문 | billing_required | 즉시 중단 |
| 401 / 403 | auth_error | 무료 한도로 단정하지 않고 provider 비활성 + 기록, 재시도 없음 |
| 400 / 404 / 422 | invalid_request | provider 비활성 |
| 5xx / 타임아웃 / 네트워크 / 결과 대기 시간 초과 | transient | 1회 재시도, 연속 2회면 provider 비활성 |
| 영상 아님·손상·짧음·검은 화면 | invalid_result | 그 장면만 대체 (캐시에서 삭제) |

- 성공한 AI 영상은 삭제·재생성하지 않습니다. 재실행(`retry`, Actions 의 5/10분 뒤 재시도)하면 같은 작업의 기록과 캐시 파일을 검증해 재사용하고, 남은 장면만 (한도가 남아 있을 때) 요청합니다.
- GitHub Actions 는 실행마다 새 서버라 캐시 **파일**은 같은 실행 안의 재시도에서만 남습니다 (요청 수 기록은 DB 로 유지). Artifact 로 올리지 않아 저장 공간을 늘리지 않습니다.
- `motion` 단계는 어떤 오류도 작업 실패로 올리지 않습니다 (서비스 오류 시 전 장면 FFmpeg 효과). `assets.require_ai_image`(정지 AI 이미지 확보 조건)의 의미는 바꾸지 않았고, 영상 변환 실패는 이미지 확보 실패가 아닙니다.
- 이미 렌더링까지 끝난 이전 작업을 재개할 때는 이 단계를 건너뜁니다 (외부 호출 없음). `remake` 는 `motion` 기록이 없는 이전 작업도 다시 만들 수 있습니다.

### 16.5 FFmpeg 움직임 효과 (외부 API 없음, GPU 불필요)

| 종류 | 표현 | 주로 고르는 장면 (visual_prompt·나레이션 단어) |
|---|---|---|
| `slow_zoom_in` / `slow_zoom_out` | 천천히 다가가기/멀어지기 (기존 Ken Burns 의 65%) | 첫 장면 Hook, 단서·증거·얼굴·지도 / 마지막 장면 |
| `pan_left` / `pan_right` | 느린 좌우 시점 이동 | 바다·사막·산·해안·지평선 |
| `parallax` | 가운데(타원 마스크)와 바깥이 반대로 약 20px 이동하는 2층 패럴랙스 | 숲·복도·터널·골목·마을 |
| `fog_overlay` | 흐르는 안개 텍스처 screen 합성 (28%) | 안개·연기·늪·구름 |
| `light_flicker` | 0.5~2.3Hz 의 느린 밝기 변화 ±5% (빠른 점멸 없음) | 촛불·등불·횃불·번개 |
| `dust_overlay` | 천천히 떠다니는 먼지 입자 | 먼지·유적·고문서·폐허·고대 |
| `none` | 움직임 없음 (Scene `effect: static`) | - |

- 강한 글리치·흔들림은 쓰지 않습니다 (첫 3초 Hook 효과만 기존대로). 텍스처·마스크는 작업 폴더에 Pillow 로 한 번 만들고, 원본 이미지는 읽기만 합니다.
- 실측 (Windows, imageio-ffmpeg 7.1, 3초 장면 1개): 줌/팬 약 2초, 먼지 3.6초, 패럴랙스 4.3초, 안개 4.5초.

### 16.6 기록 · 품질 검사

- SQLite 마이그레이션 v3 (기존 테이블 변경 없음):
  `video_motion_clips` (job_id, scene_number, priority, 원본 이미지 경로·SHA-256, 생성 영상 경로, provider, 모델, 캐시 키, 요청 시각, 상태, 성공 여부, 길이, 소요 시간, 오류 코드, 중단 사유, 무료 한도 초과 여부, FFmpeg fallback 여부·종류, 캐시 재사용 여부) /
  `video_motion_requests` (실제로 보낸 요청: provider, 모델, 시각, 날짜, 결과, HTTP 상태, 소요 시간).
- 장면 상태: `ai_video_generated` · `cache_reused` · `fallback_motion` · `quota_limited` · `provider_disabled` · `generation_failed` · `asset_video`(자료가 원래 영상).
- 작업 기록: `jobs.state_json.motion`, `data/sources/<job>_motion.json`, `render.motion`(장면별 실제 사용 소스), `production_metadata.extra_json.video_motion`.
- 로그 (키·토큰은 출력하지 않음): `Image-to-Video Summary` — 요청 시도 수 / 성공한 AI 영상 수 / 캐시 재사용 수 / 무료 한도 초과 여부 / FFmpeg fallback 장면 수 / API 호출 중단 사유 / 최종 렌더링 결과.
- 품질 검사 추가 (실패 시 기존처럼 업로드 금지): `duration_match`(영상 길이 = 나레이션 + 여유), `motion_sources`(모든 장면에 영상 소스), `motion_fallback`(AI 영상 없는 장면은 FFmpeg 대체), `motion_ai_composition`(합성 결과 기록), `motion_api_limits`(작업당 한도 이내 + 중단 후 요청 0), `motion_images_preserved`(원본 AI 이미지 SHA-256 변화 없음). 해상도·fps·코덱·오디오·검은 화면은 기존 검사.

### 16.7 테스트 / 2026-10-17 작업 내역

```bash
PYTHONUTF8=1 .venv/Scripts/python.exe -m pytest -q tests/test_video_motion.py   # 이미지→영상 (Mock, 키 불필요)
```

- 기존 테스트 173개 통과 확인 후 구현 → 전체 **193개 통과** (Windows 11, Python 3.14.8, `PYTHONUTF8=1`). `tests/test_video_motion.py` 20개: 성공 저장·사용 / 3회 성공 후 할당량 초과 → 이후 요청 0 + 다른 무료 provider 도 호출 안 함 / 재실행 시 기존 영상 재사용(재생성 없음) / 402·잔액 부족 즉시 중단 / 인증 오류 재시도 없음 / 일시 오류 제한된 재시도 후 대체 / 속도 제한 후 성공 / 손상·짧음·검은 화면 거부 / 같은 입력 캐시 재사용 / 미검증 provider 는 HTTP 요청 0 / 유료 설정 강제 무시 / 작업당·하루 한도 / 우선순위 / 오류 분류 / CogVideoX Adapter 비동기 흐름(Mock HTTP, 결과 CDN 에 키 미전송) / AI 영상 + FFmpeg 효과 혼합 렌더링 → 1080x1920·30fps·AAC + 품질 검사 통과 / 파이프라인 단계가 예외를 올리지 않음
- `test --offline` (실제 Edge TTS·Whisper·FFmpeg): `motion` 단계 외부 요청 0 → 8개 장면 모두 FFmpeg 움직임 효과(slow_zoom_in/parallax/slow_zoom_out/fog_overlay/pan_right/none 등) → 58.7초 영상, 기존 품질 검사 + 이미지→영상 검사 전부 통과.
  이 실행에서 장면 기록이 SQLite 에 저장되지 않는 버그(인자 중복)를 발견해 수정하고 DB 재사용 테스트를 추가했습니다 (기록 실패는 경고만 남기고 제작은 계속되는 것도 확인).
- 실제 Image-to-Video API 호출, 실제 GitHub Actions 실행, YouTube 업로드는 이번 작업에서 하지 않았습니다.
