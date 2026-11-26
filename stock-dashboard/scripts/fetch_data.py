#!/usr/bin/env python3
"""S&P 500 (+ 나스닥 100·다우 30) 기업의 주가 + 재무 데이터를 모아 site/data.json 으로 저장한다.

데이터 출처 (모두 무료, API 키 불필요)
  - 종목 목록/섹터 : Wikipedia "List of S&P 500 companies"
  - 지수 소속      : Nasdaq 공식 목록·ETF 운용사 보유종목 파일(QQQ, DIA), 안 되면 Wikipedia 표
                     (못 읽으면 전날 data.json 의 소속 정보를 그대로 쓴다)
  - 종목 → CIK     : SEC company_tickers.json (S&P 500 밖의 나스닥 100 종목용)
  - 재무제표       : SEC EDGAR XBRL companyfacts API (data.sec.gov)
  - 주가           : Yahoo Finance chart API

표준 라이브러리만 사용한다 (pip install 불필요).
사용법: python3 scripts/fetch_data.py [--limit N] [--out site/data.json]
"""
from __future__ import annotations

import argparse
import csv
import gzip
import html
import io
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

# SEC는 User-Agent에 연락처를 요구한다. GitHub Actions 시크릿/변수로 바꿀 수 있다.
SEC_UA = os.environ.get("SEC_USER_AGENT", "PersonalStockDashboard contact@example.com")
APP_UA = "PersonalStockDashboard/1.0"
PRICE_DELAY = 0.6  # 주가 요청 사이 간격(초). 서버에 부담 주지 않도록 순차 + 간격

WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
# 지수별 구성종목 출처(앞에서부터 시도)와, 정상이라면 나와야 할 종목 수 범위
#  - 지수를 그대로 따라가는 ETF의 운용사 공식 보유종목 파일: QQQ(나스닥 100), DIA(다우 30)
#  - 위키백과 구성종목 표 (문서에서 표가 빠지는 경우가 있어 보조로만 쓴다)
INDEX_SOURCES = {
    "ndx": {"name": "나스닥 100", "rows": (90, 110), "urls": [
        ("json", "https://api.nasdaq.com/api/quote/list-type/nasdaq100"),  # 지수 운영사(Nasdaq) 공식 목록
        ("csv", "https://www.invesco.com/us/financial-products/etfs/holdings/main/holdings/0?audienceType=Investor&action=download&ticker=QQQ"),
        ("html", "https://it.wikipedia.org/wiki/NASDAQ-100"),
        ("html", "https://de.wikipedia.org/wiki/NASDAQ-100"),
        ("html", "https://en.wikipedia.org/wiki/Nasdaq-100"),
    ]},
    "dji": {"name": "다우 30", "rows": (28, 32), "urls": [
        ("xlsx", "https://www.ssga.com/us/en/intermediary/etfs/library-content/products/fund-data/etfs/us/holdings-daily-us-en-dia.xlsx"),
        ("html", "https://en.wikipedia.org/wiki/Dow_Jones_Industrial_Average"),
    ]},
}
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
# 3년치 주간 종가: 최근 1년은 스파크라인, 그 이전은 분기별 점수 추이(그때 PER) 계산에 쓴다
CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=3y&interval=1wk&events=split"

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


# ---------------------------------------------------------------- 지수 소속 (나스닥 100 · 다우 30)
TICKER_RE = re.compile(r"^[A-Z][A-Z0-9]{0,5}(\.[A-Z0-9]{1,2})?$")


def norm_ticker(s: str) -> str:
    """'NYSE: MMM', 'GOOGL[1]', 'brk-b' 같은 표기를 S&P 500 표와 같은 'BRK.B' 꼴로 맞춘다."""
    s = re.sub(r"\[[^\]]*\]", "", s).split(":")[-1].strip()
    s = s.split()[0].upper().replace("-", ".") if s else ""
    return s if TICKER_RE.match(s) else ""


def html_tables(page: str):
    """문서 안의 표를 [[칸, …], …] 으로. 행 머리(<th>)도 칸으로 센다."""
    for m in re.finditer(r"<table[^>]*>(.*?)</table>", page, re.S):
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", m.group(1), re.S)
        grid = [[strip_tags(c) for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", r, re.S)] for r in rows]
        yield [g for g in grid if g]


def csv_tables(body: bytes):
    text = body.decode("utf-8-sig", errors="replace")
    yield [row for row in csv.reader(io.StringIO(text)) if any(c.strip() for c in row)]


def xlsx_tables(body: bytes):
    """표준 라이브러리만으로 .xlsx 의 시트들을 [[칸, …], …] 으로 읽는다."""
    m_ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    with zipfile.ZipFile(io.BytesIO(body)) as z:
        names = z.namelist()
        shared = []
        if "xl/sharedStrings.xml" in names:
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(m_ns + "si"):
                shared.append("".join(t.text or "" for t in si.iter(m_ns + "t")))
        for name in sorted(n for n in names if re.match(r"xl/worksheets/sheet\d+\.xml$", n)):
            grid = []
            for row in ET.fromstring(z.read(name)).iter(m_ns + "row"):
                cells = {}
                for c in row.iter(m_ns + "c"):
                    col = re.match(r"[A-Z]*", c.get("r", "")).group(0) or "A"
                    idx = 0
                    for ch in col:
                        idx = idx * 26 + ord(ch) - 64
                    v, inline = c.find(m_ns + "v"), c.find(m_ns + "is")
                    if c.get("t") == "s" and v is not None:
                        val = shared[int(v.text)]
                    elif inline is not None:
                        val = "".join(t.text or "" for t in inline.iter(m_ns + "t"))
                    else:
                        val = v.text if v is not None else ""
                    cells[idx - 1] = (val or "").strip()
                if cells:
                    grid.append([cells.get(i, "") for i in range(max(cells) + 1)])
            yield grid


def json_tables(body: bytes):
    """JSON 안에서 같은 모양의 객체가 줄지어 있는 목록을 찾아 [[머리글…], [값…], …] 으로."""
    def walk(x):
        if isinstance(x, list) and x and all(isinstance(i, dict) for i in x):
            keys = list(x[0])
            yield [keys] + [[str(i.get(k, "") or "") for k in keys] for i in x]
        elif isinstance(x, dict):
            for v in x.values():
                yield from walk(v)
        elif isinstance(x, list):
            for v in x:
                yield from walk(v)
    yield from walk(json.loads(body))


def pick_col(header: list[str], pattern: str):
    for i, h in enumerate(header):
        if re.search(pattern, h, re.I):
            return i
    return None


def members_from_grid(grid: list[list[str]], lo: int, hi: int):
    """위쪽 20줄 안에서 티커 칸이 있는 머리글 줄을 찾아 {티커: {"n", "s", "sub"}}.
    표 모양이 달라도 머리글 이름으로 칸을 찾는다. 종목 수가 정상 범위가 아니면 None."""
    for h, head in enumerate(grid[:20]):
        ti = pick_col(head, r"^\s*(holding\s+)?(ticker|symbol|simbolo|kürzel)")
        if ti is None:
            continue
        ni = pick_col(head, r"^\s*(company|security|name|società|unternehmen)")
        si = pick_col(head, r"gics\s*sector|^\s*sector\s*$")
        ui = pick_col(head, r"gics\s*sub")
        cell = lambda row, i: row[i].strip() if i is not None and i < len(row) else ""  # noqa: E731
        out = {}
        for row in grid[h + 1:]:
            t = norm_ticker(cell(row, ti))
            if t:
                out[t] = {"n": cell(row, ni), "s": cell(row, si), "sub": cell(row, ui)}
        if lo <= len(out) <= hi:
            return out
    return None


def load_index(src: dict) -> dict | None:
    """출처를 차례로 시도해 {티커: {"n", "s", "sub"}}. 모두 실패하면 None (이유는 로그에 남긴다)."""
    lo, hi = src["rows"]
    readers = {"html": lambda b: html_tables(b.decode("utf-8", errors="replace")), "csv": csv_tables, "xlsx": xlsx_tables,
               "json": json_tables}
    for kind, url in src["urls"]:
        host = url.split("/")[2]
        try:
            grids = list(readers[kind](http_get(url, APP_UA, retries=2)))
        except Exception as e:  # noqa: BLE001
            print(f"  [{src['name']}] {host}: 읽기 실패 ({str(e)[:150]})", file=sys.stderr, flush=True)
            continue
        for grid in grids:
            got = members_from_grid(grid, lo, hi)
            if got:
                print(f"  [{src['name']}] {host}에서 {len(got)}개 읽음", flush=True)
                return got
        heads = [" | ".join(g[0][:6])[:90] for g in grids if g][:4]
        print(f"  [{src['name']}] {host}: 맞는 표 없음 (표 {len(grids)}개, 머리글 예: {heads})", file=sys.stderr, flush=True)
    return None


def previous_data(path: str) -> tuple[dict, dict]:
    """전날 data.json 에서 (지수별 소속 티커, 티커별 이름·섹터). 목록을 못 읽은 날의 대비책."""
    try:
        with open(path, encoding="utf-8") as fh:
            old = json.load(fh)
    except Exception:  # noqa: BLE001
        return {}, {}
    members: dict[str, set] = {}
    info = {}
    for r in old.get("companies", []):
        info[r["t"]] = {"n": r.get("n", ""), "s": r.get("s", ""), "sub": r.get("sub", "")}
        for k in r.get("ix") or []:
            members.setdefault(k, set()).add(r["t"])
    return members, info


def load_memberships(companies: list[dict], prev_path: str) -> tuple[dict, list[dict]]:
    """지수별 소속 티커 집합과, S&P 500 밖이라 새로 넣을 종목 목록을 만든다.
    새 종목은 SEC CIK와 GICS 섹터를 알 수 있을 때만 넣는다 (업종 비교가 가능해야 하므로)."""
    sp = {c["t"] for c in companies}
    sectors = {c["s"] for c in companies}
    prev_members, prev_info = previous_data(prev_path)
    members = {"sp": set(sp)}
    extra_info = {}
    for key, src in INDEX_SOURCES.items():
        got = load_index(src)
        if got is None:
            old = prev_members.get(key, set())
            got = {t: prev_info.get(t, {}) for t in old}
            print(f"  {src['name']}: 목록을 못 읽어 이전 소속 정보 사용 ({len(got)}개)", flush=True)
        else:
            print(f"  {src['name']}: {len(got)}개 (S&P 500과 겹침 {len(set(got) & sp)}개)", flush=True)
        members[key] = set(got)
        for t, info in got.items():
            if t not in sp:  # 표에 섹터가 없으면 전날 알던 섹터로 채운다
                filled = {**prev_info.get(t, {}), **{k: v for k, v in info.items() if v}}
                extra_info.setdefault(t, filled)

    extras = []
    if extra_info:
        try:
            raw = json.loads(http_get(TICKERS_URL, SEC_UA, sec=True))
            ciks = {v["ticker"].upper().replace("-", "."): str(v["cik_str"]).zfill(10) for v in raw.values()}
        except Exception as e:  # noqa: BLE001
            print(f"  [CIK 목록 실패] {e}", file=sys.stderr, flush=True)
            ciks = {}
        for t, info in sorted(extra_info.items()):
            cik = ciks.get(t)
            if not cik or info.get("s") not in sectors:
                print(f"  [추가 제외] {t}: CIK 또는 GICS 섹터를 알 수 없음", flush=True)
                continue
            extras.append({"t": t, "n": info.get("n") or t, "s": info["s"], "sub": info.get("sub") or info["s"],
                           "cik": cik, "extra": True})
    return members, extras


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
    # 점수 추이 계산용 (data.json에는 넣지 않음)
    out["_q"] = {"rev": rev[4], "ni": ni[4], "op": op[4], "eps": eps[4]}
    return out


# ---------------------------------------------------------------- 주가
def fetch_price(ticker: str) -> dict:
    sym = ticker.replace(".", "-")
    data = json.loads(http_get(CHART_URL.format(sym=sym), APP_UA))
    res = data["chart"]["result"][0]
    meta = res["meta"]
    raw = res["indicators"]["quote"][0].get("close", [])
    hist = [(datetime.fromtimestamp(ts, timezone.utc).date(), c) for ts, c in zip(res.get("timestamp", []), raw) if c is not None]
    closes = [c for _, c in hist]
    year = closes[-53:]  # 최근 1년(주간 53개)
    price = meta.get("regularMarketPrice") or (closes[-1] if closes else None)
    return {
        "price": price,
        "chg1y": (price / year[0] - 1) if price and year and year[0] else None,
        "hi52": meta.get("fiftyTwoWeekHigh"),
        "spark": [round(c, 2) for c in year],
        "hist": hist,
        # 최근 1년 주식분할: [(날짜, 비율)] — 공시 이후 분할이면 EPS를 보정한다
        "splits": [(datetime.fromtimestamp(v["date"], timezone.utc).date().isoformat(), v["numerator"] / v["denominator"])
                   for v in (res.get("events") or {}).get("splits", {}).values() if v.get("denominator")],
    }


# ---------------------------------------------------------------- 점수 추이 (분기별)
# 화면(index.html prepare)과 같은 방식으로 과거 분기마다 비교 점수를 다시 계산한다.
#   - 분기 말 시점의 최근 4분기 합(TTM) 실적과 그 분기 말 주가를 쓴다
#   - 회사마다 결산 분기가 달라서, 분기 말 날짜를 가장 가까운 달력 분기(3·6·9·12월 말)에 맞춘다
#   - 같은 업종 그룹(세부분야 5개 이상이면 세부분야, 아니면 섹터) 안에서 순위 → 0~100점 → 평균
HIST_QUARTERS = 8
METRIC_DIR = {"pe": -1, "revG": 1, "epsG": 1, "opM": 1}  # PER은 낮을수록, 나머지는 높을수록 1위


def cal_quarter(e: str) -> str:
    dt = d(e)
    cands = [date(dt.year - 1, 12, 31)] + [date(dt.year, m, dd) for m, dd in ((3, 31), (6, 30), (9, 30), (12, 31))]
    q = min(cands, key=lambda x: abs((x - dt).days))
    return f"{q.year}Q{(q.month - 1) // 3 + 1}"


def ttm_by_end(quarters: dict) -> dict:
    """{분기말: 그 분기까지 연속 4분기 합}"""
    ends = sorted(quarters)
    out = {}
    for i in range(3, len(ends)):
        sel = ends[i - 3: i + 1]
        if not 350 <= (d(sel[-1]) - d(quarters[sel[0]][0])).days <= 380:
            continue
        if any(abs((d(quarters[b][0]) - d(a)).days) > 10 for a, b in zip(sel, sel[1:])):
            continue
        out[sel[-1]] = sum(quarters[e][1] for e in sel)
    return out


def year_before(series: dict, e: str):
    """1년 전(±25일) 분기의 값"""
    target = d(e).toordinal() - 365
    best = min(series, key=lambda x: abs(d(x).toordinal() - target), default=None)
    if best is None or abs(d(best).toordinal() - target) > 25:
        return None
    return series[best]


def price_at(hist: list, e: str):
    """분기 말 직전(최대 10일 전)의 주간 종가"""
    dt = d(e)
    cand = [c for day, c in hist if day <= dt + timedelta(days=3) and (dt - day).days <= 10]
    return cand[-1] if cand else None


def add_score_history(rows: list, fin_by_t: dict, prices: dict) -> None:
    # 1) 회사별 · 달력 분기별 지표 값
    vals = {}  # t -> {cq: {pe, revG, epsG, opM, loss}}
    for r in rows:
        q = (fin_by_t.get(r["t"]) or {}).get("_q")
        if not q:
            continue
        tr, tn, to, te = (ttm_by_end(q[k]) for k in ("rev", "ni", "op", "eps"))
        hist = (prices.get(r["t"]) or {}).get("hist") or []
        # 지금 주식 수(분할 반영) = 시가총액 / 주가 → 과거 주가(분할 조정됨)와 단위가 맞는다
        shares_now = r["mcap"] / r["price"] if r.get("mcap") and r.get("price") else None
        per_q = {}
        for e in sorted(tr)[-HIST_QUARTERS:]:
            rev, ni = tr.get(e), tn.get(e)
            v = {"loss": ni is not None and ni < 0}
            prev_rev = year_before(tr, e)
            v["revG"] = (rev - prev_rev) / prev_rev if rev is not None and prev_rev and prev_rev > 0 else None
            v["opM"] = to[e] / rev if e in to and rev else None
            niG = None
            prev_ni = year_before(tn, e)
            if ni is not None and prev_ni and prev_ni > 0:
                niG = (ni - prev_ni) / prev_ni
            epsG = None
            if e in te:
                prev_eps = year_before(te, e)
                if prev_eps and prev_eps > 0:
                    epsG = (te[e] - prev_eps) / prev_eps
            # 주식분할 등으로 EPS 비교가 어긋나면 순이익 성장으로 대신
            if epsG is not None and niG is not None and not 0.5 <= (1 + epsG) / max(1e-9, 1 + niG) <= 2:
                epsG = niG
            v["epsG"] = epsG if epsG is not None else niG
            px = price_at(hist, e)
            v["pe"] = px * shares_now / ni if px and shares_now and ni and ni > 0 else None
            if v["pe"] is not None and not 0 < v["pe"] < 2000:
                v["pe"] = None
            per_q[cal_quarter(e)] = v
        vals[r["t"]] = per_q

    # 2) 업종 그룹 (화면과 같은 규칙)
    sub_n = {}
    for r in rows:
        sub_n[r["sub"]] = sub_n.get(r["sub"], 0) + 1
    groups = {}
    for r in rows:
        groups.setdefault(r["sub"] if sub_n[r["sub"]] >= 5 else r["s"], []).append(r["t"])

    # 3) 그룹 · 분기마다 순위 → 점수
    pts = {}  # (t, cq) -> {metric: 0~100}
    for members in groups.values():
        qs = {cq for t in members for cq in vals.get(t, {})}
        for cq in qs:
            have = [t for t in members if cq in vals.get(t, {})]
            for m, sign in METRIC_DIR.items():
                xs = sorted((t for t in have if vals[t][cq][m] is not None), key=lambda t: vals[t][cq][m] * -sign)
                n = len(xs)
                for i, t in enumerate(xs):
                    pts.setdefault((t, cq), {})[m] = round((1 - i / (n - 1) if n > 1 else 0.5) * 100)
                if m == "pe":
                    for t in have:
                        if vals[t][cq]["pe"] is None and vals[t][cq]["loss"]:
                            pts.setdefault((t, cq), {})["pe"] = 0

    # 4) 회사별로 정리해서 data.json에 넣는다
    all_q = sorted({cq for v in vals.values() for cq in v})[-HIST_QUARTERS:]
    for r in rows:
        v = vals.get(r["t"])
        if not v:
            continue
        qs = [cq for cq in all_q if cq in v]
        if not qs:
            continue
        sh = {"q": qs, "s": [], "p": {m: [] for m in METRIC_DIR}, "v": {m: [] for m in METRIC_DIR}}
        for cq in qs:
            p = pts.get((r["t"], cq), {})
            sc = [p[m] for m in METRIC_DIR if m in p]
            sh["s"].append(round(sum(sc) / len(sc)) if len(sc) >= 2 else None)
            for m in METRIC_DIR:
                sh["p"][m].append(p.get(m))
                sh["v"][m].append(rnd(v[cq][m], 4) if v[cq][m] is not None else None)
        if any(x is not None for x in sh["s"]):
            r["sh"] = sh


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
    members, extras = load_memberships(companies, args.out)
    companies += extras
    if args.limit:
        companies = companies[: args.limit]
    print(f"종목 {len(companies)}개 (S&P 500 밖 추가 {len(extras)}개)", flush=True)

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
    fin_by_t: dict[str, dict] = {}
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
        if mcap is not None and mcap < 2e9:  # 지수 편입 대형주가 20억 달러 미만일 리 없음 → 주식수 오류
            pe = mcap = None
        if c.get("extra") and f.get("rev") is None and f.get("ni") is None:
            # 미국 회계기준(us-gaap) 공시가 없는 외국 기업 등: 비교할 숫자가 없으니 넣지 않는다
            print(f"  [추가 제외] {c['t']}: 미국 공시 재무 데이터 없음", flush=True)
            continue
        fin_by_t[c["t"]] = f
        rows.append({
            "_cik": c["cik"],
            "t": c["t"], "n": c["n"], "s": c["s"], "sub": c["sub"],
            "ix": [k for k in ("sp", "ndx", "dji") if c["t"] in members.get(k, ())],
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

    add_score_history(rows, fin_by_t, prices)
    for r in rows:
        r.pop("_cik", None)

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
