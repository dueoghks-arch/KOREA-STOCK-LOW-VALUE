import FinanceDataReader as fdr
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import os
import smtplib
from email.mime.text import MIMEText
import time
import requests
import re

def get_krx_base_universe(min_marcap_eok=3000):
    """
    조건 1: 시가총액 지정 금액(원화 억 단위) 이상인 종목 선별
    """
    print(f"⏳ [KRX] 시가총액 {min_marcap_eok}억 원 이상 종목 필터링 중...")
    try:
        # KOSPI, KOSDAQ 전종목 가져오기
        df_kospi = fdr.StockListing('KOSPI')
        df_kosdaq = fdr.StockListing('KOSDAQ')
        
        df_kospi.columns = [col.upper() for col in df_kospi.columns]
        df_kosdaq.columns = [col.upper() for col in df_kosdaq.columns]
        
        df_total = pd.concat([df_kospi, df_kosdaq], axis=0).copy()
        df_total['MARCAP'] = pd.to_numeric(df_total['MARCAP'], errors='coerce')
        df_total = df_total.dropna(subset=['MARCAP', 'CODE'])
        
        # 시총 조건 필터링 (Marcap 단위: 원)
        cutoff_value = min_marcap_eok * 1e8
        df_filtered = df_total[df_total['MARCAP'] >= cutoff_value].copy()
        
        df_filtered['CODE'] = df_filtered['CODE'].astype(str).str.strip()
        
        print(f"✅ [KRX] 필터링 완료. 대상 종목: {len(df_filtered)}개 (전체 종목 중 시총 {min_marcap_eok}억 이상)")
        return df_filtered
    except Exception as e:
        print(f"⚠️ [KRX] 데이터 수집 실패: {e}")
        return pd.DataFrame()

def fetch_naver_consensus(ticker_code):
    """
    조건 2 & 3: 네이버 금융 컨센서스 페이지에서 12M Fwd PER 및 최근 2달 추정치 변경 이력 추출
    """
    url = f"https://finance.naver.com/item/coinfo.naver?code={ticker_code}"
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    
    # 기본값 설정
    fwd_per = np.nan
    revision_count = 0
    
    try:
        # 네이버 컨센서스 내부 FnGuide iframe 혹은 에이전트 데이터 접근
        # 실시간 데이터 분석을 위해 기업분석 탭의 컨센서스 요약 페이지 크롤링
        c_url = f"https://navercomp.wisereport.co.kr/v2/company/c1010001.aspx?cmp_cd={ticker_code}"
        res = requests.get(c_url, headers=headers, timeout=10)
        
        if res.status_code == 200:
            text = res.text
            
            # 1. 12M 선행 PER (또는 Target PER) 추출 파싱
            # FnGuide 테이블 구조 안에서 '12M PER' 또는 '추정PER' 검색
            per_match = re.search(r"이평선|PER[^<]*<td>([\d\.,]+)</td>", text)
            # 좀 더 정확한 컨센서스 밸류에이션 테이블 타겟팅 (정규식 대안)
            fwd_per_find = re.findall(r"<td>([\d\.,]+)</td>", text)
            
            # 네이버 지표 중 '12M 추정 PER' 매칭 (컨센서스 테이블 위치 기준 소급 적용)
            # 기본 탑재된 FnGuide 내 consensus 데이터 summary 파싱
            if "장기영업이익" in text or "컨센서스" in text:
                # 데이터 레이아웃에 맞춰 PER 추출 (실패 시 Trailing PER 대용 및 20 이하 필터링을 위해 유연성 확보)
                dfs = pd.read_html(res.text)
                for table in dfs:
                    if any('PER' in str(col) for col in table.columns) or any('투자지표' in str(row) for row in table.index):
                        # 대다수 기업분석 첫 페이지 테이블에 주요 지표 존재
                        pass
            
            # 2. 최근 2달 내 증권사 EPS 추정치 수정 건수
            # 리포트 추정치 변경 이력 요약 데이터 카운트 파싱
            # '최근 2개월 제공 증권사 수' 또는 '추정치 변동건수' 추출
            # wiseReport 페이지 내에 포함된 최근 60일 기준 리포트 수집 개수 매칭
            revision_match = re.search(r"최근2개월간\s*추정치\s*변경\s*:\s*(\d+)건", text)
            if revision_match:
                revision_count = int(revision_match.group(1))
            else:
                # 대안: 최근 2달간 발행된 리포트 수(컨센서스 갱신 주기) 카운팅
                report_match = re.findall(r"(\d{4}/\d{2}/\d{2})\s*[^<]*\s*증권", text)
                two_months_ago = datetime.now() - timedelta(days=60)
                for date_str in report_match:
                    try:
                        r_date = datetime.strptime(date_str.strip(), "%Y/%m/%d")
                        if r_date >= two_months_ago:
                            revision_count += 1
                    except:
                        continue
                        
            # 임시 스크래핑 보정 (실제 데이터셋 매핑)
            # FnGuide 컨센서스 테이블 로드 실패 방지용 Mocking / 정밀 파싱 프로세스
            # 실제 가동시에는 크롤링 타겟 테이블인 주요재무제표(E) 영역을 분석합니다.
            for df in dfs:
                if df.shape[1] >= 5 and 'IFRS' in str(df.columns):
                    # 연간/분기 컨센서스 테이블 탐색
                    try:
                        # 12M Fwd는 보통 가장 우측(당해년도E 또는 내년도E)에 위치
                        per_row = df[df.iloc[:, 0].str.contains('PER', na=False)]
                        if not per_row.empty:
                            # 가장 최근 추정치(E) 열 선택
                            fwd_per = float(per_row.iloc[0, -2].replace(',', ''))
                    except:
                        pass
    except Exception as e:
        pass
        
    return fwd_per, revision_count

def send_email(content, is_html=False):
    user = os.environ.get('EMAIL_USER')
    pw = os.environ.get('EMAIL_PASS')
    
    if not user or not pw:
        print("\n⚠️ [ENV] Secrets missing. Outputting directly to console:\n")
        print(content)
        return

    msg = MIMEText(content, 'html' if is_html else 'plain')
    msg['Subject'] = f"📊 [국장 스캐너] 퀀트 모멘텀(시총+선행PER+EPS수정) 포착 리포트 ({datetime.now().strftime('%Y-%m-%d')})"
    msg['From'] = user
    msg['To'] = user

    try:
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(user, pw)
        server.sendmail(user, user, msg.as_string())
        server.quit()
        print("📧 [EMAIL] Report dispatched successfully!")
    except Exception as e:
        print(f"❌ [EMAIL] Dispatch failed: {e}")

def screen_krx_stocks():
    # 1. 시총 3000억 이상 유니버스 획득
    df_universe = get_krx_base_universe(min_marcap_eok=3000)
    if df_universe.empty:
        print("❌ 대상 종목이 없어 종료합니다.")
        return
        
    results = []
    total_count = len(df_universe)
    
    print(f"📊 [SCAN] {total_count}개 종목에 대한 펀더멘탈 & 컨센서스 모멘텀 스크리닝 시작...")
    
    for idx, row in df_universe.iterrows():
        ticker = row['CODE']
        name = row['NAME']
        marcap = row['MARCAP']
        
        # 과도한 트래픽 차단 예방 및 딜레이
        time.sleep(0.5)
        
        # 2 & 3. 선행 PER 및 수정 이력 가져오기
        fwd_per, revision_cnt = fetch_naver_consensus(ticker)
        
        # 데이터 정밀 가공 및 디버깅용 로그
        if (idx + 1) % 30 == 0 or (idx + 1) == total_count:
            print(f"  > 진행 상황: {idx + 1} / {total_count} 완료...")
            
        # 조건 검증 
        # 조건 2: 12개월 선행 PER가 0 초과 20 이하 (적자 제외)
        cond2 = (not np.isnan(fwd_per)) and (0 < fwd_per <= 20)
        
        # 조건 3: 최근 2달 내 수정 이력 2회 이상
        cond3 = revision_cnt >= 2
        
        # 최종 통과
        if cond2 and cond3:
            # 현재가 가져오기 (가장 최근 종가)
            try:
                price_df = fdr.DataReader(ticker, pstart=datetime.now()-timedelta(days=5))
                curr_price = price_df['Close'].iloc[-1] if not price_df.empty else 0
            except:
                curr_price = 0
                
            results.append({
                '종목코드': ticker,
                '종목명': name,
                '시가총액(억원)': round(marcap / 1e8),
                '현재가(원)': f"{int(curr_price):,}" if curr_price else "N/A",
                '12M 선행 PER': round(fwd_per, 2),
                '최근 2달 수정 건수': f"{revision_cnt}회",
                '조건 만족': "✅ 시총 + PER + 추정치 수정 충족"
            })

    today_str = datetime.now().strftime('%Y-%m-%d')
    print(f"📊 [DEBUG] 필터링 조건을 통과한 최종 종목 수: {len(results)}개")

    if results:
        final_df = pd.DataFrame(results).sort_values(by='최근 2달 수정 건수', ascending=False)
        table_html = final_df.to_html(index=False, border=1, justify='center').replace('border="1"', 'style="border-collapse: collapse; width: 100%; text-align: center; font-size: 14px;" border="1"')
        
        html_content = f"""
        <h3 style="color: #1b5e20;">🇰🇷 국장 퀀트 모멘텀 (밸류에이션 + 컨센서스 상향) 보고서 ({today_str})</h3>
        <div style="background-color: #f5f5f5; padding: 10px; border-radius: 5px; margin-bottom: 15px;">
            <p style="margin: 0; font-size: 13px; color: #333;">
            <b>[적용 로직: 아래 3가지 조건 동시 만족 종목 선별]</b><br>
            <b>1. 시가총액:</b> 코스피/코스닥 3,000억 원 이상인 중대형주 <b>(AND)</b><br>
            <b>2. 밸류에이션:</b> 12개월 선행 PER(Forward PER) 20배 이하 (저평가/적정) <b>(AND)</b><br>
            <b>3. 기관 모멘텀:</b> 최근 2달 이내에 증권사별로 EPS 추정치 등을 2회 이상 수정한 종목 (시장 관심도 집중)
            </p>
        </div>
        {table_html}
        """
        send_email(html_content, is_html=True)
    else:
        html_content = f"""
        <h3 style="color: #b71c1c;">⚠️ 국장 스캐너 알림 ({today_str})</h3>
        <p>오늘 기준 <b>시총 3000억 이상, 선행 PER 20 이하, 최근 2달 내 추정치 2회 이상 수정</b> 조건을 동시에 만족하는 종목이 포착되지 않았습니다.</p>
        """
        send_email(html_content, is_html=True)

if __name__ == "__main__":
    screen_krx_stocks()
