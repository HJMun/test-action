import os
import requests
import pandas as pd
from datetime import datetime, timedelta

# GitHub Secrets에서 텔레그램 정보 불러오기
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

def send_telegram_message(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("텔레그램 토큰 미설정 (터미널 출력용):")
        print(message)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"텔레그램 알림 실패: {e}")

def get_screened_stocks(csv_path="market_data_archive.csv"):
    try:
        df = pd.read_csv(csv_path)
        df['날짜'] = pd.to_datetime(df['날짜'])
    except FileNotFoundError:
        return None, None

    latest_date = df['날짜'].max()
    latest_df = df[df['날짜'] == latest_date].copy()
    results = {}

    # ==========================================
    # 1~3. 기존 가치투자 주식 전략 (PER, PBR, ROE 기반)
    # ==========================================
    cond1 = (latest_df['PER'] > 0) & (latest_df['PER'] <= 10)
    cond2 = (latest_df['PBR'] > 0) & (latest_df['PBR'] <= 1)
    cond3 = (latest_df['부채비율'] > 0) & (latest_df['부채비율'] <= 100)
    results['graham'] = latest_df[cond1 & cond2 & cond3].sort_values('PBR').head(3)

    mf_df = latest_df[(latest_df['PER'] > 0) & (latest_df['ROE'] > 0)].copy()
    mf_df['PER_Rank'] = mf_df['PER'].rank(ascending=True)
    mf_df['ROE_Rank'] = mf_df['ROE'].rank(ascending=False)
    mf_df['Magic_Score'] = mf_df['PER_Rank'] + mf_df['ROE_Rank']
    results['magic'] = mf_df.sort_values('Magic_Score').head(3)

    lynch_df = latest_df[(latest_df['PER'] > 0) & (latest_df['ROE'] > 0)].copy()
    lynch_df['PEG'] = lynch_df['PER'] / lynch_df['ROE']
    cond_lynch = (lynch_df['PEG'] > 0) & (lynch_df['PEG'] <= 0.5)
    results['lynch'] = lynch_df[cond_lynch].sort_values('PEG').head(3)

    # ==========================================
    # 4. 주식 & ETF 수급 다이버전스 (14일 매집 로직)
    # ==========================================
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
        
        # 14일 주가 수익률
        price_return = ((end_price - start_price) / start_price) * 100
        
        # 누적 순매수 계산
        total_foreign = group['외국인순매수'].sum()
        total_org = group['기관순매수'].sum()
        total_smart_money = total_foreign + total_org # 외국인+기관 합산 (ETF용)
        
        is_etf = group.iloc[0]['업종'] == 'ETF'
        
        data_dict = {
            '종목코드': code,
            '종목명': group.iloc[0]['종목명'],
            '주가수익률(%)': round(price_return, 2),
            '외인누적': total_foreign,
            '기관누적': total_org,
            '쌍끌이누적': total_smart_money
        }
        
        if is_etf:
            div_data_etf.append(data_dict)
        else:
            div_data_stock.append(data_dict)
            
    # [전략 4] 개별 주식: 주가 횡보(2% 이하) & 외국인 매집 최상위
    df_stock = pd.DataFrame(div_data_stock)
    if not df_stock.empty:
        cond_stock = (df_stock['주가수익률(%)'] <= 2.0) & (df_stock['외인누적'] > 0)
        results['stock_div'] = df_stock[cond_stock].sort_values('외인누적', ascending=False).head(3)
    else:
        results['stock_div'] = pd.DataFrame()

    # [전략 5] ETF: 주가 횡보(2% 이하) & 외국인+기관 쌍끌이 매집 최상위
    df_etf = pd.DataFrame(div_data_etf)
    if not df_etf.empty:
        cond_etf = (df_etf['주가수익률(%)'] <= 2.0) & (df_etf['쌍끌이누적'] > 0)
        results['etf_div'] = df_etf[cond_etf].sort_values('쌍끌이누적', ascending=False).head(3)
    else:
        results['etf_div'] = pd.DataFrame()

    return results, latest_date

def send_daily_briefing():
    screened_data, base_date = get_screened_stocks()
    if not screened_data:
        send_telegram_message("🚨 데이터를 불러오지 못해 스크리닝을 건너뜁니다.")
        return

    date_str = base_date.strftime('%Y-%m-%d')
    msg = f"📊 <b>오늘의 종목 발굴 브리핑</b> ({date_str})\n\n"

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

    msg += "<b>🕵️‍♂️ 3. 주식: 외인 매집 횡보주</b>\n"
    if not screened_data['stock_div'].empty:
        for _, row in screened_data['stock_div'].iterrows():
            msg += f"• {row['종목명']} (수익:{row['주가수익률(%)']}%, 외인:{row['외인누적']:,}주)\n"
    else: msg += "• 조건 만족 종목 없음\n"
    msg += "\n"

    msg += "<b>🏦 4. ETF: 바닥권 쌍끌이 매집 (New!)</b>\n"
    if not screened_data['etf_div'].empty:
        for _, row in screened_data['etf_div'].iterrows():
            msg += f"• {row['종목명']}\n  (수익:{row['주가수익률(%)']}%, 쌍끌이:{row['쌍끌이누적']:,}주)\n"
    else: msg += "• 조건 만족 종목 없음\n"

    msg += "\n💡 <i>자세한 데이터는 구글 시트를 확인하세요.</i>"
    send_telegram_message(msg)

if __name__ == "__main__":
    send_daily_briefing()