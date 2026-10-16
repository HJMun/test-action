import os
import json
import pandas as pd
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials
import requests

# 환경변수 로드
GSHEET_URL = os.environ.get("GSHEET_URL")
GCP_JSON = os.environ.get("GCP_SERVICE_ACCOUNT_JSON")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
SCOPES = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']

def send_telegram_message(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try: requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message})
    except Exception: pass

def run_peter_lynch_screener():
    print("피터 린치 수동 스크리너 작동 시작...")
    
    # 1. 구글 인증 및 시트 연결
    try:
        gcp_info = json.loads(GCP_JSON, strict=False)
    except Exception:
        gcp_info = eval(GCP_JSON)
        
    if "private_key" in gcp_info:
        gcp_info["private_key"] = gcp_info["private_key"].replace("\\n", "\n")
        
    credentials = Credentials.from_service_account_info(gcp_info, scopes=SCOPES)
    gc = gspread.authorize(credentials)
    wb = gc.open_by_url(GSHEET_URL)
    
    # 2. RawData 탭에서 전체 데이터 읽어오기
    raw_sheet = wb.worksheet("RawData")
    data = raw_sheet.get_all_records()
    df = pd.DataFrame(data)
    
    if df.empty:
        print("분석할 데이터가 없습니다.")
        return

    # 구글 시트의 빈칸을 처리하고 숫자로 변환
    cols_to_numeric = ['PER', 'ROE', '시가총액_억원', '종가']
    for col in cols_to_numeric:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')
    
    # 3. 가장 최근 수집된 날짜의 데이터만 필터링
    df['날짜'] = pd.to_datetime(df['날짜'])
    latest_date = df['날짜'].max()
    latest_df = df[df['날짜'] == latest_date].copy()
    
    # 4. 피터 린치 조건 및 안전마진 적용 (제외 종목 없음)
    safe_cond = (
        (latest_df['PER'].notna()) & (latest_df['PER'] >= 2.0) & (latest_df['PER'] <= 50.0) &
        (latest_df['ROE'].notna()) & (latest_df['ROE'] >= 5.0) & (latest_df['ROE'] <= 40.0) &
        (latest_df['시가총액_억원'].notna()) & (latest_df['시가총액_억원'] >= 500)
    )
    
    filtered_df = latest_df[safe_cond].copy()
    
    # PEG 계산 및 0.1 ~ 0.5 하한/상한선 추출
    filtered_df['PEG'] = round(filtered_df['PER'] / filtered_df['ROE'], 3)
    lynch_cond = (filtered_df['PEG'] >= 0.1) & (filtered_df['PEG'] <= 0.5)
    lynch_df = filtered_df[lynch_cond].sort_values('PEG', ascending=True)
    
    # 포맷팅 정리
    lynch_df['날짜'] = lynch_df['날짜'].dt.strftime("%Y-%m-%d")
    lynch_df['종목코드'] = lynch_df['종목코드'].astype(str).str.zfill(6)
    lynch_df = lynch_df.fillna("")
    
    # 5. 구글 시트에 "PeterLynch" 탭 생성 및 덮어쓰기
    sheet_name = "PeterLynch"
    try:
        target_sheet = wb.worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        target_sheet = wb.add_worksheet(title=sheet_name, rows="1000", cols="30")
        
    target_sheet.clear()
    
    if lynch_df.empty:
        target_sheet.update([["최신 날짜 기준 피터 린치 조건을 만족하는 종목이 없습니다."]])
        send_telegram_message("🚀 [수동 추출 알림]\n피터 린치(PEG 0.1~0.5) 조건 만족 종목이 없습니다.")
    else:
        # 보기 편하게 PEG 컬럼을 앞쪽(업종 다음)으로 이동
        cols = lynch_df.columns.tolist()
        if 'PEG' in cols:
            cols.insert(5, cols.pop(cols.index('PEG')))
            lynch_df = lynch_df[cols]
            
        target_sheet.update([lynch_df.columns.values.tolist()] + lynch_df.values.tolist())
        send_telegram_message(f"🚀 [수동 추출 완료]\n피터 린치 성장주 {len(lynch_df)}개 종목이 추출되었습니다.\n구글 시트 'PeterLynch' 탭을 확인하세요.")

    print("수동 추출 및 저장 완료.")

if __name__ == "__main__":
    run_peter_lynch_screener()