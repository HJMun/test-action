import os
import time
import json
import requests
import pandas as pd
from datetime import datetime, timedelta
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
import FinanceDataReader as fdr

# ==========================================
# 1. 환경 변수 및 시크릿 키 로드 (GitHub Secrets)
# ==========================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GSHEET_URL = os.environ.get("GSHEET_URL")
GCP_JSON = os.environ.get("GCP_SERVICE_ACCOUNT_JSON")
GDRIVE_FOLDER_ID = os.environ.get("GDRIVE_FOLDER_ID")

APP_KEY = os.environ.get("APP_KEY")
APP_SECRET = os.environ.get("APP_SECRET")

# 실전투자 서버 URL로 변경 완료
# (모의투자용 키를 사용하시는 경우 https://openapivts.koreainvestment.com:29443 로 변경)
KIS_BASE_URL = "https://openapi.koreainvestment.com:9443"

SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive'
]

def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"텔레그램 알림 실패: {e}")

def get_kis_access_token():
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
        return res.json().get("access_token")
    except Exception as e:
        print(f"토큰 발급 에러: {e}")
        return None

# ==========================================
# 2. 전체 종목 리스트 수집 (코스피, 코스닥, ETF)
# ==========================================
def get_all_target_symbols():
    print("시장 전체 상장 종목 및 ETF 목록을 불러옵니다...")
    krx_df = fdr.StockListing('KRX')
    krx_df = krx_df[['Code', 'Name', 'Market']].rename(columns={'Code': '종목코드', 'Name': '종목명', 'Market': '업종'})
    
    etf_df = fdr.StockListing('ETF/KR')
    etf_df = etf_df[['Symbol', 'Name']].rename(columns={'Symbol': '종목코드', 'Name': '종목명'})
    etf_df['업종'] = 'ETF'

    final_symbols_df = pd.concat([krx_df, etf_df], ignore_index=True)
    final_symbols_df.drop_duplicates(subset=['종목코드'], inplace=True)
    
    return final_symbols_df.to_dict('records')

# ==========================================
# 3. 데이터 수집 로직 (한투 API 연동)
# ==========================================
def fetch_market_data():
    today_str = datetime.now().strftime("%Y-%m-%d")
    print(f"[{today_str}] 한국투자증권 API 전체 종목 데이터 수집 시작...")
    
    access_token = get_kis_access_token()
    if not access_token:
        raise Exception("한투 API 토큰 발급에 실패하여 수집을 중단합니다.")
    
    symbols_list = get_all_target_symbols()
    market_data = []
    
    headers = {
        "content-type": "application/json",
        "authorization": f"Bearer {access_token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "FHKST01010100" 
    }

    for item in symbols_list:
        code = item['종목코드']
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": code
        }
        
        try:
            url = f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price"
            res = requests.get(url, headers=headers, params=params)
            data = res.json()
            
            if data.get('rt_cd') == '0':
                output = data.get('output', {})
                
                is_etf = item['업종'] == 'ETF'
                
                row = {
                    "날짜": today_str,
                    "종목코드": code,
                    "종목명": item['종목명'],
                    "업종": item['업종'],
                    "종가": int(output.get('stck_prpr', 0)),
                    "외국인순매수": 0, 
                    "기관순매수": 0,
                    "PER": float(output.get('per', 0)) if not is_etf and output.get('per') else 0.0,
                    "PBR": float(output.get('pbr', 0)) if not is_etf and output.get('pbr') else 0.0,
                    "ROE": 0.0, 
                    "부채비율": 0.0, 
                    "유보율": 0.0
                }
                market_data.append(row)
                
            time.sleep(0.06)
            
        except Exception as e:
            print(f"{code} ({item['종목명']}) 수집 에러: {e}")
            continue

    df = pd.DataFrame(market_data)
    return df

# ==========================================
# 4. 투트랙 저장 (CSV in Drive + Google Sheets)
# ==========================================
def save_to_hybrid_storage(new_df):
    today_str = datetime.now().strftime("%Y-%m-%d")
    csv_filename = "market_data_archive.csv"
    
    if os.path.exists(csv_filename):
        archive_df = pd.read_csv(csv_filename)
        archive_df = pd.concat([archive_df, new_df], ignore_index=True)
        archive_df.drop_duplicates(subset=['날짜', '종목코드'], keep='last', inplace=True)
        archive_df.to_csv(csv_filename, index=False, encoding='utf-8-sig')
    else:
        new_df.to_csv(csv_filename, index=False, encoding='utf-8-sig')
    
    try:
        credentials = Credentials.from_service_account_info(eval(GCP_JSON), scopes=SCOPES)
        drive_service = build('drive', 'v3', credentials=credentials)
        
        query_csv = f"name='{csv_filename}' and '{GDRIVE_FOLDER_ID}' in parents and trashed=false"
        csv_results = drive_service.files().list(q=query_csv, spaces='drive').execute()
        csv_items = csv_results.get('files', [])
        
        media = MediaFileUpload(csv_filename, mimetype='text/csv', resumable=True)
        if not csv_items:
            file_metadata = {'name': csv_filename, 'parents': [GDRIVE_FOLDER_ID]}
            drive_service.files().create(body=file_metadata, media_body=media, fields='id').execute()
        else:
            file_id = csv_items[0].get('id')
            drive_service.files().update(fileId=file_id, media_body=media).execute()

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
            final_df.drop_duplicates(subset=['날짜', '종목코드'], keep='last', inplace=True)
        else:
            final_df = new_df
            
        sheet.clear()
        sheet.update([final_df.columns.values.tolist()] + final_df.values.tolist())
        
    except Exception as e:
        print(f"구글 저장 실패: {e}")
        raise e

# ==========================================
# 5. 메인 실행 블록
# ==========================================
if __name__ == "__main__":
    try:
        daily_df = fetch_market_data()
        save_to_hybrid_storage(daily_df)
        
        success_msg = f"✅ [전체 데이터 수집 완료]\n날짜: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n수집 종목 수: {len(daily_df):,}개\n구글 드라이브 업데이트 성공"
        send_telegram_message(success_msg)
        print("작업이 성공적으로 완료되었습니다.")
        
    except Exception as e:
        error_msg = f"🚨 [데이터 수집 에러]\n내용: {str(e)}"
        send_telegram_message(error_msg)
        print(error_msg)