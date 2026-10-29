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
    except Exception as exc:
        LAST_API_ERROR = f"{type(exc).__name__}: {exc}"
        return 0

def load_data():
    try: return pd.read_csv(CSV, dtype={"추첨일":str}) if CSV.exists() else pd.DataFrame(columns=COLS)
    except Exception: return pd.DataFrame(columns=COLS)

def validate(raw):
    issues=[]
    if raw is None or raw.empty: return pd.DataFrame(columns=COLS), pd.DataFrame(columns=["행","문제"])
    df=raw.copy()
    missing=[c for c in COLS if c not in df.columns]
    if missing: return pd.DataFrame(columns=COLS), pd.DataFrame([{"행":"-","문제":f"필수 열 누락: {missing}"}])
    for c in ["회차",*NC,"보너스"]: df[c]=pd.to_numeric(df[c],errors="coerce")
    for ix,r in df.iterrows():
        probs=[]
        if pd.isna(r["회차"]): probs.append("회차 오류")
        ns=[r[c] for c in NC]
        if any(pd.isna(x) for x in ns): probs.append("번호 누락")
        elif any(x<1 or x>45 for x in ns) or len(set(ns))!=6: probs.append("번호 범위/중복 오류")
        try: datetime.strptime(str(r["추첨일"]),"%Y-%m-%d")
        except Exception: probs.append("추첨일 형식 오류")
        if probs: issues.append({"행":int(ix)+2,"문제":", ".join(probs)})
    df=df.dropna(subset=["회차",*NC]).copy()
    df["회차"]=df["회차"].astype(int); df[NC]=df[NC].astype(int)
    df=df.drop_duplicates("회차",keep="last").sort_values("회차").reset_index(drop=True)
    if not df.empty:
        actual=set(df["회차"]); expected=set(range(int(df["회차"].min()),int(df["회차"].max())+1))
        for n in sorted(expected-actual): issues.append({"행":"-","문제":f"{n}회 누락"})
    return df,pd.DataFrame(issues,columns=["행","문제"])

def log_event(status,count,msg):
    row={"실행시각":datetime.now().strftime("%Y-%m-%d %H:%M:%S"),"상태":status,"수집회차":count,"메시지":msg}
    old=pd.read_csv(LOG) if LOG.exists() else pd.DataFrame(columns=row)
    pd.concat([old,pd.DataFrame([row])],ignore_index=True).to_csv(LOG,index=False,encoding="utf-8-sig")

def update_history(years=2, progress=None):
    old=load_data(); newest=latest_round()
    if newest<=0:
        detail = LAST_API_ERROR or "원인을 확인할 수 없는 오류"
        msg = f"동행복권 데이터 조회 실패: {detail}"
        log_event("실패",0,msg); return old,msg
    cutoff=date.today()-timedelta(days=int(years*365.25))
    first=max(1,newest-int(years*365.25/7)-4); rows=[]
    targets=list(range(first,newest+1))
    for i,n in enumerate(targets,1):
        row=fetch_draw(n)
        if row:
            try:
                if datetime.strptime(row["추첨일"],"%Y-%m-%d").date()>=cutoff: rows.append(row)
            except Exception: pass
        if progress and (i%4==0 or i==len(targets)):
            progress.progress(i/max(1,len(targets)),text=f"{n}회 확인 중")
        if i%8==0: time.sleep(.1)
    if not old.empty:
        for _,r in old.iterrows():
            try:
                if datetime.strptime(str(r["추첨일"]),"%Y-%m-%d").date()>=cutoff: rows.append(r.to_dict())
            except Exception: pass
    out,issues=validate(pd.DataFrame(rows))
    if not out.empty: out.to_csv(CSV,index=False,encoding="utf-8-sig")
    msg=f"업데이트 완료: {len(out)}개 회차"
    if not issues.empty: msg+=f" / 검증 경고 {len(issues)}건"
    log_event("성공",len(out),msg)
    return out,msg

def nums(row): return sorted(int(row[c]) for c in NC)
def runs(ns):
    result=[]; cur=[]
    for n in sorted(ns):
        if cur and n==cur[-1]+1: cur.append(n)
        else:
            if len(cur)>=2: result.append(cur)
            cur=[n]
    if len(cur)>=2: result.append(cur)
    return result
def zones(ns):
    return [sum(1<=n<=10 for n in ns),sum(11<=n<=20 for n in ns),sum(21<=n<=30 for n in ns),sum(31<=n<=40 for n in ns),sum(41<=n<=45 for n in ns)]

def get_stats(df):
    whole=Counter(n for _,r in df.iterrows() for n in nums(r))
    c4=Counter(n for _,r in df.tail(4).iterrows() for n in nums(r))
    c8=Counter(n for _,r in df.tail(8).iterrows() for n in nums(r))
    freq=pd.DataFrame({"번호":range(1,46),"전체":[whole[n] for n in range(1,46)],"최근4주":[c4[n] for n in range(1,46)],"최근8주":[c8[n] for n in range(1,46)]})
    par=[]; zon=[]; con=[]; adj=[]
    for i,(_,r) in enumerate(df.iterrows()):
        ns=nums(r); odd=sum(n%2 for n in ns); rr=runs(ns)
        par.append({"회차":int(r["회차"]),"홀수":odd,"짝수":6-odd})
        zon.append({"회차":int(r["회차"]),**dict(zip(["1-10","11-20","21-30","31-40","41-45"],zones(ns)))})
        con.append({"회차":int(r["회차"]),"연속번호 여부":"있음" if rr else "없음","연속번호":", ".join("-".join(map(str,x)) for x in rr) if rr else "-"})
        if i:
            prev=set(nums(df.iloc[i-1])); shared=sorted(prev&set(ns))
            adj.append({"이전 회차":int(df.iloc[i-1]["회차"]),"현재 회차":int(r["회차"]),"공통 번호":", ".join(map(str,shared)) if shared else "-","공통 개수":len(shared)})
    return freq,pd.DataFrame(par),pd.DataFrame(zon),pd.DataFrame(con),pd.DataFrame(adj)

def triples_table(df):
    seen={}
    for _,r in df.iterrows():
        for tri in itertools.combinations(nums(r),3): seen.setdefault(tri,[]).append(int(r["회차"]))
    rows=[{"번호 조합":", ".join(map(str,k)),"동시 출현 횟수":len(v),"회차 목록":", ".join(map(str,v))} for k,v in seen.items() if len(v)>=3]
    return pd.DataFrame(rows).sort_values(["동시 출현 횟수","번호 조합"],ascending=[False,True]) if rows else pd.DataFrame(columns=["번호 조합","동시 출현 횟수","회차 목록"])

def weight_scores(df,window,rstrength,lstrength,gstrength):
    recent=Counter(n for _,r in df.tail(window).iterrows() for n in nums(r))
    whole=Counter(n for _,r in df.iterrows() for n in nums(r))
    last={n:None for n in range(1,46)}
    for i,(_,r) in enumerate(df.iterrows()):
        for n in nums(r): last[n]=i
    mr=max(recent.values(),default=1); mw=max(whole.values(),default=1)
    mg=max([len(df)-1-v for v in last.values() if v is not None]+[1])
    scores={}
    for n in range(1,46):
        rec=recent[n]/mr; long=whole[n]/mw
        gap=(len(df)-1-last[n])/mg if last[n] is not None else 1
        scores[n]=max(.05,1+rstrength*rec+lstrength*long+gstrength*gap)
    return scores,recent,whole,last

def valid_combo(c, omin,omax,smin,smax,zmin,zmax,maxrun,include,exclude):
    odd=sum(n%2 for n in c); zz=zones(c)
    return omin<=odd<=omax and smin<=sum(c)<=smax and zmin<=sum(x>0 for x in zz)<=zmax and sum(len(x) for x in runs(c))<=maxrun and set(include).issubset(c) and not(set(exclude)&set(c))

def generate(df,explicit,count,window,rs,ls,gs,omin,omax,smin,smax,zmin,zmax,maxrun,include,exclude,seed=None):
    scores,*_=weight_scores(df,window,rs,ls,gs)
    for n,w in explicit.items(): scores[n]*=w
    rng=random.Random(seed); pool=list(range(1,46)); output=[]; seen=set(); attempts=0
    while len(output)<count and attempts<max(15000,count*6000):
        attempts+=1; rem=pool[:]; ws=[scores[n] for n in rem]; pick=[]
        for _ in range(6):
            ix=rng.choices(range(len(rem)),weights=ws,k=1)[0]
            pick.append(rem.pop(ix)); ws.pop(ix)
        combo=tuple(sorted(pick))
        if combo in seen or not valid_combo(combo,omin,omax,smin,smax,zmin,zmax,maxrun,include,exclude): continue
        seen.add(combo); output.append(combo)
    return output,attempts

def run_backtest(df,rounds,per_round,params):
    records=[]; start=max(8,len(df)-rounds)
    for pos in range(start,len(df)):
        train=df.iloc[:pos].reset_index(drop=True); target=set(nums(df.iloc[pos]))
        combos,_=generate(train,params["weights"],per_round,min(params["window"],len(train)),params["rs"],params["ls"],params["gs"],
                          params["omin"],params["omax"],params["smin"],params["smax"],params["zmin"],params["zmax"],params["maxrun"],[],[],
                          seed=12345+int(df.iloc[pos]["회차"]))
        for j,c in enumerate(combos,1):
            hit=sorted(set(c)&target)
            records.append({"회차":int(df.iloc[pos]["회차"]),"조합번호":j,"일치 개수":len(hit),
                            "일치 번호":", ".join(map(str,hit)) or "-","추천 조합":", ".join(map(str,c))})
    return pd.DataFrame(records)

# Sidebar controls
st.sidebar.header("데이터 관리")
years=st.sidebar.selectbox("수집/분석 기간",[1,2,3,5],index=1)
if st.sidebar.button("🌐 데이터 가져오기 / 업데이트",type="primary",use_container_width=True):
    bar=st.sidebar.progress(0)
    with st.spinner("공식 API에서 데이터를 확인 중입니다..."):
        d,msg=update_history(years,bar)
    st.session_state["history"]=d
    st.sidebar.success(msg) if "완료" in msg else st.sidebar.warning(msg)
if st.sidebar.button("📂 저장 데이터 다시 읽기",use_container_width=True):
    st.session_state["history"]=load_data()
if "history" not in st.session_state: st.session_state["history"]=load_data()
df,issues=validate(st.session_state["history"])
if df.empty:
    st.info("처음 실행 시 왼쪽의 **데이터 가져오기 / 업데이트** 버튼을 누르세요. 인터넷 연결이 필요합니다.")
    st.code("pip install -r requirements.txt\nstreamlit run app.py",language="bash")
    st.stop()
dates=pd.to_datetime(df["추첨일"],errors="coerce")
if dates.notna().any(): df=df[dates>=dates.max()-pd.Timedelta(days=365.25*years)].reset_index(drop=True)
freq,parity,zone,consec,adj=get_stats(df)
cr=(consec["연속번호 여부"]=="있음").mean() if len(consec) else 0
ar=(adj["공통 개수"]>0).mean() if len(adj) else 0
st.caption(f"분석 범위 {int(df.iloc[0]['회차'])}~{int(df.iloc[-1]['회차'])}회 · {len(df)}개 회차 · 최신 추첨일 {df.iloc[-1]['추첨일']}")
a,b,c,d=st.columns(4); a.metric("분석 회차",len(df)); b.metric("최신 회차",f"{int(df.iloc[-1]['회차'])}회"); c.metric("연속번호 포함",f"{cr:.1%}"); d.metric("직전 회차 중복",f"{ar:.1%}")

t1,t2,t_sum,t_gap,t3,t4,t5,t6,t7=st.tabs(["📊 빈도","⚖️ 홀짝·구간","➕ 합계 통계","🕒 장기 미출현 번호","🔗 연속·중복","🔎 반복 3개 조합","🎯 추천 생성","🧪 백테스트","🛠️ 데이터·자동화"])
with t1:
    period=st.radio("빈도 기준",["최근 4주","최근 8주","전체"],horizontal=True)
    col={"최근 4주":"최근4주","최근 8주":"최근8주","전체":"전체"}[period]
    st.plotly_chart(px.bar(freq,x="번호",y=col,title=f"1~45 번호별 {period} 출현 횟수").update_layout(xaxis=dict(dtick=1)),use_container_width=True)
    st.subheader("최근 4주 상위 10개 번호")
    st.dataframe(freq.sort_values(["최근4주","번호"],ascending=[False,True]).head(10),hide_index=True,use_container_width=True)
    st.dataframe(freq,hide_index=True,use_container_width=True)
with t2:
    pl=parity.melt(id_vars="회차",value_vars=["홀수","짝수"],var_name="종류",value_name="개수")
    st.plotly_chart(px.bar(pl,x="회차",y="개수",color="종류",barmode="stack",title="회차별 홀짝 구성"),use_container_width=True)
    pdist=parity.groupby(["홀수","짝수"]).size().reset_index(name="회차 수")
    pdist["비율"]=pdist["회차 수"]/len(df)
    st.dataframe(
        pdist.style.format({"비율": "{:.2%}"}),
        hide_index=True,
        use_container_width=True
    )
    zl=zone.melt(id_vars="회차",var_name="구간",value_name="개수")
    st.plotly_chart(px.bar(zl,x="회차",y="개수",color="구간",barmode="stack",title="회차별 번호 구간 분포"),use_container_width=True)
    st.dataframe(zone.sort_values("회차",ascending=False),hide_index=True,use_container_width=True)

with t_sum:
    st.subheader("회차별 당첨번호 6개 합계 분석")
    sum_df = df[["회차", "추첨일", *NC]].copy()
    sum_df["합계"] = sum_df[NC].sum(axis=1)

    latest_sum_row = sum_df.iloc[-1]
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("전체 평균", f"{sum_df['합계'].mean():.1f}")
    m2.metric("중앙값", f"{sum_df['합계'].median():.0f}")
    m3.metric("최저 합계", f"{int(sum_df['합계'].min())}")
    m4.metric("최고 합계", f"{int(sum_df['합계'].max())}")
    m5.metric("최신 회차 합계", f"{int(latest_sum_row['합계'])}", f"{int(latest_sum_row['회차'])}회")

    st.plotly_chart(
        px.line(sum_df, x="회차", y="합계", markers=True,
                title="회차별 당첨번호 합계 추이"),
        use_container_width=True
    )
    left_sum, right_sum = st.columns(2)
    with left_sum:
        st.plotly_chart(
            px.histogram(sum_df, x="합계", nbins=15,
                         title="당첨번호 합계 분포", labels={"합계": "6개 번호 합계", "count": "회차 수"}),
            use_container_width=True
        )
    with right_sum:
        sum_bins = [20, 80, 100, 120, 140, 160, 180, 200, 220, 240, 260]
        sum_labels = ["21–79", "80–99", "100–119", "120–139", "140–159",
                      "160–179", "180–199", "200–219", "220–239", "240–255"]
        sum_df["합계 구간"] = pd.cut(sum_df["합계"], bins=sum_bins, labels=sum_labels, right=False)
        sum_dist = (sum_df.groupby("합계 구간", observed=False).size()
                    .reindex(sum_labels, fill_value=0).rename("회차 수").reset_index())
        sum_dist.columns = ["합계 구간", "회차 수"]
        sum_dist["비율"] = sum_dist["회차 수"] / len(sum_df)
        st.dataframe(sum_dist.style.format({"비율": "{:.2%}"}), hide_index=True, use_container_width=True)

    st.subheader("회차별 합계 상세")
    st.dataframe(
        sum_df[["회차", "추첨일", *NC, "합계"]].sort_values("회차", ascending=False),
        hide_index=True, use_container_width=True
    )
    st.download_button(
        "합계 통계 CSV 다운로드",
        sum_df[["회차", "추첨일", *NC, "합계"]].to_csv(index=False).encode("utf-8-sig"),
        "lotto_sum_statistics.csv", "text/csv"
    )

with t_gap:
    st.subheader("이번 회차에 나온 장기 미출현 번호")
    st.caption("최근 회차 기준으로 직전 4회 이상 나오지 않았다가 이번 회차에 나온 본번호를 표시합니다. 경과 주수는 이번 회차와 직전 출현 회차의 회차 번호 차이로 계산합니다.")
    latest_row = df.iloc[-1]
    latest_numbers = nums(latest_row)
    current_round = int(latest_row["회차"])
    gap_rows = []

    for number in latest_numbers:
        previous = df.iloc[:-1]
        previous_hits = previous[previous[NC].eq(number).any(axis=1)]
        if previous_hits.empty:
            gap_rows.append({
                "번호": number,
                "직전 출현 회차": "분석 범위 내 이전 출현 없음",
                "이번 출현 회차": current_round,
                "경과 주수": "확인 불가"
            })
        else:
            previous_round = int(previous_hits.iloc[-1]["회차"])
            gap_weeks = current_round - previous_round
            if gap_weeks >= 5:
                gap_rows.append({
                    "번호": number,
                    "직전 출현 회차": previous_round,
                    "이번 출현 회차": current_round,
                    "경과 주수": gap_weeks
                })

    if gap_rows:
        gap_table = pd.DataFrame(gap_rows).sort_values("번호").reset_index(drop=True)
        st.success(f"{current_round}회 당첨번호 중 장기 미출현 번호: {len(gap_table)}개")
        st.dataframe(gap_table, hide_index=True, use_container_width=True)
    else:
        st.info(f"{current_round}회 당첨번호 중 직전 4회 이상 미출현 후 나온 번호가 없습니다.")

with t3:
    st.metric("연속번호 포함 회차 비율",f"{cr:.2%}")
    st.dataframe(consec.sort_values("회차",ascending=False),hide_index=True,use_container_width=True)
    st.write(f"직전 회차와 본번호가 하나 이상 겹치는 비율: **{ar:.2%}**")
    st.dataframe(adj.sort_values("현재 회차",ascending=False),hide_index=True,use_container_width=True)
with t4:
    tri=triples_table(df)
    if tri.empty: st.info("3회 이상 반복된 3개 번호 조합이 없습니다.")
    else:
        # 최소 동시 출현 횟수 슬라이더 오류 방지
        if tri.empty or "동시 출현 횟수" not in tri.columns:
            st.info("분석할 3개 번호 조합 데이터가 없습니다.")
            threshold = 3
        else:
            max_count = tri["동시 출현 횟수"].max()

            if tri["동시 출현 횟수"].notna().any():
                max_count = int(max_count)

                if max_count > 3:
                    threshold = st.slider(
                        "최소 동시 출현 횟수",
                        min_value=3,
                        max_value=max_count,
                        value=3
                    )
                else:
                    threshold = 3
                    st.info("동시 출현 횟수가 3회 이상인 조합만 표시합니다.")
            else:
                threshold = 3
                st.info("동시 출현 횟수 데이터가 없습니다.")



# Recommendation controls are also reused by backtest.
with t5:
    st.subheader("가중치 자동 계산 + 사용자 지정 가중치")
    selected=st.multiselect("가중치를 적용할 번호",list(range(1,46)),[2,16,21,41],key="sel")
    factor=st.slider("선택 번호 가중치 배수",1.0,5.0,2.0,.25,key="factor")
    window=st.slider("최근 빈도 분석 기간(회)",4,min(52,max(4,len(df))),min(8,max(4,len(df))),key="window")
    rs=st.slider("최근 빈도 반영 강도",0.,2.,.5,.1,key="rs")
    ls=st.slider("전체 빈도 반영 강도",0.,2.,.2,.1,key="ls")
    gs=st.slider("미출현 기간 반영 강도",0.,2.,0.,.1,key="gs")
    st.markdown("#### 조합 조건")
    q1,q2,q3,q4=st.columns(4)
    omin=q1.selectbox("홀수 최소",range(7),1,key="omin"); omax=q2.selectbox("홀수 최대",range(7),5,key="omax")
    smin=q3.number_input("합계 최소",21,270,100,key="smin"); smax=q4.number_input("합계 최대",21,270,200,key="smax")
    q5,q6,q7=st.columns(3)
    zmin = q5.selectbox(
        "포함 구간 최소",
        options=list(range(1, 6)),
        index=2,
        key="zmin"
    )

    zmax = q6.selectbox(
        "포함 구간 최대",
        options=list(range(1, 6)),
        index=4,
        key="zmax"
    )
    
    maxrun=q7.selectbox("연속번호에 포함되는 번호 최대 수",range(7),3,key="maxrun")
    include=st.multiselect("반드시 포함할 번호",list(range(1,46)),key="include")
    exclude=st.multiselect("제외할 번호",list(range(1,46)),key="exclude")
    count=st.slider("추천 조합 개수",1,50,15,key="count")
    seedtxt=st.text_input("랜덤 시드(선택)",key="seed")
    bad=omin>omax or smin>smax or zmin>zmax or len(include)>6 or bool(set(include)&set(exclude))
    if bad: st.error("조건을 확인하세요: 최소/최대 값, 필수 번호 6개 이하, 필수/제외 중복 여부.")
    elif st.button("🎲 추천 조합 생성",type="primary"):
        explicit={n:factor for n in selected}
        seed=int(seedtxt) if seedtxt.strip().lstrip("-").isdigit() else None
        out,attempts=generate(df,explicit,count,window,rs,ls,gs,omin,omax,smin,smax,zmin,zmax,maxrun,include,exclude,seed)
        st.session_state["recs"]=out; st.session_state["attempts"]=attempts
    recs=st.session_state.get("recs",[])
    if recs:
        result=pd.DataFrame([{"조합":i+1,**{f"번호{j+1}":n for j,n in enumerate(x)},"합계":sum(x),"홀수":sum(n%2 for n in x)} for i,x in enumerate(recs)])
        st.caption(f"{len(recs)}개 생성 · 시도 {st.session_state.get('attempts',0):,}회")
        if len(recs)<count: st.warning("조건이 까다로워 요청 수보다 적게 생성했습니다. 필터를 완화해 보세요.")
        st.dataframe(result,hide_index=True,use_container_width=True)
        st.download_button("추천 조합 CSV 다운로드",result.to_csv(index=False).encode("utf-8-sig"),"lotto_recommendations.csv","text/csv")

with t6:
    st.subheader("워크포워드 백테스트")
    st.caption("각 테스트 회차는 그 회차 이전의 데이터만 이용해 추천을 생성합니다. 보너스 번호는 일치 수에 포함하지 않습니다.")
    test_n=st.slider("검증할 최근 회차 수",5,min(40,max(5,len(df)-8)),min(10,max(5,len(df)-8)))
    per=st.slider("회차별 조합 수",1,30,15)
    bt_sel=st.multiselect("백테스트 사용자 가중치 번호",list(range(1,46)),[2,16,21,41],key="bt_sel")
    bt_factor=st.slider("백테스트 사용자 가중치 배수",1.,5.,2.,.25,key="bt_factor")
    if st.button("🧪 백테스트 실행",type="primary"):
        params={"weights":{n:bt_factor for n in bt_sel},"window":window,"rs":rs,"ls":ls,"gs":gs,"omin":omin,"omax":omax,"smin":smin,"smax":smax,"zmin":zmin,"zmax":zmax,"maxrun":maxrun}
        with st.spinner("과거 회차를 순차 검증 중입니다..."): st.session_state["bt"]=run_backtest(df,test_n,per,params)
    bt=st.session_state.get("bt",pd.DataFrame())
    if not bt.empty:
        dist=bt["일치 개수"].value_counts().reindex(range(7),fill_value=0).rename_axis("일치 개수").reset_index(name="조합 수")
        best=bt.groupby("회차")["일치 개수"].max().reset_index(name="회차별 최고 일치")
        x,y,z=st.columns(3); x.metric("검증 회차",bt["회차"].nunique()); y.metric("생성 조합",len(bt)); z.metric("조합당 평균 일치",f"{bt['일치 개수'].mean():.3f}")
        st.plotly_chart(px.bar(dist,x="일치 개수",y="조합 수",title="일치 개수 분포"),use_container_width=True)
        st.plotly_chart(px.line(best,x="회차",y="회차별 최고 일치",markers=True),use_container_width=True)
        st.dataframe(bt.sort_values(["회차","일치 개수"],ascending=[False,False]),hide_index=True,use_container_width=True)
        st.download_button("백테스트 CSV 다운로드",bt.to_csv(index=False).encode("utf-8-sig"),"lotto_backtest.csv","text/csv")
    else: st.info("백테스트 실행 버튼을 눌러 과거 결과를 확인하세요.")
with t7:
    st.subheader("API 연결 진단")
    if st.button("연결 테스트"):
        try:
            test_rows = _get_new_api_list(f"{BASE_API}/selectPstLt645Info.do")
            st.success(f"연결 성공: 최신 회차 응답 {len(test_rows)}건 수신")
            if test_rows:
                st.json(test_rows[0])
        except Exception as exc:
            st.error(f"{type(exc).__name__}: {exc}")
    st.caption("동행복권 홈페이지 개편 후 새 조회 경로를 사용합니다. 접속 제한/응답 형식 변경 시 이곳에서 오류를 확인하세요.")
    st.subheader("데이터 검증")
    if issues.empty: st.success("데이터 검증에서 회차 누락이나 형식 오류가 발견되지 않았습니다.")
    else:
        st.warning(f"검증 경고 {len(issues)}건")
        st.dataframe(issues,hide_index=True,use_container_width=True)
    st.download_button("현재 당첨번호 CSV 다운로드",df.to_csv(index=False).encode("utf-8-sig"),"lotto_history.csv","text/csv")
    st.subheader("업데이트 로그")
    if LOG.exists():
        logs=pd.read_csv(LOG); st.dataframe(logs.sort_values("실행시각",ascending=False),hide_index=True,use_container_width=True)
    else: st.info("업데이트 기록이 없습니다.")
    st.subheader("주간 자동 업데이트 설정")
    st.info("자동 업데이트 예약 안내는 README.md를 참고하세요.")
    st.markdown("자동 가중치는 최근 빈도·전체 빈도·미출현 기간을 별도 조절합니다. 필터는 홀짝·합계·구간 수·연속번호·필수/제외 번호를 지원합니다.")
    st.caption("공식 API의 접근 가능 여부와 응답 형식에 따라 업데이트가 실패할 수 있습니다. 업데이트 로그를 확인하세요.")
