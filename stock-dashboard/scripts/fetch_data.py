#!/usr/bin/env python3
"""S&P 500 기업의 주가 + 재무 데이터를 모아 site/data.json 으로 저장한다.

데이터 출처 (모두 무료, API 키 불필요)
  - 종목 목록/섹터 : Wikipedia "List of S&P 500 companies"
  - 재무제표       : SEC EDGAR XBRL companyfacts API (data.sec.gov)
  - 주가           : Yahoo Finance chart API

표준 라이브러리만 사용한다 (pip install 불필요).
사용법: python3 scripts/fetch_data.py [--limit N] [--out site/data.json]
"""
from __future__ import annotations

import argparse
import gzip
import html
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone

# SEC는 User-Agent에 연락처를 요구한다. GitHub Actions 시크릿/변수로 바꿀 수 있다.
SEC_UA = os.environ.get("SEC_USER_AGENT", "PersonalStockDashboard contact@example.com")
APP_UA = "PersonalStockDashboard/1.0"
PRICE_DELAY = 0.6  # 주가 요청 사이 간격(초). 서버에 부담 주지 않도록 순차 + 간격

WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=1y&interval=1wk&events=split"

# 항목별 XBRL 태그 후보 (회사마다, 연도마다 쓰는 태그가 다르다)
CONCEPTS = {
    "rev": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "RevenuesNetOfInterestExpense",
        "SalesRevenueGoodsNet",
        "SalesRevenueServicesNet",
    ],
    "ni": ["NetIncomeLoss", "NetIncomeLossAvailableToCommonStockholdersBasic", "ProfitLoss"],
    "op": ["OperatingIncomeLoss"],
    "eps": ["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted", "EarningsPerShareBasic"],
}
FORMS = {"10-K", "10-K/A", "10-Q", "10-Q/A", "10-KT", "20-F", "40-F"}

_sec_lock = threading.Lock()
_sec_last = [0.0]


def http_get(url: str, ua: str, retries: int = 4, sec: bool = False) -> bytes:
    for attempt in range(retries):
        if sec:  # SEC 요청 한도(초당 10회)를 넘지 않도록 간격 유지
            with _sec_lock:
                wait = 0.13 - (time.time() - _sec_last[0])
                if wait > 0:
                    time.sleep(wait)
                _sec_last[0] = time.time()
        req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept-Encoding": "gzip"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                return body
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise
            err = e
            if e.code == 429:  # 요청 한도 초과: 서버가 알려준 만큼(없으면 넉넉히) 기다린다
                ra = e.headers.get("Retry-After", "")
                time.sleep(int(ra) if ra.isdigit() else 20 * (attempt + 1))
                continue
        except Exception as e:  # noqa: BLE001 - 네트워크 오류는 재시도
            err = e
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"GET 실패 {url}: {err}")


# ---------------------------------------------------------------- 종목 목록
def strip_tags(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def load_sp500() -> list[dict]:
    page = http_get(WIKI_URL, APP_UA).decode("utf-8")
    start = page.find('id="constituents"')
    end = page.find("</table>", start)
    if start < 0:
        raise RuntimeError("Wikipedia 표(constituents)를 찾지 못함")
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", page[start:end], re.S)
    out = []
    for row in rows:
        cells = [strip_tags(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if len(cells) < 7:
            continue
        out.append({"t": cells[0], "n": cells[1], "s": cells[2], "sub": cells[3], "cik": cells[6].zfill(10)})
    if len(out) < 400:
        raise RuntimeError(f"S&P 500 종목 수가 이상함: {len(out)}")
    return out


# ---------------------------------------------------------------- 재무
def d(s: str) -> date:
    return date.fromisoformat(s)


def periods_for(facts: dict, tags: list[str], unit_pred) -> dict:
    """여러 태그를 합쳐 {(start,end): value} 를 만든다.
    같은 기간이 여러 번 보고되면 가장 최근 제출본(정정/분할 반영)을 쓴다.
    최신 기간을 가진 태그를 우선하고, 빈 기간만 다른 태그로 채운다."""
    gaap = facts.get("us-gaap", {})
    per_tag = []
    for tag in tags:
        units = gaap.get(tag, {}).get("units", {})
        best: dict = {}
        for unit, items in units.items():
            if not unit_pred(unit):
                continue
            for f in items:
                if "start" not in f or f.get("form") not in FORMS:
                    continue
                k = (f["start"], f["end"])
                if k not in best or f["filed"] >= best[k][1]:
                    best[k] = (f["val"], f["filed"])
        if best:
            per_tag.append({k: v[0] for k, v in best.items()})
    if not per_tag:
        return {}
    per_tag.sort(key=lambda m: (max(e for _, e in m), len(m)), reverse=True)
    merged = dict(per_tag[0])
    covered_ends = {e for _, e in merged}
    for m in per_tag[1:]:
        for (s, e), v in m.items():
            if e not in covered_ends:
                merged[(s, e)] = v
                covered_ends.add(e)
    return merged


def split_periods(per: dict):
    """기간을 연간(약 1년)과 분기(약 3개월)로 나누고, 4분기를 연간-1~3분기로 계산한다."""
    annual, quarters = {}, {}
    for (s, e), v in per.items():
        days = (d(e) - d(s)).days
        if 340 <= days <= 390:
            annual[e] = (s, v)
        elif 80 <= days <= 100:
            if e not in quarters or s > quarters[e][0]:
                quarters[e] = (s, v)
    for e, (s, v) in annual.items():
        if e in quarters:
            continue
        inside = [(qe, qs, qv) for qe, (qs, qv) in quarters.items()
                  if (d(qs) - d(s)).days >= -7 and (d(e) - d(qe)).days >= 20]
        if len(inside) == 3:
            last_end = max(x[0] for x in inside)
            q4s = date.fromordinal(d(last_end).toordinal() + 1).isoformat()
            quarters[e] = (q4s, v - sum(x[2] for x in inside))
    return annual, quarters


def ttm(quarters: dict, end_idx_from_last: int = 0):
    """최근 4개 연속 분기 합계. end_idx_from_last=4 이면 1년 전 TTM."""
    ends = sorted(quarters)
    if len(ends) < 4 + end_idx_from_last:
        return None, None
    sel = ends[len(ends) - 4 - end_idx_from_last: len(ends) - end_idx_from_last]
    span = (d(sel[-1]) - d(quarters[sel[0]][0])).days
    if not 350 <= span <= 380:
        return None, None
    for a, b in zip(sel, sel[1:]):
        if abs((d(quarters[b][0]) - d(a)).days) > 10:
            return None, None
    return sum(quarters[e][1] for e in sel), sel[-1]


def growth(cur, prev):
    if cur is None or prev is None or prev <= 0:
        return None
    return (cur - prev) / prev


def metric_ttm(per: dict):
    """(TTM 값, 1년 전 TTM 값, 기준일, 연간 dict, 분기 dict)"""
    annual, quarters = split_periods(per)
    cur, cur_end = ttm(quarters, 0)
    prev, prev_end = ttm(quarters, 4)
    if cur is not None and prev is not None and abs((d(cur_end) - d(prev_end)).days - 365) > 25:
        prev = None
    last_annual = max(annual) if annual else None
    # 분기 자료가 없거나 연간 자료가 더 최신이면 연간 값을 사용
    if annual and (cur is None or last_annual > cur_end):
        ys = sorted(annual)
        cur, cur_end = annual[ys[-1]][1], ys[-1]
        prev = annual[ys[-2]][1] if len(ys) > 1 and 350 <= (d(ys[-1]) - d(ys[-2])).days <= 380 else None
    return cur, prev, cur_end, annual, quarters


def shares_outstanding(facts: dict, ref: str | None):
    """발행주식수. 표지(dei) 값이 최신이면 그것을, 아니면 최근 분기 희석주식수를 쓴다."""
    items = facts.get("dei", {}).get("EntityCommonStockSharesOutstanding", {}).get("units", {}).get("shares", [])
    if items:
        latest = max(items, key=lambda f: (f["filed"], f["end"]))
        if ref is None or abs((d(latest["end"]) - d(ref)).days) <= 200:
            same = {}
            for f in items:  # 같은 제출본의 여러 주식 클래스를 합산
                if f["accn"] == latest["accn"]:
                    same[(f["end"], f["val"])] = f["val"]
            return sum(same.values()) or None
    wa = facts.get("us-gaap", {}).get("WeightedAverageNumberOfDilutedSharesOutstanding", {}).get("units", {}).get("shares", [])
    wa = [f for f in wa if f.get("form") in FORMS]
    if wa:
        latest = max(wa, key=lambda f: (f["end"], f["filed"]))
        if ref is None or abs((d(latest["end"]) - d(ref)).days) <= 200:
            return latest["val"]
    return None


def fresh(m, ref: str | None, days: int = 120):
    """기준일보다 오래된(=더 이상 보고하지 않는 태그) 값은 버린다."""
    cur, prev, end, annual, quarters = m
    if end is None or ref is None or (d(ref) - d(end)).days > days:
        return None, None, None, annual, quarters
    return m


def build_financials(facts: dict) -> dict:
    usd = lambda u: u == "USD"
    eps_unit = lambda u: u.startswith("USD/sh")
    rev = metric_ttm(periods_for(facts, CONCEPTS["rev"], usd))
    ni = metric_ttm(periods_for(facts, CONCEPTS["ni"], usd))
    ref = max([x for x in (rev[2], ni[2]) if x], default=None)
    rev, ni = fresh(rev, ref), fresh(ni, ref)
    op = fresh(metric_ttm(periods_for(facts, CONCEPTS["op"], usd)), ref)
    eps = metric_ttm(periods_for(facts, CONCEPTS["eps"], eps_unit))
    # EPS는 순이익과 같은 기간일 때만 사용 (태그/클래스가 꼬인 회사 방지)
    if ni[2] is None or eps[2] is None or abs((d(eps[2]) - d(ni[2])).days) > 10:
        eps = (None, None, None, eps[3], eps[4])

    out = {
        "rev": rev[0], "revG": growth(rev[0], rev[1]),
        "ni": ni[0], "niG": growth(ni[0], ni[1]),
        "op": op[0],
        "eps": eps[0], "epsG": growth(eps[0], eps[1]),
        "asOf": ref,
        "shares": shares_outstanding(facts, ref),
    }
    out["netM"] = ni[0] / rev[0] if ni[0] is not None and rev[0] else None
    out["opM"] = op[0] / rev[0] if op[0] is not None and rev[0] else None

    # 3년 매출 연평균 성장률 (연간 기준)
    ra = rev[3]
    ys = sorted(ra)
    out["rev3y"] = None
    if len(ys) >= 4:
        a, b = ra[ys[-1]][1], ra[ys[-4]][1]
        if b > 0 and a > 0 and 1000 <= (d(ys[-1]) - d(ys[-4])).days <= 1130:
            out["rev3y"] = (a / b) ** (1 / 3) - 1

    # 차트용 연간 시계열 (최근 10년)
    def annual_series(annual):
        return {e: v for e, (s, v) in annual.items()}
    ar, an, ae = annual_series(rev[3]), annual_series(ni[3]), annual_series(eps[3])
    years = sorted(set(ar) | set(an))[-10:]
    out["annual"] = {"end": years, "rev": [ar.get(y) for y in years],
                     "ni": [an.get(y) for y in years], "eps": [ae.get(y) for y in years]}
    # 차트용 분기 시계열 (최근 12분기)
    qr = {e: v for e, (s, v) in rev[4].items()}
    qn = {e: v for e, (s, v) in ni[4].items()}
    qs = sorted(set(qr) | set(qn))[-12:]
    out["quarterly"] = {"end": qs, "rev": [qr.get(q) for q in qs], "ni": [qn.get(q) for q in qs]}
    return out


# ---------------------------------------------------------------- 주가
def fetch_price(ticker: str) -> dict:
    sym = ticker.replace(".", "-")
    data = json.loads(http_get(CHART_URL.format(sym=sym), APP_UA))
    res = data["chart"]["result"][0]
    meta = res["meta"]
    closes = [c for c in res["indicators"]["quote"][0].get("close", []) if c is not None]
    price = meta.get("regularMarketPrice") or (closes[-1] if closes else None)
    return {
        "price": price,
        "chg1y": (price / closes[0] - 1) if price and closes and closes[0] else None,
        "hi52": meta.get("fiftyTwoWeekHigh"),
        "spark": [round(c, 2) for c in closes],
        # 최근 1년 주식분할: [(날짜, 비율)] — 공시 이후 분할이면 EPS를 보정한다
        "splits": [(datetime.fromtimestamp(v["date"], timezone.utc).date().isoformat(), v["numerator"] / v["denominator"])
                   for v in (res.get("events") or {}).get("splits", {}).values() if v.get("denominator")],
    }


# ---------------------------------------------------------------- 조립
def rnd(x, n=4):
    if x is None:
        return None
    if isinstance(x, float):
        return round(x, n)
    return x


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="테스트용: 앞에서 N개 종목만")
    ap.add_argument("--out", default="site/data.json")
    args = ap.parse_args()

    t0 = time.time()
    companies = load_sp500()
    if args.limit:
        companies = companies[: args.limit]
    print(f"종목 {len(companies)}개", flush=True)

    ciks = sorted({c["cik"] for c in companies})
    fin: dict[str, dict] = {}

    def do_fin(cik):
        try:
            facts = json.loads(http_get(FACTS_URL.format(cik=cik), SEC_UA, sec=True))["facts"]
            fin[cik] = build_financials(facts)
        except Exception as e:  # noqa: BLE001
            print(f"  [재무 실패] CIK {cik}: {e}", file=sys.stderr, flush=True)

    with ThreadPoolExecutor(6) as ex:
        for i, _ in enumerate(ex.map(do_fin, ciks), 1):
            if i % 50 == 0:
                print(f"  재무 {i}/{len(ciks)}", flush=True)

    prices: dict[str, dict] = {}

    def do_price(t):
        try:
            prices[t] = fetch_price(t)
        except Exception as e:  # noqa: BLE001
            print(f"  [주가 실패] {t}: {e}", file=sys.stderr, flush=True)

    for i, c in enumerate(companies, 1):  # 순차 요청 + 간격
        do_price(c["t"])
        time.sleep(PRICE_DELAY)
        if i % 100 == 0:
            print(f"  주가 {i}/{len(companies)}", flush=True)
    print(f"  주가 {len(prices)}/{len(companies)}", flush=True)

    rows = []
    for c in companies:
        f = fin.get(c["cik"], {})
        p = prices.get(c["t"], {})
        price = p.get("price")
        eps = f.get("eps")
        shares = f.get("shares")
        for sd, ratio in p.get("splits", []):
            if f.get("asOf") and sd > f["asOf"] and ratio > 0:
                if eps is not None:
                    eps = eps / ratio
                if shares:
                    shares = shares * ratio
        # EPS와 순이익/주식수가 크게 어긋나면(클래스가 여러 개인 회사 등) 신뢰하지 않음
        if eps and shares and f.get("ni") and f["ni"] > 0:
            implied = f["ni"] / shares
            if not 0.5 <= eps / implied <= 2:
                eps = None
        pe = price / eps if price and eps and eps > 0 else None
        if pe is None and eps is None and price and shares and f.get("ni") and f["ni"] > 0:
            pe = price * shares / f["ni"]  # EPS 태그가 없는 회사: 시가총액 / 순이익
        mcap = None
        if price and f.get("ni") and pe:
            mcap = pe * f["ni"]  # 순이익 × PER = 시가총액 (주식 클래스 문제 회피)
        elif price and shares:
            mcap = price * shares
        if mcap is not None and mcap < 2e9:  # S&P 500 기업이 20억 달러 미만일 리 없음 → 주식수 오류
            pe = mcap = None
        rows.append({
            "t": c["t"], "n": c["n"], "s": c["s"], "sub": c["sub"],
            "price": rnd(price, 2), "chg1y": rnd(p.get("chg1y")), "hi52": rnd(p.get("hi52"), 2),
            "mcap": round(mcap) if mcap else None,
            "pe": rnd(pe, 2), "eps": rnd(eps, 3), "epsG": rnd(f.get("epsG")),
            "rev": f.get("rev"), "revG": rnd(f.get("revG")), "rev3y": rnd(f.get("rev3y")),
            "ni": f.get("ni"), "niG": rnd(f.get("niG")),
            "netM": rnd(f.get("netM")), "opM": rnd(f.get("opM")),
            "asOf": f.get("asOf"),
            "annual": f.get("annual"), "quarterly": f.get("quarterly"),
            "spark": p.get("spark"),
        })

    payload = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "count": len(rows),
        "companies": rows,
    }
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
    ok_fin = sum(1 for r in rows if r["rev"] is not None)
    ok_pe = sum(1 for r in rows if r["pe"] is not None)
    print(f"완료: {len(rows)}개, 매출 {ok_fin}, PER {ok_pe}, {time.time() - t0:.0f}초 -> {args.out}")
    # 대부분 실패했다면 배포하지 않도록 오류 종료
    return 0 if ok_fin >= 0.8 * len(rows) and len(prices) >= 0.8 * len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
