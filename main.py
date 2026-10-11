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

SCOPES = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']

def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try: requests.post(url, json=payload)
    except: pass

def get_kis_access_token():
    url = f"{KIS_BASE_URL}/oauth2/tokenP"
    headers = {"content-type": "application/json"}
    body = {"grant_type": "client_credentials", "appkey": APP_KEY, "appsecret": APP_SECRET}
    try: return requests.post(url, headers=headers, data=json.dumps(body)).json().get("access_token")
    except: return None

def get_all_target_symbols():
    print("시장 전체 상장 종목 및 ETF 목록을 불러옵니다...")
    # KRX 종목 (시장구분과 섹터 분리)
    krx_df = fdr.StockListing('KRX')
    krx_df = krx_df[['Code', 'Name', 'Market', 'Sector']].rename(
        columns={'Code': '종목코드', 'Name': '종목명', 'Market': '시장구분', 'Sector': '업종'}
    )
    krx_df['업종'] = krx_df['업종'].fillna('기타') # 섹터가 없는 경우 처리
    krx_df['종목코드'] = krx_df['종목코드'].astype(str).str.zfill(6) # 6자리 문자 고정
    
    # ETF 종목
    etf_df = fdr.StockListing('ETF/KR')
    etf_df = etf_df[['Symbol', 'Name']].rename(columns={'Symbol': '종목코드', 'Name': '종목명'})
    etf_df['시장구분'] = 'ETF'
    etf_df['업종'] = 'ETF'
    etf_df['종목코드'] = etf_df['종목코드'].astype(str).str.zfill(6)
    
    final_df = pd.concat([krx_df, etf_df], ignore_index=True).drop_duplicates(subset=['종목코드'])
    return final_df.to_dict('records')

def safe_kis_request(url, headers, params):
    for _ in range(3):
        try:
            res = requests.get(url, headers=headers, params=params, timeout=5)
            data = res.json()
            if data.get('rt_cd') != '0' and ('초과' in data.get('msg1', '') or 'EGW' in data.get('msg_cd', '')):
                time.sleep(1.0)
                continue
            return data
        except: time.sleep(1.0)
    return {}

def get_naver_finance(code):
    url = f"https://finance.naver.com/item/main.naver?code={code}"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    lblt_rate, rsrv_rate, div_yield = None, None, None # 기본값을 0이 아닌 None(결측치)으로
    try:
        res = requests.get(url, headers=headers, timeout=5)
        soup = BeautifulSoup(res.text, 'html.parser')
        
        def extract_value(keyword):
            th = soup.find('th', string=re.compile(keyword))
            if th:
                tr = th.parent
                tds = tr.find_all('td')
                vals = [td.text.strip().replace(',', '').replace('%', '') for td in tds if td.text.strip() not in ['', '-', 'N/A']]
                if vals:
                    try: return float(vals[-1])
                    except: pass
            return None
            
        lblt_rate = extract_value('부채비율')
        rsrv_rate = extract_value('유보율')
        div_yield = extract_value('배당수익률') # 배당수익률 추가
    except: pass
    return lblt_rate, rsrv_rate, div_yield

def fetch_market_data():
    today_str = datetime.now().strftime("%Y-%m-%d")
    print(f"[{today_str}] 프로 버전 데이터 수집 시작...")
    
    access_token = get_kis_access_token()
    symbols_list = get_all_target_symbols()
    market_data = []
    
    headers = {"content-type": "application/json", "authorization": f"Bearer {access_token}", "appkey": APP_KEY, "appsecret": APP_SECRET}

    for item in symbols_list:
        code = item['종목코드']
        is_etf = item['업종'] == 'ETF'
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code}
        
        try:
            # 1. 시세 및 OHLC, 시총, 거래량
            data_price = safe_kis_request(f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price", {**headers, "tr_id": "FHKST01010100"}, params)
            time.sleep(0.06) 
            
            output_price = data_price.get('output', {}) if data_price.get('rt_cd') == '0' else {}
            
            # OHLC 및 변동성
            close_price = int(output_price.get('stck_prpr', 0))
            open_price = int(output_price.get('stck_oprc', 0))
            high_price = int(output_price.get('stck_hgpr', 0))
            low_price = int(output_price.get('stck_lwpr', 0))
            change_rate = float(output_price.get('prdy_ctrt', 0))
            
            # 규모 및 유동성
            market_cap = float(output_price.get('hts_avls', 0)) # 시가총액(억원)
            volume = int(output_price.get('acml_vol', 0))
            trade_value_100m = round((volume * close_price) / 100000000, 2) # 거래대금(억원)
            
            # 재무 비율 (없으면 0이 아닌 None 처리)
            per = float(output_price.get('per')) if output_price.get('per') else None
            pbr = float(output_price.get('pbr')) if output_price.get('pbr') else None
            
            # 2. 수급 데이터 (세부 기관 포함)
            data_inv = safe_kis_request(f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-investor", {**headers, "tr_id": "FHKST01010900"}, params)
            time.sleep(0.06)
            
            frgn_qty, orgn_qty, pnsn_qty, pe_qty, fncg_qty = 0, 0, 0, 0, 0
            if data_inv.get('rt_cd') == '0':
                out_inv = data_inv.get('output', [])
                if len(out_inv) > 0:
                    ti = out_inv[0]
                    def g(k): return int(ti.get(k, 0)) if ti.get(k) else 0
                    frgn_qty = g('frgn_ntby_qty')     # 외국인
                    orgn_qty = g('orgn_ntby_qty')     # 기관합계
                    pnsn_qty = g('prps_ntby_qty')     # 연기금
                    pe_qty   = g('pe_fund_ntby_qty')  # 사모펀드
                    fncg_qty = g('fncg_cmn_ntby_qty') # 금융투자

            # 3. 재무 데이터 크롤링 (ROE 공식 + 네이버 배당/부채)
            roe_val, lblt_rate, rsrv_rate, div_yield = None, None, None, None
            if not is_etf and close_price > 0:
                if per and pbr and per > 0:
                    roe_val = round((pbr / per) * 100, 2)
                lblt_rate, rsrv_rate, div_yield = get_naver_finance(code)
                time.sleep(0.1)
                
            # 4. 데이터 병합
            market_data.append({
                "날짜": today_str,
                "종목코드": code, # 6자리 문자열
                "종목명": item['종목명'],
                "시장구분": item['시장구분'],
                "업종": item['업종'],
                "시가총액_억원": market_cap,
                "거래대금_억원": trade_value_100m,
                "등락률(%)": change_rate,
                "종가": close_price,
                "시가": open_price,
                "고가": high_price,
                "저가": low_price,
                "거래량": volume,
                "외인매수_억원": round(frgn_qty * close_price / 100000000, 2),
                "기관매수_억원": round(orgn_qty * close_price / 100000000, 2),
                "연기금매수_억원": round(pnsn_qty * close_price / 100000000, 2),
                "사모펀드매수_억원": round(pe_qty * close_price / 100000000, 2),
                "금융투자매수_억원": round(fncg_qty * close_price / 100000000, 2),
                "PER": per,
                "PBR": pbr,
                "ROE": roe_val, 
                "부채비율": lblt_rate, 
                "유보율": rsrv_rate,
                "배당수익률(%)": div_yield
            })
        except Exception as e:
            print(f"{code} 수집 에러: {e}")
            continue

    return pd.DataFrame(market_data)

def save_to_hybrid_storage(new_df):
    today_str = datetime.now().strftime("%Y-%m-%d")
    csv_filename = "market_data_archive.csv"
    
    # NaN 값을 빈 문자열로 안전하게 변환하여 구글 시트 오류 방지
    sheet_df = new_df.fillna("") 
    
    if os.path.exists(csv_filename):
        try:
            archive_df = pd.read_csv(csv_filename, dtype={'종목코드': str})
            archive_df['종목코드'] = archive_df['종목코드'].astype(str).str.zfill(6)
            archive_df = pd.concat([archive_df, new_df], ignore_index=True)
            archive_df.drop_duplicates(subset=['날짜', '종목코드'], keep='last', inplace=True)
            archive_df.to_csv(csv_filename, index=False, encoding='utf-8-sig')
        except: new_df.to_csv(csv_filename, index=False, encoding='utf-8-sig')
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
            drive_service.files().create(body={'name': csv_filename, 'parents': [GDRIVE_FOLDER_ID]}, media_body=media, fields='id').execute()
        else:
            drive_service.files().update(fileId=csv_items[0].get('id'), media_body=media).execute()

        gc = gspread.authorize(credentials)
        sheet = gc.open_by_url(GSHEET_URL).worksheet("RawData")
        
        existing_data = sheet.get_all_records()
        existing_df = pd.DataFrame(existing_data)
        
        if not existing_df.empty:
            existing_df['날짜'] = pd.to_datetime(existing_df['날짜'])
            cutoff_date = pd.to_datetime(today_str) - timedelta(days=30)
            recent_df = existing_df[existing_df['날짜'] >= cutoff_date].copy()
            recent_df['날짜'] = recent_df['날짜'].dt.strftime("%Y-%m-%d")
            recent_df['종목코드'] = recent_df['종목코드'].astype(str).str.zfill(6)
            final_df = pd.concat([recent_df, sheet_df], ignore_index=True)
            final_df.drop_duplicates(subset=['날짜', '종목코드'], keep='last', inplace=True)
        else:
            final_df = sheet_df
            
        sheet.clear()
        sheet.update([final_df.columns.values.tolist()] + final_df.values.tolist())
    except Exception as e:
        print(f"구글 저장 실패: {e}")
        raise e

if __name__ == "__main__":
    try:
        daily_df = fetch_market_data()
        save_to_hybrid_storage(daily_df)
        send_telegram_message(f"✅ [프로 버전 수집 완료]\n날짜: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n종목 수: {len(daily_df):,}개")
    except Exception as e:
        send_telegram_message(f"🚨 [수집 에러]\n{str(e)}")