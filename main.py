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
from concurrent.futures import ThreadPoolExecutor, as_completed

def get_krx_base_universe(min_marcap_eok=3000):
    print(f"⏳ [KRX] 시가총액 {min_marcap_eok}억 원 이상 종목 필터링 중...")
    try:
        df_kospi = fdr.StockListing('KOSPI')
        df_kosdaq = fdr.StockListing('KOSDAQ')
        
        df_kospi.columns = [col.upper() for col in df_kospi.columns]
        df_kosdaq.columns = [col.upper() for col in df_kosdaq.columns]
        
        df_total = pd.concat([df_kospi, df_kosdaq], axis=0).copy()
        df_total['MARCAP'] = pd.to_numeric(df_total['MARCAP'], errors='coerce')
        df_total = df_total.dropna(subset=['MARCAP', 'CODE'])
        
        cutoff_value = min_marcap_eok * 1e8
        df_filtered = df_total[df_total['MARCAP'] >= cutoff_value].copy()
        df_filtered['CODE'] = df_filtered['CODE'].astype(str).str.strip()
        
        print(f"✅ [KRX] 대상 종목: {len(df_filtered)}개")
        return df_filtered
    except Exception as e:
        print(f"⚠️ [KRX] 실패: {e}")
        return pd.DataFrame()

def fetch_naver_consensus(ticker_code):
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    fwd_per = np.nan
    revision_count = 0
    
    try:
        c_url = f"https://navercomp.wisereport.co.kr/v2/company/c1010001.aspx?cmp_cd={ticker_code}"
        # 타임아웃을 5초로 줄여 응답 없는 사이트에 매달리는 시간 최소화
        res = requests.get(c_url, headers=headers, timeout=5)
        
        if res.status_code == 200:
            text = res.text
            
            revision_match = re.search(r"최근2개월간\s*추정치\s*변경\s*:\s*(\d+)건", text)
            if revision_match:
                revision_count = int(revision_match.group(1))
            else:
                report_match = re.findall(r"(\d{4}/\d{2}/\d{2})\s*[^<]*\s*증권", text)
                two_months_ago = datetime.now() - timedelta(days=60)
                for date_str in report_match:
                    try:
                        r_date = datetime.strptime(date_str.strip(), "%Y/%m/%d")
                        if r_date >= two_months_ago:
                            revision_count += 1
                    except:
                        continue
                        
            if "장기영업이익" in text or "컨센서스" in text:
                dfs = pd.read_html(res.text, flavor='lxml')
                for df in dfs:
                    if df.shape[1] >= 5 and 'IFRS' in str(df.columns):
                        try:
                            per_row = df[df.iloc[:, 0].str.contains('PER', na=False)]
                            if not per_row.empty:
                                fwd_per = float(per_row.iloc[0, -2].replace(',', ''))
                        except:
                            pass
    except Exception:
        pass
        
    return fwd_per, revision_count

def process_single_stock(row):
    """
    개별 종목을 검사하는 함수 (멀티스레딩에서 독립적으로 실행됨)
    """
    ticker = row['CODE']
    name = row['NAME']
    marcap = row['MARCAP']
    
    fwd_per, revision_cnt = fetch_naver_consensus(ticker)
    
    cond2 = (not np.isnan(fwd_per)) and (0 < fwd_per <= 20)
    cond3 = revision_cnt >= 2
    
    if cond2 and cond3:
        try:
            # 타임아웃 방지를 위해 최근 5일치만 빠르게 로드
            price_df = fdr.DataReader(ticker, pstart=datetime.now()-timedelta(days=5))
            curr_price = price_df['Close'].iloc[-1] if not price_df.empty else 0
        except:
            curr_price = 0
            
        return {
            '종목코드': ticker,
            '종목명': name,
            '시가총액(억원)': round(marcap / 1e8),
            '현재가(원)': f"{int(curr_price):,}" if curr_price else "N/A",
            '12M 선행 PER': round(fwd_per, 2),
            '최근 2달 수정 건수': f"{revision_cnt}회",
            '조건 만족': "✅ 시총 + PER + 추정치 수정 충족"
        }
    return None

def send_email(content, is_html=False):
    user = os.environ.get('EMAIL_USER')
    pw = os.environ.get('EMAIL_PASS')
    
    if not user or not pw:
        print("\n⚠️ [ENV] Secrets missing. Outputting directly to console:\n")
        print(content)
        return

    msg = MIMEText(content, 'html' if is_html else 'plain')
    msg['Subject'] = f"📊 [국장 스캐너] 퀀트 모멘텀 포착 리포트 ({datetime.now().strftime('%Y-%m-%d')})"
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
    df_universe = get_krx_base_universe(min_marcap_eok=3000)
    if df_universe.empty:
        print("❌ 대상 종목이 없어 종료합니다.")
        return
        
    results = []
    total_count = len(df_universe)
    completed_count = 0
    
    print(f"📊 [SCAN] {total_count}개 종목 스크리닝 시작 (멀티스레딩 가동)...")
    
    # max_workers=10 으로 설정하여 10개씩 동시에 처리 (네이버 서버 차단 방지를 위해 적정선 유지)
    with ThreadPoolExecutor(max_workers=10) as executor:
        # 딕셔너리 컴프리헨션으로 각 종목의 작업을 스레드풀에 예약
        futures = {executor.submit(process_single_stock, row): row for idx, row in df_universe.iterrows()}
        
        for future in as_completed(futures):
            completed_count += 1
            if completed_count % 50 == 0 or completed_count == total_count:
                print(f"  > 진행 상황: {completed_count} / {total_count} 완료...")
                
            res = future.result()
            if res:
                results.append(res)

    today_str = datetime.now().strftime('%Y-%m-%d')
    print(f"📊 [DEBUG] 필터링 통과 최종 종목 수: {len(results)}개")

    if results:
        final_df = pd.DataFrame(results).sort_values(by='최근 2달 수정 건수', ascending=False)
        table_html = final_df.to_html(index=False, border=1, justify='center').replace('border="1"', 'style="border-collapse: collapse; width: 100%; text-align: center; font-size: 14px;" border="1"')
        
        html_content = f"""
        <h3 style="color: #1b5e20;">🇰🇷 국장 퀀트 모멘텀 (밸류에이션 + 컨센서스 상향) 보고서 ({today_str})</h3>
        {table_html}
        """
        send_email(html_content, is_html=True)
    else:
        html_content = f"<h3>⚠️ 국장 스캐너 알림 ({today_str})</h3><p>조건을 만족하는 종목이 포착되지 않았습니다.</p>"
        send_email(html_content, is_html=True)

if __name__ == "__main__":
    screen_krx_stocks()
