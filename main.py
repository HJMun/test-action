import os
import time
import json
import re
import requests
import pandas as pd
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
import FinanceDataReader as fdr

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GSHEET_URL = os.environ.get("GSHEET_URL")
GCP_JSON = os.environ.get("GCP_SERVICE_ACCOUNT_JSON")
GDRIVE_FOLDER_ID = os.environ.get("GDRIVE_FOLDER_ID")

APP_KEY = os.environ.get("APP_KEY")
APP_SECRET = os.environ.get("APP_SECRET")
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
    url = f"{KIS_BASE_URL}/oauth2/tokenP"
    headers = {"content-type": "application/json"}
    body = {"grant_type": "client_credentials", "appkey": APP_KEY, "appsecret": APP_SECRET}
    try:
        res = requests.post(url, headers=headers, data=json.dumps(body))
        return res.json().get("access_token")
    except Exception as e:
        print(f"토큰 발급 에러: {e}")
        return None

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

# ⭐ 추가된 함수: 네이버 금융에서 재무데이터 크롤링
def get_naver_finance(code):
    url = f"https://finance.naver.com/item/main.naver?code={code}"
    headers = {'User-Agent': 'Mozilla/5.0'}
    roe_val, lblt_rate, rsrv_rate = 0.0, 0.0, 0.0
    try:
        res = requests.get(url, headers=headers, timeout=3)
        soup = BeautifulSoup(res.text, 'html.parser')
        
        def extract_value(keyword):
            th = soup.find('th', string=re.compile(keyword))
            if th:
                tr = th.parent
                tds = tr.find_all('td')
                vals = [td.text.strip().replace(',', '') for td in tds if td.text.strip() not in ['', '-', 'N/A']]
                if vals:
                    return float(vals[-1])
            return 0.0

        roe_val = extract_value('ROE')
        lblt_rate = extract_value('부채비율')
        rsrv_rate = extract_value('유보율')
    except:
        pass
    return roe_val, lblt_rate, rsrv_rate

def fetch_market_data():
    today_str = datetime.now().strftime("%Y-%m-%d")
    print(f"[{today_str}] 시세/수급(한투API) + 재무(네이버 크롤링) 하이브리드 수집 시작...")
    
    access_token = get_kis_access_token()
    if not access_token:
        raise Exception("한투 API 토큰 발급 실패.")
    
    symbols_list = get_all_target_symbols()
    market_data = []
    
    headers = {"content-type": "application/json", "authorization": f"Bearer {access_token}", "appkey": APP_KEY, "appsecret": APP_SECRET}

    for item in symbols_list:
        code = item['종목코드']
        is_etf = item['업종'] == 'ETF'
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code}
        
        try:
            # 1. 시세 (한투)
            res_price = requests.get(f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price", headers={**headers, "tr_id": "FHKST01010100"}, params=params)
            data_price = res_price.json()
            time.sleep(0.05)
            
            # 2. 재무 (네이버) - 기존 한투 API 대신 크롤링 사용
            roe_val, lblt_rate, rsrv_rate = 0.0, 0.0, 0.0
            if not is_etf:
                roe_val, lblt_rate, rsrv_rate = get_naver_finance(code)
                
            # 3. 수급 (한투)
            frgn_net_qty, orgn_net_qty = 0, 0
            res_inv = requests.get(f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-investor", headers={**headers, "tr_id": "FHKST01010900"}, params=params)
            data_inv = res_inv.json()