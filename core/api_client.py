"""
한국투자증권 API 클라이언트
"""
import requests
import json
import hashlib
import time
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List
from config.settings import config
from utils.logger import logger
import os
from dotenv import load_dotenv
from pathlib import Path

class KISAPIClient:
    """한국투자증권 REST API 클라이언트"""
    
    def __init__(self):
        # .env 로드 (필요하다면)
        env_path = Path(__file__).parent.parent / ".env"
        load_dotenv(dotenv_path=env_path)
        
        # 곧바로 환경 변수에서 읽어온다
        self.base_url   = os.getenv("KIS_BASE_URL")
        self.app_key    = os.getenv("KIS_APP_KEY")
        self.app_secret = os.getenv("KIS_APP_SECRET")
        self.account_no = os.getenv("KIS_ACCOUNT_NO")
        
        self.access_token      = None
        self.token_expire_time = None
        self.token_issue_time  = None
        
    def _get_access_token(self) -> str:
        """접근 토큰 발급/갱신"""
        current_time = datetime.now().timestamp()
        
        # 토큰이 있고 만료되지 않았으면 재사용
        if self.access_token and self.token_expire_time:
            # 만료 1시간 전까지는 기존 토큰 재사용
            if current_time < self.token_expire_time - 3600:
                return self.access_token
            
            # 만료 1시간 전이면 토큰 갱신
            logger.log_system("Token will expire soon, refreshing...")
        
        # 토큰 발급/갱신
        url = f"{self.base_url}/oauth2/tokenP"
        headers = {"content-type": "application/json"}
        body = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "appsecret": self.app_secret
        }
        
        try:
            response = requests.post(url, headers=headers, json=body)
            response.raise_for_status()
            token_data = response.json()
            
            self.access_token = token_data["access_token"]
            self.token_issue_time = current_time
            # 토큰 만료 시간 설정 (24시간)
            self.token_expire_time = current_time + (24 * 60 * 60)
            
            logger.log_system("Access token refreshed successfully")
            return self.access_token
            
        except Exception as e:
            logger.log_error(e, "Failed to get access token")
            raise
    
    def _get_hashkey(self, data: Dict[str, Any]) -> str:
        """해시키 생성"""
        url = f"{self.base_url}/uapi/hashkey"
        headers = {
            "content-type": "application/json",
            "appkey": self.app_key,
            "appsecret": self.app_secret
        }
        
        try:
            response = requests.post(url, headers=headers, json=data)
            response.raise_for_status()
            hashkey = response.json()["HASH"]
            return hashkey
            
        except Exception as e:
            logger.log_error(e, "Failed to get hashkey")
            raise
    
    def _make_request(self, method: str, path: str, headers: Dict = None, 
                     params: Dict = None, data: Dict = None, max_retries: int = 3) -> Dict[str, Any]:
        """API 요청 실행"""
        url = f"{self.base_url}{path}"
        
        default_headers = {
            "authorization": f"Bearer {self._get_access_token()}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_cont": "",
        }
        
        if headers:
            default_headers.update(headers)
        
        for attempt in range(max_retries):
            try:
                if method.upper() == "GET":
                    response = requests.get(url, headers=default_headers, params=params)
                else:
                    response = requests.post(url, headers=default_headers, json=data)
                
                response.raise_for_status()
                result = response.json()
                
                # API 응답 코드 체크
                if result.get("rt_cd") != "0":
                    error_msg = result.get("msg1", "Unknown error")
                    logger.log_error(f"API error: {error_msg}")
                    
                    # 토큰 만료 에러인 경우 토큰 갱신 후 재시도
                    if "token" in error_msg.lower() and attempt < max_retries - 1:
                        self.access_token = None  # 토큰 강제 갱신
                        continue
                    
                    raise Exception(f"API error: {error_msg}")
                
                return result
                
            except requests.exceptions.RequestException as e:
                if attempt < max_retries - 1:
                    wait_time = (attempt + 1) * 2  # 지수 백오프
                    logger.log_error(f"Request failed, retrying in {wait_time} seconds...")
                    time.sleep(wait_time)
                    continue
                raise
            
        raise Exception("Max retries exceeded")
    
    def get_current_price(self, symbol: str) -> Dict[str, Any]:
        """현재가 조회"""
        path = "/uapi/domestic-stock/v1/quotations/inquire-price"
        headers = {
            "tr_id": "FHKST01010100"
        }
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",  # 주식
            "FID_INPUT_ISCD": symbol
        }
        
        return self._make_request("GET", path, headers=headers, params=params)
    
    def get_orderbook(self, symbol: str) -> Dict[str, Any]:
        """호가 조회"""
        path = "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn"
        headers = {
            "tr_id": "FHKST01010200"
        }
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": symbol
        }
        
        return self._make_request("GET", path, headers=headers, params=params)
    
    def get_account_balance(self) -> Dict[str, Any]:
        """계좌 잔고 조회"""
        path = "/uapi/domestic-stock/v1/trading/inquire-balance"
        headers = {
            "tr_id": "TTTC8434R"  # 실전투자
        }
        params = {
            "CANO": self.account_no[:8],
            "ACNT_PRDT_CD": self.account_no[8:],
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "N",
            "INQR_DVSN": "02",
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "01",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": ""
        }
        
        return self._make_request("GET", path, headers=headers, params=params)
    
    def place_order(self, symbol: str, order_type: str, side: str, 
                   quantity: int, price: int = 0) -> Dict[str, Any]:
        """주문 실행"""
        path = "/uapi/domestic-stock/v1/trading/order-cash"
        
        # 매수/매도 구분
        if side.upper() == "BUY":
            tr_id = "TTTC0802U"  # 매수
        else:
            tr_id = "TTTC0801U"  # 매도
        
        headers = {
            "tr_id": tr_id
        }
        
        # 주문 유형 (00: 지정가, 01: 시장가)
        ord_dvsn = "01" if order_type.upper() == "MARKET" else "00"
        
        data = {
            "CANO": self.account_no[:8],
            "ACNT_PRDT_CD": self.account_no[8:],
            "PDNO": symbol,
            "ORD_DVSN": ord_dvsn,
            "ORD_QTY": str(quantity),
            "ORD_UNPR": str(price) if ord_dvsn == "00" else "0"
        }
        
        # 해시키 생성
        hashkey = self._get_hashkey(data)
        headers["hashkey"] = hashkey
        
        result = self._make_request("POST", path, headers=headers, data=data)
        
        # 주문 결과 로깅
        if result.get("rt_cd") == "0":
            logger.log_trade(
                action=side,
                symbol=symbol,
                price=price,
                quantity=quantity,
                order_id=result.get("output", {}).get("ODNO"),
                order_type=order_type
            )
        else:
            logger.log_error(
                Exception(f"Order failed: {result.get('msg1')}"),
                f"Place order for {symbol}"
            )
        
        return result
    
    def cancel_order(self, order_id: str, symbol: str, quantity: int) -> Dict[str, Any]:
        """주문 취소"""
        path = "/uapi/domestic-stock/v1/trading/order-rvsecncl"
        headers = {
            "tr_id": "TTTC0803U"  # 취소
        }
        
        data = {
            "CANO": self.account_no[:8],
            "ACNT_PRDT_CD": self.account_no[8:],
            "KRX_FWDG_ORD_ORGNO": "",  # 주문 시 받은 한국거래소전송주문조직번호
            "ORGN_ODNO": order_id,  # 원주문번호
            "ORD_DVSN": "00",  # 주문구분
            "RVSE_CNCL_DVSN_CD": "02",  # 취소
            "ORD_QTY": "0",  # 전량 취소
            "ORD_UNPR": "0",
            "QTY_ALL_ORD_YN": "Y"  # 전량주문여부
        }
        
        hashkey = self._get_hashkey(data)
        headers["hashkey"] = hashkey
        
        return self._make_request("POST", path, headers=headers, data=data)
    
    def get_order_history(self, start_date: str = None, end_date: str = None) -> Dict[str, Any]:
        """주문 내역 조회"""
        if not start_date:
            start_date = datetime.now().strftime("%Y%m%d")
        if not end_date:
            end_date = datetime.now().strftime("%Y%m%d")
            
        path = "/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
        headers = {
            "tr_id": "TTTC8001R"  # 일별 주문체결 조회
        }
        
        params = {
            "CANO": self.account_no[:8],
            "ACNT_PRDT_CD": self.account_no[8:],
            "INQR_STRT_DT": start_date,
            "INQR_END_DT": end_date,
            "SLL_BUY_DVSN_CD": "00",  # 전체
            "INQR_DVSN": "00",  # 역순
            "PDNO": "",  # 전종목
            "CCLD_DVSN": "00",  # 전체
            "ORD_GNO_BRNO": "",
            "ODNO": "",
            "INQR_DVSN_3": "00",
            "INQR_DVSN_1": "",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": ""
        }
        
        return self._make_request("GET", path, headers=headers, params=params)
    
    def get_stock_info(self, symbol: str) -> Dict[str, Any]:
        """종목 기본 정보 조회"""
        path = "/uapi/domestic-stock/v1/quotations/search-stock-info"
        headers = {
            "tr_id": "CTPF1002R"
        }
        params = {
            "PRDT_TYPE_CD": "300",  # 주식/ETF/ETN
            "PDNO": symbol
        }
        
        return self._make_request("GET", path, headers=headers, params=params)
    
    def get_daily_price(self, symbol: str, start_date: str = None, 
                       end_date: str = None) -> Dict[str, Any]:
        """일별 시세 조회"""
        if not end_date:
            end_date = datetime.now().strftime("%Y%m%d")
        if not start_date:
            start_date = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")
        
        path = "/uapi/domestic-stock/v1/quotations/inquire-daily-price"
        headers = {
            "tr_id": "FHKST01010400"
        }
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": symbol,
            "FID_PERIOD_DIV_CODE": "D",  # 일별
            "FID_ORG_ADJ_PRC": "1",  # 수정주가
            "FID_INPUT_DATE_1": start_date,
            "FID_INPUT_DATE_2": end_date
        }
        
        return self._make_request("GET", path, headers=headers, params=params)
    
    def get_minute_price(self, symbol: str, time_unit: str = "1") -> Dict[str, Any]:
        """분봉 조회"""
        path = "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"
        headers = {
            "tr_id": "FHKST03010200"
        }
        params = {
            "FID_ETC_CLS_CODE": "",
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": symbol,
            "FID_INPUT_HOUR_1": datetime.now().strftime("%H%M%S"),
            "FID_PW_DATA_INCU_YN": "Y",
            "FID_HOUR_CLS_CODE": time_unit  # 1: 1분봉, 5: 5분봉 등
        }
        
        return self._make_request("GET", path, headers=headers, params=params)
    
    def get_investor_trend(self, symbol: str) -> Dict[str, Any]:
        """투자자별 매매동향"""
        path = "/uapi/domestic-stock/v1/quotations/inquire-investor"
        headers = {
            "tr_id": "FHKST01010900"
        }
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": symbol
        }
        
        return self._make_request("GET", path, headers=headers, params=params)
    
    def get_market_index(self, index_code: str = "0001") -> Dict[str, Any]:
        """시장 지수 조회 (0001: KOSPI, 1001: KOSDAQ)"""
        path = "/uapi/domestic-stock/v1/quotations/inquire-index-price"
        headers = {
            "tr_id": "FHPUP02100000"
        }
        params = {
            "FID_COND_MRKT_DIV_CODE": "U",
            "FID_INPUT_ISCD": index_code
        }
        
        return self._make_request("GET", path, headers=headers, params=params)
    
    def get_market_trading_volume(self, market: str = "ALL") -> Dict[str, Any]:
        """시장 전체 거래량 조회"""
        path = "/uapi/domestic-stock/v1/quotations/volume-rank"
        headers = {
            "tr_id": "FHPST01710000"
        }
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_COND_SCR_DIV_CODE": "20171",
            "FID_INPUT_ISCD": "0000" if market == "ALL" else "0001",  # 0000: 전체, 0001: 코스피
            "FID_DIV_CLS_CODE": "0",
            "FID_BLNG_CLS_CODE": "0",
            "FID_TRGT_CLS_CODE": "111111111",
            "FID_TRGT_EXLS_CLS_CODE": "000000",
            "FID_INPUT_PRICE_1": "",
            "FID_INPUT_PRICE_2": "",
            "FID_VOL_CNT": "",
            "FID_INPUT_DATE_1": ""
        }
        
        return self._make_request("GET", path, headers=headers, params=params)

# 싱글톤 인스턴스
api_client = KISAPIClient()
