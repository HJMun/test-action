import os
import time
import requests
import pandas as pd
from datetime import datetime, timedelta
import gspread
from google.oauth2.service_account import Credentials

# ==========================================
# 1. 환경 변수 및 시크릿 키 로드 (GitHub Secrets에서 가져옴)
# ==========================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GSHEET_URL = os.environ.get("GSHEET_URL") # 예: https://docs.google.com/spreadsheets/d/abc...
GCP_JSON = os.environ.get("GCP_SERVICE_ACCOUNT_JSON") # 구글 API 인증 JSON

def send_telegram_message(message):
    """텔레그램 봇으로 알림 메시지를 전송하는 함수"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    
    # 🚨 여기에 에러 추적기를 달았습니다!
    response = requests.post(url, json=payload)
    print(f"텔레그램 전송 결과: {response.text}")

# ==========================================
# 2. 데이터 수집 로직 (한국투자증권 API - 초당 제한 고려)
# ==========================================
def fetch_market_data():
    """전 종목 데이터를 수집하는 함수 (API 호출)"""
    today_str = datetime.now().strftime("%Y-%m-%d")
    print(f"[{today_str}] 전 종목 데이터 수집 시작...")
    
    # 임시 테스트용 데이터 (실제로는 여기서 한투 API를 호출하여 2,500개 종목 수집)
    # 팁: for문을 돌며 API를 호출할 때 time.sleep(0.1) 을 넣어 초당 10건으로 제한해야 합니다.
    mock_data = [
        {"날짜": today_str, "종목코드": "005930", "종목명": "삼성전자", "업종": "전기전자", "종가": 70000, "외국인순매수": 150000, "기관순매수": -50000},
        {"날짜": today_str, "종목코드": "000660", "종목명": "SK하이닉스", "업종": "전기전자", "종가": 150000, "외국인순매수": 20000, "기관순매수": 10000},
    ]
    
    # 금융주를 배제하지 않고 모두 가져와 DataFrame으로 변환
    df = pd.DataFrame(mock_data)
    return df

# ==========================================
# 3. 투트랙 하이브리드 저장 (Google Sheets + CSV)
# ==========================================
def save_to_hybrid_storage(new_df):
    today_str = datetime.now().strftime("%Y-%m-%d")
    
    # 3-1. CSV 아카이브 업데이트 (전체 데이터 누적)
    csv_filename = "market_data_archive.csv"
    if os.path.exists(csv_filename):
        new_df.to_csv(csv_filename, mode='a', header=False, index=False, encoding='utf-8-sig')
    else:
        new_df.to_csv(csv_filename, index=False, encoding='utf-8-sig')
    
    # 3-2. Google Sheets 업데이트 (최근 1달치만 롤링 유지)
    try:
        credentials = Credentials.from_service_account_info(eval(GCP_JSON), scopes=['https://www.googleapis.com/auth/spreadsheets'])
        gc = gspread.authorize(credentials)
        sheet = gc.open_by_url(GSHEET_URL).worksheet("RawData")
        
        existing_data = sheet.get_all_records()
        existing_df = pd.DataFrame(existing_data)
        
        if not existing_df.empty:
            existing_df['날짜'] = pd.to_datetime(existing_df['날짜'])
            cutoff_date = pd.to_datetime(today_str) - timedelta(days=30)
            recent_df = existing_df[existing_df['날짜'] >= cutoff_date].copy()
            recent_df['날짜'] = recent_df['날짜'].dt.strftime("%Y-%m-%d")
            final_df = pd.concat([recent_df, new_df], ignore_index=True)
        else:
            final_df = new_df
            
        sheet.clear()
        sheet.update([final_df.columns.values.tolist()] + final_df.values.tolist())
        
    except Exception as e:
        print(f"구글 시트 업데이트 건너뜀(시크릿 미설정 등): {e}")

# ==========================================
# 4. 메인 실행 블록 (알림 시스템 포함)
# ==========================================
if __name__ == "__main__":
    try:
        daily_df = fetch_market_data()
        save_to_hybrid_storage(daily_df)
        
        success_msg = f"✅ [데이터 수집 완료]\n- 날짜: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n- 수집 종목 수: {len(daily_df)}개"
        send_telegram_message(success_msg)
        print("작업이 성공적으로 완료되었습니다.")
        
    except Exception as e:
        error_msg = f"🚨 [데이터 수집 에러]\n- 내용: {str(e)}"
        try:
            send_telegram_message(error_msg)
        except:
            print("텔레그램 전송 실패:", error_msg)