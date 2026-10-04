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
KIS_BASE_URL = "https://openapi.koreainvestment.com:9443" # 실전투자 URL

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
# 3. 데이터 수집 로직 (한투 API 시세 + 재무 투트랙 연동)
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
        "appsecret": APP_SECRET
    }

    for item in symbols_list:
        code = item['종목코드']
        is_etf = item['업종'] == 'ETF'
        
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": code
        }
        
        try:
            # [API 1차 호출] 주식 현재가 및 기본 지표 (PER, PBR)
            url_price = f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price"
            headers_price = headers.copy()
            headers_price["tr_id"] = "FHKST01010100"
            
            res_price = requests.get(url_price, headers=headers_price, params=params)
            data_price = res_price.json()
            time.sleep(0.06) # API 차단 방지용 기본 딜레이
            
            # 재무 지표 기본값 세팅
            roe_val, lblt_rate, rsrv_rate = 0.0, 0.0, 0.0
            
            # [API 2차 호출] 기업 재무비율 (ROE, 부채비율, 유보율) - ETF는 건너뜀
            if not is_etf:
                url_fin = f"{KIS_BASE_URL}/uapi/domestic-stock/v1/finance/financial-ratio"
                headers_fin = headers.copy()
                headers_fin["tr_id"] = "FHKST1060000C" 
                
                res_fin = requests.get(url_fin, headers=headers_fin, params=params)
                data_fin = res_fin.json()
                
                if data_fin.get('rt_cd') == '0':
                    output_fin = data_fin.get('output', {})
                    # KIS API 응답 키값 파싱 (비어있거나 에러일 경우 0.0 처리)
                    try: roe_val = float(output_fin.get('roe_val', 0.0))
                    except: pass
                    try: lblt_rate = float(output_fin.get('lblt_rate', 0.0))
                    except: pass
                    try: rsrv_rate = float(output_fin.get('rsrv_rate', 0.0))
                    except: pass
                    
                time.sleep(0.06) # 재무 데이터 조회 후 추가 딜레이
            
            # 수집된 데이터를 하나의 행(Row)으로 병합
            if data_price.get('rt_cd') == '0':
                output_price = data_price.get('output', {})
                
                row = {
                    "날짜": today_str,
                    "종목코드": code,
                    "종목명": item['종목명'],
                    "업종": item['업종'],
                    "종가": int(output_price.get('stck_prpr', 0)),
                    "외국인순매수": 0, 
                    "기관순매수": 0,
                    "PER": float(output_price.get('per', 0)) if not is_etf and output_price.get('per') else 0.0,
                    "PBR": float(output_price.get('pbr', 0)) if not is_etf and output_price.get('pbr') else 0.0,
                    "ROE": roe_val, 
                    "부채비율": lblt_rate, 
                    "유보율": rsrv_rate
                }
                market_data.append(row)
                
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
        try:
            archive_df = pd.read_csv(csv_filename)
            archive_df = pd.concat([archive_df, new_df], ignore_index=True)
            archive_df.drop_duplicates(subset=['날짜', '종목코드'], keep='last', inplace=True)
            archive_df.to_csv(csv_filename, index=False, encoding='utf-8-sig')
        except pd.errors.EmptyDataError:
            # 사용자가 빈 깡통 파일을 올렸을 경우 발생하는 에러를 예외 처리하여 새 데이터로 덮어씀
            new_df.to_csv(csv_filename, index=False, encoding='utf-8-sig')
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