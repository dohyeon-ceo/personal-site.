# personal-site.
개인적으로 만들고 싶은 사이트를 모아두는 곳

## 사이트 목록

| 폴더 | 내용 |
|---|---|
| [`stock-dashboard/`](stock-dashboard/) | **FIT (Find Top)** — 미국 S&P 500 기업의 PER·성장률을 분야별로 비교하는 대시보드 (모바일 중심, 매일 자동 갱신) |
| [`youtube-script/`](youtube-script/) | **유튜브 스크립트 추출기** — 유튜브 주소를 넣으면 자막을 텍스트로 정리해 복사·저장 (개인용) |

## 배포 (GitHub Pages)

1. 저장소 **Settings → Pages → Source** 를 **GitHub Actions** 로 변경
2. **Actions** 탭 → "사이트 배포" → **Run workflow**
3. 주소: `https://dohyeon-ceo.github.io/personal-site./`

이후 `main`에 변경이 올라오면 자동으로 다시 배포되고, 주식 데이터는 평일 매일 자동 갱신된다.

새 사이트를 추가할 때는 폴더를 만들고 루트 `index.html` 목록과 `.github/workflows/deploy.yml` 의 "배포 폴더 구성" 단계에 한 줄씩 추가하면 된다.
