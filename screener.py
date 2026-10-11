import os
import requests
import pandas as pd
from datetime import datetime, timedelta

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

def send_telegram_message(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(message)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try: requests.post(url, json=payload)
    except: pass

def get_screened_stocks(csv_path="market_data_archive.csv"):
    try:
        # 종목코드를 강제로 6자리 문자열로 읽기
        df = pd.read_csv(csv_path, dtype={'종목코드': str})
        df['종목코드'] = df['종목코드'].astype(str).str.zfill(6)
        df['날짜'] = pd.to_datetime(df['날짜'])
        df = df[df['종목명'] != '오이솔루션']
    except FileNotFoundError:
        return None, None

    latest_date = df['날짜'].max()
    latest_df = df[df['날짜'] == latest_date].copy()
    results = {}

    # 💡 프로 필터 1: 시가총액 500억 이상 (잡주/유동성 부족 제외)
    # 💡 프로 필터 2: NaN(비어있는 재무 데이터) 제외 로직 추가 (.notna())
    
    # 1. 방어형 가치주 (+ 시총 500억 이상)
    cond_graham = (latest_df['PER'].notna()) & (latest_df['PER'] > 0) & (latest_df['PER'] <= 10) & \
                  (latest_df['PBR'].notna()) & (latest_df['PBR'] > 0) & (latest_df['PBR'] <= 1) & \
                  (latest_df['부채비율'].notna()) & (latest_df['부채비율'] > 0) & (latest_df['부채비율'] <= 100) & \
                  (latest_df['시가총액_억원'].notna()) & (latest_df['시가총액_억원'] >= 500)
    results['graham'] = latest_df[cond_graham].sort_values('PBR').head(3)

    # 안전 필터 (마법공식/피터린치 공통)
    safe_cond = (latest_df['PER'].notna()) & (latest_df['PER'] >= 2.0) & (latest_df['PER'] <= 50.0) & \
                (latest_df['ROE'].notna()) & (latest_df['ROE'] >= 5.0) & (latest_df['ROE'] <= 40.0) & \
                (latest_df['시가총액_억원'].notna()) & (latest_df['시가총액_억원'] >= 500)

    # 2. 마법공식
    mf_df = latest_df[safe_cond].copy()
    mf_df['PER_Rank'] = mf_df['PER'].rank(ascending=True)
    mf_df['ROE_Rank'] = mf_df['ROE'].rank(ascending=False)
    mf_df['Magic_Score'] = mf_df['PER_Rank'] + mf_df['ROE_Rank']
    results['magic'] = mf_df.sort_values('Magic_Score').head(3)

    # 3. 피터 린치 성장주
    lynch_df = latest_df[safe_cond].copy()
    lynch_df['PEG'] = lynch_df['PER'] / lynch_df['ROE']
    cond_lynch = (lynch_df['PEG'] > 0) & (lynch_df['PEG'] <= 0.5)
    results['lynch'] = lynch_df[cond_lynch].sort_values('PEG').head(3)

    # 4 & 5. 수급 매집 (주식 & ETF)
    fourteen_days_ago = latest_date - timedelta(days=14)
    period_df = df[df['날짜'] >= fourteen_days_ago].copy()
    
    div_data_stock = []
    div_data_etf = []
    
    for code, group in period_df.groupby('종목코드'):
        if len(group) < 2: continue
        group = group.sort_values('날짜')
        start_price = group.iloc[0]['종가']
        end_price = group.iloc[-1]['종가']
        if start_price == 0: continue
        
        price_return = ((end_price - start_price) / start_price) * 100
        
        total_foreign_amt = group['외인매수_억원'].sum()
        total_org_amt = group['기관매수_억원'].sum()
        total_smart_money_amt = total_foreign_amt + total_org_amt
        
        is_etf = group.iloc[0]['시장구분'] == 'ETF'
        
        data_dict = {
            '종목코드': code,
            '종목명': group.iloc[0]['종목명'],
            '주가수익률(%)': round(price_return, 2),
            '외인누적_억원': total_foreign_amt,
            '쌍끌이누적_억원': total_smart_money_amt
        }
        
        if is_etf: div_data_etf.append(data_dict)
        else: div_data_stock.append(data_dict)
            
    df_stock = pd.DataFrame(div_data_stock)
    if not df_stock.empty:
        cond_stock = (df_stock['주가수익률(%)'] <= 2.0) & (df_stock['외인누적_억원'] > 0)
        results['stock_div'] = df_stock[cond_stock].sort_values('외인누적_억원', ascending=False).head(3)
    else:
        results['stock_div'] = pd.DataFrame()

    df_etf = pd.DataFrame(div_data_etf)
    if not df_etf.empty:
        cond_etf = (df_etf['주가수익률(%)'] <= 2.0) & (df_etf['쌍끌이누적_억원'] > 0)
        results['etf_div'] = df_etf[cond_etf].sort_values('쌍끌이누적_억원', ascending=False).head(3)
    else:
        results['etf_div'] = pd.DataFrame()

    return results, latest_date

def send_daily_briefing():
    screened_data, base_date = get_screened_stocks()
    if not screened_data: return

    date_str = base_date.strftime('%Y-%m-%d')
    msg = f"📊 <b>오늘의 퀀트 브리핑(PRO)</b> ({date_str})\n\n"

    msg += "<b>🏆 1. 방어형 가치주 (PER 10↓, PBR 1↓)</b>\n"
    if not screened_data['graham'].empty:
        for _, row in screened_data['graham'].iterrows():
            msg += f"• {row['종목명']} (PER:{row['PER']:.1f}, PBR:{row['PBR']:.1f})\n"
    else: msg += "• 조건 만족 종목 없음\n"
    msg += "\n"

    msg += "<b>🧙‍♂️ 2. 마법공식 (저PER+고ROE)</b>\n"
    if not screened_data['magic'].empty:
        for _, row in screened_data['magic'].iterrows():
            msg += f"• {row['종목명']} (PER:{row['PER']:.1f}, ROE:{row['ROE']:.1f})\n"
    else: msg += "• 조건 만족 종목 없음\n"
    msg += "\n"

    msg += "<b>🚀 3. 피터린치 성장주 (PEG 0.5↓)</b>\n"
    if not screened_data['lynch'].empty:
        for _, row in screened_data['lynch'].iterrows():
            msg += f"• {row['종목명']} (PEG:{row['PEG']:.2f}, ROE:{row['ROE']:.1f})\n"
    else: msg += "• 조건 만족 종목 없음\n"
    msg += "\n"

    msg += "<b>🕵️‍♂️ 4. 주식: 외인 매집 횡보주</b>\n"
    if not screened_data['stock_div'].empty:
        for _, row in screened_data['stock_div'].iterrows():
            msg += f"• {row['종목명']} (수익:{row['주가수익률(%)']}%, 외인:{row['외인누적_억원']:.1f}억)\n"
    else: msg += "• 조건 만족 종목 없음\n"
    msg += "\n"

    msg += "<b>🏦 5. ETF: 바닥권 쌍끌이 매집</b>\n"
    if not screened_data['etf_div'].empty:
        for _, row in screened_data['etf_div'].iterrows():
            msg += f"• {row['종목명']}\n  (수익:{row['주가수익률(%)']}%, 쌍끌이:{row['쌍끌이누적_억원']:.1f}억)\n"
    else: msg += "• 조건 만족 종목 없음\n"

    send_telegram_message(msg)

if __name__ == "__main__":
    send_daily_briefing()