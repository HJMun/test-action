import os
import requests
import pandas as pd
from datetime import datetime, timedelta

# GitHub Secrets에서 텔레그램 정보 불러오기
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

def send_telegram_message(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("텔레그램 토큰이 설정되지 않아 메시지를 출력만 합니다.")
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

    # 1. 방어형 가치주
    cond1 = (latest_df['PER'] > 0) & (latest_df['PER'] <= 10)
    cond2 = (latest_df['PBR'] > 0) & (latest_df['PBR'] <= 1)
    cond3 = (latest_df['부채비율'] > 0) & (latest_df['부채비율'] <= 100)
    results['graham'] = latest_df[cond1 & cond2 & cond3].sort_values('PBR').head(3)

    # 2. 마법공식
    mf_df = latest_df[(latest_df['PER'] > 0) & (latest_df['ROE'] > 0)].copy()
    mf_df['PER_Rank'] = mf_df['PER'].rank(ascending=True)
    mf_df['ROE_Rank'] = mf_df['ROE'].rank(ascending=False)
    mf_df['Magic_Score'] = mf_df['PER_Rank'] + mf_df['ROE_Rank']
    results['magic'] = mf_df.sort_values('Magic_Score').head(3)

    # 3. 피터 린치 성장주
    lynch_df = latest_df[(latest_df['PER'] > 0) & (latest_df['ROE'] > 0)].copy()
    lynch_df['PEG'] = lynch_df['PER'] / lynch_df['ROE']
    cond_lynch = (lynch_df['PEG'] > 0) & (lynch_df['PEG'] <= 0.5)
    results['lynch'] = lynch_df[cond_lynch].sort_values('PEG').head(3)

    # 4. 외국인 수급 다이버전스
    fourteen_days_ago = latest_date - timedelta(days=14)
    period_df = df[df['날짜'] >= fourteen_days_ago].copy()
    
    div_data = []
    for code, group in period_df.groupby('종목코드'):
        if len(group) < 2: continue
        group = group.sort_values('날짜')
        start_price = group.iloc[0]['종가']
        end_price = group.iloc[-1]['종가']
        if start_price == 0: continue
        
        price_return = ((end_price - start_price) / start_price) * 100
        total_foreign = group['외국인순매수'].sum()
        
        div_data.append({
            '종목코드': code,
            '종목명': group.iloc[0]['종목명'],
            '주가수익률(%)': round(price_return, 2),
            '누적외국인순매수': total_foreign,
        })
        
    div_df = pd.DataFrame(div_data)
    if not div_df.empty:
        cond_div = (div_df['주가수익률(%)'] <= 2.0) & (div_df['누적외국인순매수'] > 0)
        results['foreigner'] = div_df[cond_div].sort_values('누적외국인순매수', ascending=False).head(3)
    else:
        results['foreigner'] = pd.DataFrame()

    return results, latest_date

def send_daily_briefing():
    screened_data, base_date = get_screened_stocks()
    if not screened_data:
        send_telegram_message("🚨 데이터를 불러오지 못해 스크리닝을 건너뜁니다.")
        return

    date_str = base_date.strftime('%Y-%m-%d')
    msg = f"📊 <b>오늘의 종목 발굴 브리핑</b> ({date_str})\n\n"

    # 전략 1
    msg += "<b>🏆 1. 방어형 가치주 (PER 10↓, PBR 1↓)</b>\n"
    if not screened_data['graham'].empty:
        for _, row in screened_data['graham'].iterrows():
            msg += f"• {row['종목명']} (PER: {row['PER']:.1f}, PBR: {row['PBR']:.1f})\n"
    else:
        msg += "• 조건 만족 종목 없음 (또는 데이터 부족)\n"
    msg += "\n"

    # 전략 2
    msg += "<b>🧙‍♂️ 2. 마법공식 (저PER + 고ROE)</b>\n"
    if not screened_data['magic'].empty:
        for _, row in screened_data['magic'].iterrows():
            msg += f"• {row['종목명']} (PER: {row['PER']:.1f}, ROE: {row['ROE']:.1f})\n"
    else:
        msg += "• 조건 만족 종목 없음\n"
    msg += "\n"

    # 전략 3
    msg += "<b>🚀 3. 피터린치 성장주 (PEG 0.5↓)</b>\n"
    if not screened_data['lynch'].empty:
        for _, row in screened_data['lynch'].iterrows():
            msg += f"• {row['종목명']} (PEG: {row['PEG']:.2f}, ROE: {row['ROE']:.1f})\n"
    else:
        msg += "• 조건 만족 종목 없음\n"
    msg += "\n"

    # 전략 4
    msg += "<b>🕵️‍♂️ 4. 외국인 매집 횡보주</b>\n"
    if not screened_data['foreigner'].empty:
        for _, row in screened_data['foreigner'].iterrows():
            msg += f"• {row['종목명']} (수익률: {row['주가수익률(%)']}%, 순매수: {row['누적외국인순매수']:,}주)\n"
    else:
        msg += "• 조건 만족 종목 없음\n"

    msg += "\n💡 <i>자세한 데이터는 구글 시트를 확인하세요.</i>"
    send_telegram_message(msg)

if __name__ == "__main__":
    send_daily_briefing()