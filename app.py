import itertools, random, time
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import plotly.express as px
import requests
import streamlit as st

st.set_page_config(page_title="로또 통계 분석 Pro", page_icon="🎱", layout="wide")
ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
CSV = DATA / "lotto_history.csv"
LOG = DATA / "update_log.csv"
BASE_API = "https://www.dhlottery.co.kr/lt645"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36",
    "Referer": "https://www.dhlottery.co.kr/lt645/result",
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json, text/plain, */*",
}
DRAW_CACHE = {}
LAST_API_ERROR = ""

NC = [f"번호{i}" for i in range(1, 7)]
COLS = ["회차", "추첨일", *NC, "보너스"]

def _get_new_api_list(url, params=None):
    """동행복권 개편 후 JSON 응답을 읽고 응답 형식을 확인한다."""
    r = requests.get(url, params=params, timeout=15, headers=HEADERS)
    r.raise_for_status()
    content_type = r.headers.get("Content-Type", "").lower()
    if "json" not in content_type:
        raise RuntimeError(
            f"JSON이 아닌 응답을 받았습니다 (HTTP {r.status_code}, "
            f"Content-Type={content_type or '없음'}). 접속 차단/대기 페이지일 수 있습니다."
        )
    payload = r.json()
    rows = payload.get("data", {}).get("list", [])
    if not isinstance(rows, list):
        raise RuntimeError("새 API 응답에 data.list 목록이 없습니다.")
    return rows

def _convert_new_draw(item):
    """새 API 필드명을 앱 내부의 기존 CSV 열 이름으로 변환한다."""
    try:
        draw_no = int(item["ltEpsd"])
        raw_date = str(item["ltRflYmd"])
        if len(raw_date) == 8 and raw_date.isdigit():
            draw_date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}"
        else:
            draw_date = datetime.strptime(raw_date[:10], "%Y-%m-%d").strftime("%Y-%m-%d")
        numbers = [int(item[f"tm{i}WnNo"]) for i in range(1, 7)]
        bonus = int(item["bnsWnNo"])
        if len(set(numbers)) != 6 or not all(1 <= n <= 45 for n in numbers) or not 1 <= bonus <= 45:
            return None
        return {"회차": draw_no, "추첨일": draw_date,
                **{f"번호{i+1}": numbers[i] for i in range(6)}, "보너스": bonus}
    except (KeyError, TypeError, ValueError):
        return None

def fetch_draw(n):
    """특정 회차를 새 API에서 조회한다. 한 응답의 주변 회차도 캐시에 저장한다."""
    global LAST_API_ERROR
    n = int(n)
    if n in DRAW_CACHE:
        return DRAW_CACHE[n]
    try:
        rows = _get_new_api_list(
            f"{BASE_API}/selectPstLt645InfoNew.do",
            {"srchDir": "center", "srchLtEpsd": n},
        )
        for item in rows:
            row = _convert_new_draw(item)
            if row:
                DRAW_CACHE[row["회차"]] = row
        return DRAW_CACHE.get(n)
    except Exception as exc:
        LAST_API_ERROR = f"{type(exc).__name__}: {exc}"
        return None

def latest_round():
    """최신 추첨 결과 조회 API로 발표된 최신 회차를 확인한다."""
    global LAST_API_ERROR
    try:
        rows = _get_new_api_list(f"{BASE_API}/selectPstLt645Info.do")
        converted = [_convert_new_draw(item) for item in rows]
        converted = [r for r in converted if r]
        if not converted:
            raise RuntimeError("최신 회차 응답에서 당첨번호를 찾지 못했습니다.")
        for row in converted:
            DRAW_CACHE[row["회차"]] = row
        LAST_API_ERROR = ""
        return max(row["회차"] for row in converted)
