# 작업 규칙

이 저장소는 사용자의 개인 사이트 모음이며, 코드를 잃어버리지 않는 것이 가장 중요하다.

- **코드를 수정할 때마다 바로 커밋하고 `main` 브랜치에 푸시한다.** 사용자가 이 저장소에 대해 명시적으로 허락한 방식이다. 작업을 끝낼 때 커밋되지 않은 변경이 남아 있으면 안 된다.
- `main`에 푸시하면 GitHub Actions가 저장된 데이터로 GitHub Pages에 바로 배포한다(약 1분). 배포가 깨지지 않도록 푸시 전에 화면을 확인한다.
- 평일 아침 예약 실행이 `stock-dashboard/site/data.json`을 새로 받아 `main`에 직접 커밋한다. 그래서 작업 시작 전과 푸시 전에 `git pull --rebase origin main`으로 최신 상태를 받는다.
- 사이트마다 폴더 하나 (`stock-dashboard/` 등). 새 사이트를 추가하면 루트 `index.html` 목록과 `.github/workflows/deploy.yml` 의 "배포 폴더 구성" 단계에도 추가한다.
- 사용자는 한국어를 쓰고 주로 휴대폰으로 본다. 화면은 모바일 우선, 하단의 "PC 버전으로 보기"로 PC 화면 전환.

## 아이디어 노트

- 사용자의 코딩 아이디어는 왜(목적)·어떻게(방법)·상태를 함께 적어 Claude 문서 "FIT·코딩 아이디어 노트"(https://claude.ai/artifact/B4QWD9MzLzCtmfS85dbTcE)에 모은다. 작업을 시작할 때 문서 도구가 있으면 먼저 읽고, 새 아이디어나 진행 상황이 생기면 거기에 반영한다.
- 노트는 코딩 전용이다. 사업 아이디어는 코딩 아이디어와 이어져 새 기회가 보일 때만, 어디서 왔고 무엇과 이어지는지 표시해서 적는다.

## stock-dashboard

- `site/index.html` 화면 (외부 라이브러리 없음), `site/names.js` 한글 이름표, `site/data.json` 데이터 스냅샷
- `scripts/fetch_data.py` 데이터 수집 (표준 라이브러리만). 출처: SEC EDGAR(재무), Yahoo Finance(주가), Wikipedia(S&P 500 목록, 나스닥 100·다우 30 구성종목)
- 지수 소속은 종목마다 `ix` 배열(`sp`·`ndx`·`dji`). 화면의 지수 선택은 보여줄 기업만 거르고, 비교 점수는 항상 전체 기업 기준이다. 지수 목록을 못 읽은 날은 전날 `data.json`의 소속을 쓰고 수집은 계속한다.
- Yahoo 요청은 순차 + 간격을 두고, 429 응답에는 기다렸다 재시도한다. User-Agent나 호스트를 바꿔가며 제한을 우회하지 않는다.
- 로컬 실행: `python3 stock-dashboard/scripts/fetch_data.py --out stock-dashboard/site/data.json` 후 `python3 -m http.server -d stock-dashboard/site 8000`
