import os,time
from pathlib import Path
from datetime import date,datetime,timedelta
import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
CSV = DATA / "lotto_history.csv"
LOG = DATA / "update_log.csv"
# GitHub에 data/lotto_history.csv를 포함하면 Cloud 재배포 후에도 초기 데이터를 복구할 수 있습니다.
BUNDLED_CSV = ROOT / "lotto_history.csv"
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
WIN_COLS = ["1등 당첨 게임 수", "자동", "수동", "반자동"]
ALL_COLS = COLS + WIN_COLS

def winner_counts(values):
    try:
        counts = [float(v) for v in values]
        if not all(v.is_integer() and v >= 0 for v in counts): return {}
        total, auto, manual, semi = map(int, counts)
        if total != auto + manual + semi: return {}
        return dict(zip(WIN_COLS, [total, auto, manual, semi]))
    except (TypeError, ValueError, OverflowError):
        return {}

def _api_session():
    session = requests.Session()
    retry = Retry(total=2, connect=2, read=1, backoff_factor=0.7,
                  status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=frozenset(["GET"]), respect_retry_after_header=True)
    adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4)
    session.mount("https://", adapter)
    return session

API_SESSION = _api_session()

def _get_new_api_list(url, params=None):
    """짧은 연결 제한 + 제한된 재시도 + 응답 구조 검증."""
    r = API_SESSION.get(url, params=params, timeout=(5, 15), headers=HEADERS)
    r.raise_for_status()
    if "json" not in r.headers.get("Content-Type", "").lower():
        raise RuntimeError(f"JSON이 아닌 응답: HTTP {r.status_code} / {r.headers.get('Content-Type', '없음')}")
    payload = r.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise RuntimeError("동행복권 API 응답 구조가 변경되었을 수 있습니다.")
    rows = payload["data"].get("list")
    if not isinstance(rows, list):
        raise RuntimeError("API 응답에 data.list 목록이 없습니다.")
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
        if len(set(numbers)) != 6 or not all(1 <= n <= 45 for n in numbers) or not 1 <= bonus <= 45 or bonus in numbers:
            return None
        return {"회차": draw_no, "추첨일": draw_date,
                **{f"번호{i+1}": numbers[i] for i in range(6)}, "보너스": bonus, **winner_counts([item.get(k) for k in ("rnk1WnNope", "winType1", "winType2", "winType3")])}
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
    except Exception as exc:
        LAST_API_ERROR = f"{type(exc).__name__}: {exc}"
        return 0

def load_data():
    """로컬 데이터 우선, 없으면 GitHub에 포함된 초기 CSV 사용."""
    for path in (CSV, BUNDLED_CSV):
        if path.exists():
            try:
                frame = pd.read_csv(path, dtype={"추첨일": str})
                if set(COLS).issubset(frame.columns) and not frame.empty:
                    return frame
            except (OSError, pd.errors.ParserError, UnicodeError):
                continue
    return pd.DataFrame(columns=ALL_COLS)

def save_data(frame):
    """원자적 교체로 업데이트 도중 파일 손상을 방지."""
    tmp = CSV.with_suffix(".tmp")
    frame.to_csv(tmp, index=False, encoding="utf-8-sig")
    os.replace(tmp, CSV)

def validate(raw):
    issues=[]; rows=[]
    if raw is None or raw.empty: return pd.DataFrame(columns=ALL_COLS), pd.DataFrame(columns=["행","문제"])
    if not set(COLS).issubset(raw.columns): return pd.DataFrame(columns=ALL_COLS), pd.DataFrame([{"행":"-","문제":"필수 열 누락"}])
    for i, (_, r) in enumerate(raw.iterrows(), 2):
        try:
            vals=[float(r[c]) for c in ["회차",*NC,"보너스"]]
            if not all(v.is_integer() for v in vals): raise ValueError()
            n,*balls=map(int,vals)
            if n<1 or len(set(balls))!=7 or not all(1<=v<=45 for v in balls): raise ValueError()
            d=datetime.strptime(str(r["추첨일"]),"%Y-%m-%d").strftime("%Y-%m-%d")
            rows.append({**dict(zip(COLS,[n,d,*sorted(balls[:6]),balls[6]])), **winner_counts([r.get(c) for c in WIN_COLS])})
        except (ValueError,TypeError,OverflowError): issues.append({"행":i,"문제":"회차/날짜/번호 오류"})
    df=pd.DataFrame(rows,columns=ALL_COLS)
    if not df.empty:
        # Preserve known winner counts when importing an older number-only CSV.
        metadata=df.dropna(subset=WIN_COLS).drop_duplicates("회차",keep="last").set_index("회차")[WIN_COLS]
        df=df.drop_duplicates("회차",keep="last").set_index("회차")
        df[WIN_COLS]=df[WIN_COLS].combine_first(metadata).reindex(df.index)
        df=df.reset_index().sort_values("회차").reset_index(drop=True)
    if not df.empty:
        for n in sorted(set(range(int(df["회차"].min()),int(df["회차"].max())+1))-set(df["회차"])): issues.append({"행":"-","문제":f"{n}회 누락"})
    return df,pd.DataFrame(issues,columns=["행","문제"])

def log_event(status, count, msg):
    row = {"실행시각": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "상태": status, "수집회차": count, "메시지": msg}
    try:
        old = pd.read_csv(LOG) if LOG.exists() else pd.DataFrame(columns=row)
        pd.concat([old, pd.DataFrame([row])], ignore_index=True).to_csv(
            LOG, index=False, encoding="utf-8-sig")
    except (OSError, pd.errors.ParserError):
        pass  # 로그 기록 실패가 분석을 막지 않도록 처리

def update_history(years=5):
    old,_=validate(load_data()); newest=latest_round()
    if not newest: return old, f"조회 실패: {LAST_API_ERROR}", False
    first=max(1,newest-int(years*365.25/7))
    existing=set(old["회차"]); calls=0
    complete=set(old.dropna(subset=WIN_COLS)["회차"])

    for n in range(newest,first-1,-1):
        if n in complete or n in DRAW_CACHE: continue
        calls+=1
        if fetch_draw(n) is None: break
        time.sleep(.3)
    fresh=pd.DataFrame([r for n,r in DRAW_CACHE.items() if first<=n<=newest],columns=ALL_COLS)
    out,_=validate(pd.concat([old,fresh],ignore_index=True))
    missing=set(range(first,newest+1))-set(out["회차"])
    if not out.empty: save_data(out)
    msg=f"저장 {len(out)}회 / 신규 {len(set(out['회차'])-existing)}회 / 추가 API 호출 {calls}회 / 누락 {len(missing)}회"
    missing_stats = len(out.loc[out["회차"].between(first,newest) & out[WIN_COLS].isna().any(axis=1)])
    msg += f" / 당첨 유형 미확인 {missing_stats}회"
    log_event("부분 성공" if missing or missing_stats else "성공",len(out),msg)
    return out,msg,not missing
