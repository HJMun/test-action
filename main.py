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
    try:
        requests.post(url, json=payload)
    except Exception:
        pass

def get_kis_access_token():
    url = f"{KIS_BASE_URL}/oauth2/tokenP"
    headers = {"content-type": "application/json"}
    body = {"grant_type": "client_credentials", "appkey": APP_KEY, "appsecret": APP_SECRET}
    try:
        res = requests.post(url, headers=headers, data=json.dumps(body))
        return res.json().get("access_token")
    except Exception:
        return None

def get_all_target_symbols():
    print("시장 전체 상장 종목 및 ETF 목록을 불러옵니다...")
    krx_df = fdr.StockListing('KRX')
    try:
        desc_df = fdr.StockListing('KRX-DESC')
        krx_df = pd.merge(krx_df, desc_df[['Symbol', 'Sector']], left_on='Code', right_on='Symbol', how='left')
    except Exception:
        pass
    if 'Sector' not in krx_df.columns:
        krx_df['Sector'] = '기타'
    krx_df = krx_df[['Code', 'Name', 'Market', 'Sector']].rename(columns={'Code': '종목코드', 'Name': '종목명', 'Market': '시장구분', 'Sector': '업종'})
    krx_df['업종'] = krx_df['업종'].fillna('기타')
    krx_df['종목코드'] = krx_df['종목코드'].astype(str).str.zfill(6)
    
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
        except Exception:
            time.sleep(1.0)
    return {}

def fetch_market_data():
    today_str = datetime.now().strftime("%Y-%m-%d")
    print(f"[{today_str}] 프로 버전 수집 시작 (100% 한투 API 의존 모드)...")
    access_token = get_kis_access_token()
    symbols_list = get_all_target_symbols()
    market_data = []
    headers = {"content-type": "application/json", "authorization": f"Bearer {access_token}", "appkey": APP_KEY, "appsecret": APP_SECRET}
    
    for item in symbols_list:
        code = item['종목코드']
        is_etf = item['업종'] == 'ETF'
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code}
        
        try:
            data_price = safe_kis_request(f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price", {**headers, "tr_id": "FHKST01010100"}, params)
            time.sleep(0.06) 
            output_price = data_price.get('output', {}) if data_price.get('rt_cd') == '0' else {}
            close_price = int(output_price.get('stck_prpr', 0))
            open_price = int(output_price.get('stck_oprc', 0))
            high_price = int(output_price.get('stck_hgpr', 0))
            low_price = int(output_price.get('stck_lwpr', 0))
            change_rate = float(output_price.get('prdy_ctrt', 0))
            market_cap = float(output_price.get('hts_avls', 0)) 
            volume = int(output_price.get('acml_vol', 0))
            trade_value_100m = round((volume * close_price) / 100000000, 2) 
            per = float(output_price.get('per')) if output_price.get('per') else None
            pbr = float(output_price.get('pbr')) if output_price.get('pbr') else None
            
            data_inv = safe_kis_request(f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-investor", {**headers, "tr_id": "FHKST01010900"}, params)
            time.sleep(0.06)
            
            # ⭐ 수정 1: KIS API 공식 수급 변수명으로 완벽 복구
            frgn_qty, orgn_qty, pnsn_qty, pe_qty, fncg_qty = 0, 0, 0, 0, 0
            if data_inv.get('rt_cd') == '0':
                out_inv = data_inv.get('output', [])
                if len(out_inv) > 0:
                    ti = out_inv[0]
                    def g(k): return int(ti.get(k, 0)) if ti.get(k) else 0
                    frgn_qty = g('frgn_ntby_qty')
                    orgn_qty = g('orgn_ntby_qty')
                    pnsn_qty = g('prps_ntby_qty')     # 연기금
                    pe_qty   = g('pe_fund_ntby_qty')  # 사모펀드
                    fncg_qty = g('fncg_cmn_ntby_qty') # 금융투자

            # ⭐ 수정 2: 크롤링 차단 회피를 위해 한투 재무 API 전면 도입
            roe_val, lblt_rate, rsrv_rate, div_yield = None, None, None, 0.0
            if not is_etf and close_price > 0:
                if per is not None and pbr is not None and per != 0:
                    roe_val = round((pbr / per) * 100, 2)
                    
                data_fin = safe_kis_request(f"{KIS_BASE_URL}/uapi/domestic-stock/v1/finance/financial-ratio", {**headers, "tr_id": "FHKST1060000C"}, params)
                if data_fin.get('rt_cd') == '0':
                    out_fin = data_fin.get('output', {})
                    if out_fin.get('lblt_rate'):
                        try: lblt_rate = float(out_fin.get('lblt_rate'))
                        except Exception: pass
                    if out_fin.get('rsrv_rate'):
                        try: rsrv_rate = float(out_fin.get('rsrv_rate'))
                        except Exception: pass
                time.sleep(0.06)
                
            market_data.append({
                "날짜": today_str,
                "종목코드": code, 
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
    sheet_df = new_df.fillna("") 
    
    if os.path.exists(csv_filename):
        try:
            archive_df = pd.read_csv(csv_filename, dtype={'종목코드': str})
            archive_df['종목코드'] = archive_df['종목코드'].astype(str).str.zfill(6)
            archive_df = pd.concat([archive_df, new_df], ignore_index=True)
            archive_df.drop_duplicates(subset=['날짜', '종목코드'], keep='last', inplace=True)
            archive_df.to_csv(csv_filename, index=False, encoding='utf-8-sig')
        except Exception:
            new_df.to_csv(csv_filename, index=False, encoding='utf-8-sig')
    else:
        new_df.to_csv(csv_filename, index=False, encoding='utf-8-sig')
        
    try:
        try:
            gcp_info = json.loads(GCP_JSON, strict=False)
        except Exception:
            gcp_info = eval(GCP_JSON)
            
        if "private_key" in gcp_info:
            gcp_info["private_key"] = gcp_info["private_key"].replace("\\n", "\n")
            
        credentials = Credentials.from_service_account_info(gcp_info, scopes=SCOPES)
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
        raise e