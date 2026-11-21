# S&P 500 한눈에 보기

미국 S&P 500 전 종목의 **PER과 재무 성장률**을 한 화면에서 보는 무료 개인 대시보드.

- 표: 주가, 1년 수익률, 시가총액, PER, 매출(TTM), 매출·순이익·EPS 성장률, 영업이익률, 3년 매출 CAGR
- 열 머리글 클릭으로 정렬, 검색, 섹터 필터, 빠른 필터(저PER / 고성장 / 성장+적정가 / 고수익)
- 종목 클릭 시 연간·분기 매출, 순이익, EPS 차트
- 다크 모드, 모바일 지원

## 데이터 (전부 무료, API 키 불필요)

| 항목 | 출처 |
|---|---|
| 재무제표 | 미국 SEC EDGAR XBRL 공시 (`data.sec.gov`) |
| 주가 | Yahoo Finance chart API |
| 종목 목록·섹터 | Wikipedia *List of S&P 500 companies* |

GitHub Actions가 평일 매일 22:17 UTC(한국 시간 다음날 오전 7시경)에 데이터를 새로 받아 GitHub Pages에 배포한다.

## 배포

저장소 루트 README의 "배포" 절 참고. 주소: `https://dohyeon-ceo.github.io/personal-site./stock-dashboard/`

(선택) Settings → Secrets and variables → Actions → Variables 에 `SEC_USER_AGENT` 를 `이름 이메일` 형식으로 등록하면 SEC 권장사항에 맞게 요청한다.

## 내 컴퓨터에서 실행

```bash
python3 stock-dashboard/scripts/fetch_data.py            # stock-dashboard/site/data.json 생성 (약 10분)
python3 -m http.server -d stock-dashboard/site 8000      # http://localhost:8000
```

## 계산 방식

- **TTM**: 최근 4개 분기 합. 4분기는 `연간 − (1~3분기)` 로 계산
- **PER**: 주가 ÷ TTM 희석 EPS (적자면 표시 안 함)
- **성장률**: TTM vs 1년 전 TTM (1년 전이 적자면 표시 안 함)
- 공시 원본을 자동 가공하므로 일부 기업(금융·리츠 등)은 값이 빠지거나 다를 수 있음. 투자 참고용.
