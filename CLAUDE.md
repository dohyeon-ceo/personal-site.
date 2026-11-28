# 작업 규칙

이 저장소는 사용자의 개인 사이트 모음이며, 코드를 잃어버리지 않는 것이 가장 중요하다.

- **코드를 수정할 때마다 바로 커밋하고 `main` 브랜치에 푸시한다.** 사용자가 이 저장소에 대해 명시적으로 허락한 방식이다. 작업을 끝낼 때 커밋되지 않은 변경이 남아 있으면 안 된다.
- `main`에 푸시하면 GitHub Actions가 저장된 데이터로 GitHub Pages에 바로 배포한다(약 1분). 배포가 깨지지 않도록 푸시 전에 화면을 확인한다.
- 평일 아침 예약 실행이 `stock-dashboard/site/data.json`을 새로 받아 `main`에 직접 커밋한다. 그래서 작업 시작 전과 푸시 전에 `git pull --rebase origin main`으로 최신 상태를 받는다.
- **새 기능을 넣기 전에 되돌릴 지점을 저장한다.** 작업 직전 `main` 커밋을 `save/<날짜>-before-<기능>` 브랜치로 푸시한다 (예: `git push origin HEAD:refs/heads/save/2026-11-21-before-cap`). 태그 푸시는 이 환경에서 막혀 있어 브랜치를 쓴다.
- 사용자가 "되돌려줘"라고 하면 그 기능의 커밋만 `git revert`로 되돌려 `main`에 푸시한다. `reset`·강제 푸시는 하지 않는다 (다른 대화의 커밋과 기록이 사라진다). 되돌린 것도 다시 되살릴 수 있다. 그 뒤에 쌓인 기능이 함께 빠져야 하면(되돌리기 충돌) 진행 전에 사용자에게 무엇이 같이 빠지는지 먼저 말하고 허락을 받는다.
- 사이트마다 폴더 하나 (`stock-dashboard/` 등). 새 사이트를 추가하면 루트 `index.html` 목록과 `.github/workflows/deploy.yml` 의 "배포 폴더 구성" 단계에도 추가한다.
- 사용자는 한국어를 쓰고 주로 휴대폰으로 본다. 화면은 모바일 우선, 하단의 "PC 버전으로 보기"로 PC 화면 전환.

## 아이디어 노트

- 사용자의 코딩 아이디어는 왜(목적)·어떻게(방법)·상태를 함께 적어 Claude 문서 "FIT·코딩 아이디어 노트"(https://claude.ai/artifact/B4QWD9MzLzCtmfS85dbTcE)에 모은다. 작업을 시작할 때 문서 도구가 있으면 먼저 읽고, 새 아이디어나 진행 상황이 생기면 거기에 반영한다.
- 노트는 코딩 전용이다. 사업 아이디어는 코딩 아이디어와 이어져 새 기회가 보일 때만, 어디서 왔고 무엇과 이어지는지 표시해서 적는다.

## youtube-script

- `youtube-script/index.html` 화면. 개인용 (`noindex`). 사용자가 "출시"를 말하기 전까지 홍보·공개 목적의 작업은 하지 않는다.
- 자막은 두 경로로 가져온다: ① 자막 서버 `api/transcript.js` (Vercel 서버리스 함수, 이 저장소를 Vercel에 연결하면 배포됨) ② 공개 Invidious 미러 (CORS 허용, 불안정). GitHub Pages는 정적 호스팅이라 ①은 Vercel에서만 돈다.
- Vercel 주소가 생기면 화면의 `API_SERVER` 상수에 넣는다. Vercel 주소에서 열면 같은 서버를 바로 쓴다.
- 이 작업 환경에서는 유튜브·미러 접속이 막혀 있어 실제 영상 테스트는 사용자 휴대폰에서 해야 한다.

## 법적 주의 (stock-dashboard)

- 투자 권유처럼 보이는 말("추천", "매수/매도", "사야 할", "유망", "목표가")을 화면에 쓰지 않는다. 점수·배지는 "같은 업종 안 숫자 비교"로만 표현한다.
- 면책 문구는 첫 방문 안내창, 하단, `#/terms` 이용 안내 페이지에 있다. 내용을 바꾸면 `TERMS_DATE`를 고친다 (바뀌면 안내창이 다시 뜬다).
- 측정 도구(GA·Clarity 등)나 외부 전송을 넣으면 이용 안내의 '개인정보' 항목을 반드시 같이 고친다.
- 유료화·광고 전에: 유사투자자문업 해당 여부를 법률 전문가에게 확인하고, 주가 출처를 정식 라이선스 데이터로 바꾼다 (Yahoo Finance 약관은 상업적 이용·재배포 금지). 사용자에게 먼저 알린다.

## stock-dashboard

- `site/index.html` 화면 (외부 라이브러리 없음), `site/names.js` 한글 이름표, `site/data.json` 데이터 스냅샷
- `scripts/fetch_data.py` 데이터 수집 (표준 라이브러리만). 출처: SEC EDGAR(재무), Yahoo Finance(주가), Wikipedia(S&P 500 목록, 나스닥 100·다우 30 구성종목)
- 지수 소속은 종목마다 `ix` 배열(`sp`·`ndx`·`dji`). 화면의 지수 선택은 보여줄 기업만 거르고, 비교 점수는 항상 전체 기업 기준이다. 지수 목록을 못 읽은 날은 전날 `data.json`의 소속을 쓰고 수집은 계속한다.
- 점수 추이: 종목마다 `sh` (`q` 달력 분기, `s` 점수, `p` 항목별 점수, `v` 항목 값). `fetch_data.py`의 `add_score_history`가 화면 `prepare`와 같은 규칙(업종 그룹·순위→0~100점·평균)으로 최근 8분기를 다시 계산한다. 점수 규칙을 바꾸면 두 곳을 같이 고친다.
- Yahoo 요청은 순차 + 간격을 두고, 429 응답에는 기다렸다 재시도한다. User-Agent나 호스트를 바꿔가며 제한을 우회하지 않는다.
- 로컬 실행: `python3 stock-dashboard/scripts/fetch_data.py --out stock-dashboard/site/data.json` 후 `python3 -m http.server -d stock-dashboard/site 8000`
