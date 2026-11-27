import itertools, random, time, os
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import plotly.express as px
import requests
import streamlit as st

st.set_page_config(page_title="로또 통계 분석 Pro", page_icon="🎱", layout="wide")
from lotto_data import *

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
    return freq,pd.DataFrame(par),pd.DataFrame(zon),pd.DataFrame(con),pd.DataFrame(adj,columns=["이전 회차","현재 회차","공통 번호","공통 개수"])

def triples_table(df):
    seen={}
    for _,r in df.iterrows():
        for tri in itertools.combinations(nums(r),3): seen.setdefault(tri,[]).append(int(r["회차"]))
    rows=[{"번호 조합":", ".join(map(str,k)),"동시 출현 횟수":len(v),"회차 목록":", ".join(map(str,v))} for k,v in seen.items() if len(v)>=2]
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
    """각 테스트 회차 직전 N회의 당첨번호에 추가 가중치를 적용한다."""
    records=[]; start=max(8,len(df)-rounds)
    recent_rounds = int(params.get("recent_rounds", 4))
    recent_factor = float(params.get("recent_factor", 2.0))

    for pos in range(start,len(df)):
        # 테스트 대상 회차 이전 데이터만 사용해 미래 정보 누출을 방지
        train=df.iloc[:pos].reset_index(drop=True)
        target=set(nums(df.iloc[pos]))

        recent_numbers = {
            n
            for _, previous_row in train.tail(min(recent_rounds, len(train))).iterrows()
            for n in nums(previous_row)
        }
        combined_weights = dict(params["weights"])
        for n in recent_numbers:
            combined_weights[n] = combined_weights.get(n, 1.0) * recent_factor

        combos,_=generate(train,combined_weights,per_round,min(params["window"],len(train)),params["rs"],params["ls"],params["gs"],
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
if "history" not in st.session_state or st.sidebar.button("저장 데이터 다시 읽기"):
    st.session_state["history"]=load_data()
with st.sidebar.expander("CSV 복원 / 수동 입력"):
    uploaded=st.file_uploader("당첨번호 CSV",type="csv")
    if uploaded is not None and st.button("CSV 병합"):
        try:
            incoming,err=validate(pd.read_csv(uploaded))
            if incoming.empty: st.error("유효한 데이터 없음")
            else:
                st.session_state["history"],_=validate(pd.concat([st.session_state["history"],incoming],ignore_index=True)); st.success(f"병합 완료: 검증 경고 {len(err)}건")
        except Exception as exc: st.error(str(exc))
    with st.form("manual_draw"):
        rn=st.number_input("회차",min_value=1,step=1,value=1252)
        dt=st.date_input("추첨일")
        balls=st.text_input("본번호 6개와 보너스 (공백 구분)")
        if st.form_submit_button("회차 추가"):
            try:
                values=list(map(int,balls.split()))
                incoming,_=validate(pd.DataFrame([dict(zip(COLS,[rn,str(dt),*values]))])) if len(values)==7 else (pd.DataFrame(),None)
                if incoming.empty: raise ValueError("번호 7개를 중복 없이 입력하세요.")
                st.session_state["history"],_=validate(pd.concat([st.session_state["history"],incoming],ignore_index=True)); st.success("추가 완료")
            except Exception as exc: st.error(str(exc))
    st.caption("수동 변경은 현재 세션에만 유지됩니다. CSV를 내려받아 GitHub에 반영하세요.")
df,issues=validate(st.session_state["history"])
if df.empty:
    st.info("국내 PC에서 python update_lotto.py 실행 후 data/lotto_history.csv를 GitHub에 올리거나 CSV를 복원하세요.")
    st.stop()
st.sidebar.download_button("전체 CSV 다운로드",df.to_csv(index=False).encode("utf-8-sig"),"lotto_history.csv")
st.sidebar.caption(f"저장 최신 회차: {df.iloc[-1]['회차']}회 / {df.iloc[-1]['추첨일']}")
if CSV.exists(): st.sidebar.caption("CSV 파일 수정 시각 (서버 기준): "+datetime.fromtimestamp(CSV.stat().st_mtime).strftime("%Y-%m-%d %H:%M"))
def show_draw(r):
    colors=["#c89b16","#2783c5","#d34e4e","#777777","#519742"]
    balls=" ".join(f'<span style="display:inline-block;background:{colors[(int(n)-1)//10]};color:white;border-radius:50%;width:25px;height:25px;text-align:center;line-height:25px">{int(n)}</span>' for n in [*nums(r),r["보너스"]])
    st.markdown(f"**{int(r['회차'])}회** · {r['추첨일']}<br>{balls}",unsafe_allow_html=True)
with st.sidebar:
    st.subheader("최근 5회 당첨번호")
    for _,r in df.tail(5).iloc[::-1].iterrows(): show_draw(r)
    query=st.number_input("회차 검색",min_value=1,step=1,value=int(df.iloc[-1]["회차"]))
    found=df[df["회차"]==query]
    if not found.empty: show_draw(found.iloc[0])
    else: st.caption("저장된 회차 없음")
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
    st.subheader("반복 3개 번호 조합")
    st.caption("선택한 분석 기간 내 본번호 6개에서 동일한 3개 번호가 2회 이상 함께 출현한 조합입니다. 보너스 번호는 제외합니다.")
    tri = triples_table(df)
    st.markdown("#### 번호 조합 검색")
    search_nums = st.multiselect("검색할 번호 3개를 선택하세요", list(range(1, 46)), max_selections=3, key="triple_search_nums")
    hits = []
    if len(search_nums) == 3:
        wanted = set(search_nums)
        hits = sorted({int(row["회차"]) for _, row in df.iterrows() if wanted.issubset({int(row[f"번호{i}"]) for i in range(1, 7)})}, reverse=True)
        if len(hits) >= 2:
            st.success(f"반복 조합에 있습니다: {len(hits)}회 출현")
        elif hits:
            st.info("1회 출현: 반복 조합 목록에는 없습니다.")
        else:
            st.info("선택한 분석 기간에 출현 기록이 없습니다.")
        if hits:
            st.write("출현 회차: " + ", ".join(map(str, hits)))
    else:
        st.caption("서로 다른 번호 3개를 선택하면 자동 검색합니다.")
    st.markdown("#### 전체 반복 조합 목록")
    if tri.empty:
        st.info("2회 이상 반복된 3개 번호 조합이 없습니다.")
    else:
        max_count = int(tri["동시 출현 횟수"].max())
        threshold = st.selectbox("최소 동시 출현 횟수", list(range(2, max_count + 1)), key="triple_threshold")
        filtered = tri.loc[tri["동시 출현 횟수"] >= threshold].reset_index(drop=True)
        if len(search_nums) == 3 and 2 <= len(hits) < threshold:
            st.caption(f"검색 조합은 현재 최소 {threshold}회 필터에 해당하지 않습니다.")
        st.metric("반복 조합 수", len(filtered))
        st.dataframe(filtered, hide_index=True, use_container_width=True)
        st.download_button("반복 3개 조합 CSV 다운로드", filtered.to_csv(index=False).encode("utf-8-sig"), "lotto_repeated_triples.csv", "text/csv")

# Recommendation controls are also reused by backtest.
with t5:
    st.subheader("가중치 자동 계산 + 사용자 지정 가중치")
    selected=st.multiselect("가중치를 적용할 번호",list(range(1,46)),[2,16,21,41],key="sel")
    factor=st.slider("선택 번호 가중치 배수",1.0,5.0,2.0,.25,key="factor")
    window=st.slider("최근 빈도 분석 기간(회)",1,max(2,min(52,len(df))),min(8,len(df)),key="window")
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
    st.caption("A·B·C·D 네 가지 가중치 조건을 동일한 검증 회차와 조합 수로 비교합니다. 각 테스트 회차는 해당 회차 이전 데이터만 사용하며, 보너스 번호는 일치 수에 포함하지 않습니다.")
    test_n=st.slider("검증할 최근 회차 수",2,max(3,min(40,len(df)-8)),min(10,max(2,len(df)-8)))
    per=st.slider("조건별 회차당 조합 수",1,30,15)
    bt_sel=st.multiselect("공통 적용할 사용자 가중치 번호",list(range(1,46)),[2,16,21,41],key="bt_sel")
    bt_factor=st.slider("사용자 가중치 배수",1.,5.,2.,.25,key="bt_factor")

    scenario_defs = [
        ("A", "가중치 미적용", 0, 1.0),
        ("B", "직전 2회 × 2배", 2, 2.0),
        ("C", "직전 4회 × 2배", 4, 2.0),
        ("D", "직전 5회 × 3배", 5, 3.0),
    ]
    st.markdown("#### 비교 조건")
    scenario_table = pd.DataFrame([
        {"구분": code, "조건": label, "직전 회차": "없음" if n == 0 else f"{n}회", "추가 배수": f"{factor:g}배"}
        for code, label, n, factor in scenario_defs
    ])
    st.dataframe(scenario_table, hide_index=True, use_container_width=True)
    st.caption("사용자 지정 번호 가중치는 A·B·C·D 모두에 동일하게 적용됩니다. 직전 회차 가중치와 사용자 지정 번호가 겹치면 배수가 곱해집니다.")

    if st.button("🧪 A·B·C·D 비교 백테스트 실행",type="primary"):
        all_results=[]
        with st.spinner("A·B·C·D 네 조건을 순차 검증 중입니다..."):
            for code, label, recent_n, recent_mult in scenario_defs:
                params={"weights":{n:bt_factor for n in bt_sel},
                        "recent_rounds":recent_n,"recent_factor":recent_mult,
                        "window":window,"rs":rs,"ls":ls,"gs":gs,
                        "omin":omin,"omax":omax,"smin":smin,"smax":smax,
                        "zmin":zmin,"zmax":zmax,"maxrun":maxrun}
                result=run_backtest(df,test_n,per,params)
                if not result.empty:
                    result.insert(0,"조건",code)
                    result.insert(1,"조건 설명",label)
                    all_results.append(result)
        st.session_state["bt_compare"]=pd.concat(all_results,ignore_index=True) if all_results else pd.DataFrame()
        st.session_state["bt_compare_settings"]={"test_n":test_n,"per":per,"user_numbers":bt_sel,"user_factor":bt_factor}

    bt=st.session_state.get("bt_compare",pd.DataFrame())
    if not bt.empty:
        # 조건별 비교 지표: 평균 적중 수, 회차별 최고 적중 수 평균, 3개 이상 적중 조합 수
        summary_rows=[]
        for (code, label), group in bt.groupby(["조건","조건 설명"],sort=False):
            summary_rows.append({"조건":code,"조건 설명":label,
                "생성조합수":len(group),"평균일치개수":group["일치 개수"].mean(),
                "3개이상적중":int((group["일치 개수"]>=3).sum()),
                "4개이상적중":int((group["일치 개수"]>=4).sum())})
        summary=pd.DataFrame(summary_rows)
        best=(bt.groupby(["조건","회차"],sort=False)["일치 개수"].max()
              .groupby("조건").mean().rename("회차별최고일치 평균").reset_index())
        summary=summary.merge(best,on="조건",how="left")
        summary=summary[["조건","조건 설명","생성조합수","평균일치개수","회차별최고일치 평균","3개이상적중","4개이상적중"]]
        st.markdown("#### A·B·C·D 성능 비교")
        st.dataframe(summary.round({"평균일치개수":3,"회차별최고일치 평균":3}),hide_index=True,use_container_width=True)
        chart=summary.melt(id_vars=["조건"],value_vars=["평균일치개수","회차별최고일치 평균"],var_name="지표",value_name="평균 적중")
        st.plotly_chart(px.bar(chart,x="조건",y="평균 적중",color="지표",barmode="group",title="조건별 평균 적중 수 비교"),use_container_width=True)
        dist=(bt.groupby(["조건","일치 개수"]).size().reset_index(name="조합 수"))
        st.plotly_chart(px.bar(dist,x="일치 개수",y="조합 수",color="조건",barmode="group",title="조건별 일치 개수 분포"),use_container_width=True)
        st.markdown("#### 회차별 최고 적중 수")
        best_each=bt.groupby(["조건","회차"])["일치 개수"].max().reset_index(name="최고 일치")
        st.plotly_chart(px.line(best_each,x="회차",y="최고 일치",color="조건",markers=True,title="A·B·C·D 회차별 최고 적중 비교"),use_container_width=True)
        st.markdown("#### 전체 추천 조합 결과")
        st.dataframe(bt.sort_values(["조건","회차","일치 개수"],ascending=[True,False,False]),hide_index=True,use_container_width=True)
        st.download_button("A·B·C·D 전체 결과 CSV 다운로드",bt.to_csv(index=False).encode("utf-8-sig"),"lotto_backtest_ABCD.csv","text/csv")
        st.download_button("A·B·C·D 요약 CSV 다운로드",summary.to_csv(index=False).encode("utf-8-sig"),"lotto_backtest_ABCD_summary.csv","text/csv")
    else:
        st.info("A·B·C·D 비교 백테스트 실행 버튼을 눌러 네 조건을 한 번에 비교하세요.")

with t7:
    st.subheader("데이터 검증 및 자동화")
    if issues.empty: st.success("검증 경고 없음")
    else: st.dataframe(issues,hide_index=True)
    st.info("공식 API 수집은 국내 PC의 update_lotto.py에서 수행합니다. Cloud는 GitHub CSV만 읽습니다.")
    if LOG.exists(): st.dataframe(pd.read_csv(LOG),hide_index=True)
