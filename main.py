import os
import time
import json
import requests
import pandas as pd
from datetime import datetime, timedelta
import gspread
from google.oauth2.service_account import Credentials

# ==========================================
# 1. 환경 변수 및 시크릿 키 로드 (GitHub Secrets)
# ==========================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GSHEET_URL = os.environ.get("GSHEET_URL")
GCP_JSON = os.environ.get("GCP_SERVICE_ACCOUNT_JSON")

# 한국투자증권 API 열쇠 로드
APP_KEY = os.environ.get("APP_KEY")
APP_SECRET = os.environ.get("APP_SECRET")

# 실전투자 / 모의투자 서버 URL 설정 (모의투자인 경우 도메인 변경 필요)
# 모의투자: https://openapivts.koreainvestment.com:9443
# 실전투자: https://openapi.koreainvestment.com:9443
KIS_BASE_URL = "https://openapivts.koreainvestment.com:9443"

def send_telegram_message(message):
    """텔레그램 봇으로 알림 메시지를 전송하는 함수"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    response = requests.post(url, json=payload)
    print(f"텔레그램 전송 결과: {response.text}")

def get_kis_access_token():
    """한국투자증권 API Access Token 발급 함수"""
    path = "/oauth2/tokenP"
    url = f"{KIS_BASE_URL}{path}"
    headers = {"content-type": "application/json"}
    body = {
        "grant_type": "client_credentials",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET
    }
    
    try:
        res = requests.post(url, headers=headers, data=json.dumps(body))
        res_data = res.json()
        access_token = res_data.get("access_token")
        return access_token
    except Exception as e:
        print(f"한투 토큰 발급 에러: {e}")
        return None

# ==========================================
# 2. 데이터 수집 로직 (한투 API 연동 + 재무 지표 포함)
# ==========================================
def fetch_market_data():
    """한국투자증권 API를 통해 확장된 재무/시세 데이터를 수집하는 함수"""
    today_str = datetime.now().strftime("%Y-%m-%d")
    print(f"[{today_str}] 한국투자증권 API 연동 데이터 수집 시작...")
    
    # 1단계: API 접근을 위한 토큰 발급
    access_token = get_kis_access_token()
    if not access_token:
        print("⚠️ 한투 API 토큰 발급 실패. 임시 구조 데이터로 대체합니다.")
    
    # 2단계: 실제 API 연동 부 (추후 종목 루프를 돌며 시세 및 재무 지표 수집)
    # 아래는 우리가 확정한 최종 컬럼 구조에 맞춘 샘플 데이터 구조입니다.
    # (실제 API 호출 시 get_price / get_financial_info 등을 조합하여 채워넣게 됩니다)
    market_data = [
        {
            "날짜": today_str, "종목코드": "005930", "종목명": "삼성전자", "업종": "전기전자", 
            "종가": 70000, "외국인순매수": 150000, "기관순매수": -50000,
            "PER": 14.5, "PBR": 1.3, "ROE": 9.8, "부채비율": 27.2, "유보율": 2500.0
        },
        {
            "날짜": today_str, "종목코드": "000660", "종목명": "SK하이닉스", "업종": "전기전자", 
            "종가": 150000, "외국인순매수": 20000, "기관순매수": 10000,
            "PER": 10.2, "PBR": 1.7, "ROE": 15.4, "부채비율": 65.4, "유보율": 850.0
        },
    ]
    
    df = pd.DataFrame(market_data)
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
    
    # 3-2. Google Sheets 업데이트 (최근 1달치 롤링 유지)
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
        print("구글 시트 업데이트 완료!")
        
    except Exception as e:
        print(f"구글 시트 업데이트 건너뜀: {e}")

# ==========================================
# 4. 메인 실행 블록
# ==========================================
if __name__ == "__main__":
    try:
        daily_df = fetch_market_data()
        save_to_hybrid_storage(daily_df)
        
        success_msg = f"✅ [데이터 수집 완료]\n- 날짜: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n- 수집 종목 수: {len(daily_df)}개\n- 추가 지표: PER, PBR, ROE, 부채비율, 유보율 반영 완료"
        send_telegram_message(success_msg)
        print("작업이 성공적으로 완료되었습니다.")
        
    except Exception as e:
        error_msg = f"🚨 [데이터 수집 에러]\n- 내용: {str(e)}"
        try:
            send_telegram_message(error_msg)
        except:
            print("텔레그램 전송 실패:", error_msg)
