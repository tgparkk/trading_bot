"""
한국투자증권 API 클라이언트
"""
import requests
import json
import hashlib
import time
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List
from config.settings import config, APIConfig
from utils.logger import logger
import os
from dotenv import load_dotenv
from pathlib import Path
from utils.database import db
import asyncio

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
        
        self.access_token = None
        self.token_expire_time = None  # 만료 시간 (Unix timestamp)
        self.token_issue_time = None   # 발급 시간 (Unix timestamp)
        self._token_lock = asyncio.Lock()  # 토큰 발급 및 검증을 위한 락
        
        self.config = config.get("api", APIConfig.from_env())
        self.base_url = self.config.base_url
        self.app_key = self.config.app_key
        self.app_secret = self.config.app_secret
        self.account_no = self.config.account_no
        
        # 앱 시작 시 DB에서 유효한 토큰 로드
        self.load_token_from_db()
        
    def _get_access_token(self) -> str:
        """접근 토큰 발급/갱신"""
        current_time = datetime.now().timestamp()
        
        # 토큰이 있고 만료되지 않았으면 재사용
        if self.access_token and self.token_expire_time:
            # 만료 1시간 전까지는 기존 토큰 재사용
            if current_time < self.token_expire_time - 3600:
                # 토큰 로그를 무분별하게 남기지 않도록 삭제
                logger.log_system("기존 토큰이 유효하여 재사용합니다.")
                return self.access_token
            
            # 만료 1시간 전이면 토큰 갱신
            logger.log_system("Token will expire soon, refreshing...")
        
        # 토큰 발급/갱신 작업 로그
        logger.log_system("새로운 KIS API 토큰 발급을 시작합니다...")
        
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
            
            # 토큰 발급 로그 저장
            db.save_token_log(
                event_type="ISSUE",
                token=self.access_token,
                issue_time=datetime.fromtimestamp(current_time),
                expire_time=datetime.fromtimestamp(self.token_expire_time),
                status="SUCCESS"
            )
            
            logger.log_system("Access token refreshed successfully")
            return self.access_token
            
        except Exception as e:
            # 토큰 발급 실패 로그 저장
            db.save_token_log(
                event_type="FAIL",
                status="FAIL",
                error_message=str(e)
            )
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
        
        # 토큰이 이미 있고 유효하다면 매번 토큰 발급을 시도하지 않도록 함
        current_time = datetime.now().timestamp()
        token_valid = self.access_token and self.token_expire_time and current_time < self.token_expire_time - 3600
        
        default_headers = {
            "authorization": f"Bearer {self.access_token if token_valid else self._get_access_token()}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_cont": "",
        }
        
        if headers:
            default_headers.update(headers)
        
        token_refresh_attempts = 0
        max_token_refresh_attempts = 2  # 토큰 갱신 최대 시도 횟수
        
        for attempt in range(max_retries):
            try:
                if method.upper() == "GET":
                    response = requests.get(url, headers=default_headers, params=params)
                else:
                    response = requests.post(url, headers=default_headers, json=data)
                
                # 500 에러 발생 시 토큰 강제 갱신
                if response.status_code == 500:
                    if token_refresh_attempts >= max_token_refresh_attempts:
                        logger.log_error("최대 토큰 갱신 시도 횟수 초과")
                        raise Exception("Maximum token refresh attempts exceeded")
                    
                    logger.log_system(f"500 에러 발생, 토큰 강제 갱신 시도... (시도 {token_refresh_attempts + 1}/{max_token_refresh_attempts})")
                    self.access_token = None  # 토큰 초기화
                    self.token_expire_time = None
                    
                    # 토큰 강제 갱신
                    try:
                        new_token = self._get_access_token()
                        logger.log_system("토큰 강제 갱신 성공")
                        # 헤더 업데이트
                        default_headers["authorization"] = f"Bearer {new_token}"
                        token_refresh_attempts += 1
                        time.sleep(1)  # 토큰 갱신 후 잠시 대기
                        continue  # 새 토큰으로 재시도
                    except Exception as token_error:
                        logger.log_error(token_error, "토큰 강제 갱신 실패")
                        if token_refresh_attempts >= max_token_refresh_attempts - 1:
                            raise Exception("Token refresh failed after multiple attempts")
                        token_refresh_attempts += 1
                        continue
                
                response.raise_for_status()
                result = response.json()
                
                # API 응답 코드 체크
                if result.get("rt_cd") != "0":
                    error_msg = result.get("msg1", "Unknown error")
                    logger.log_error(f"API error: {error_msg}")
                    
                    # 토큰 관련 에러인 경우 토큰 갱신
                    if any(keyword in error_msg.lower() for keyword in ["token", "auth", "unauthorized"]):
                        if token_refresh_attempts < max_token_refresh_attempts:
                            logger.log_system("토큰 관련 에러 발생, 토큰 갱신 시도...")
                            self.access_token = None  # 토큰 초기화
                            token_refresh_attempts += 1
                            continue
                    
                    raise Exception(f"API error: {error_msg}")
                
                return result
                
            except requests.exceptions.RequestException as e:
                if attempt < max_retries - 1:
                    wait_time = (attempt + 1) * 2  # 지수 백오프
                    logger.log_error(f"Request failed, retrying in {wait_time} seconds...")
                    time.sleep(wait_time)
                else:
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
        
        # 테스트 모드 확인 (TEST_MODE=True이면 모의투자, 아니면 실전투자)
        test_mode_str = os.getenv("TEST_MODE", "False").strip()
        is_test_mode = test_mode_str.lower() in ['true', '1', 't', 'y', 'yes']
        
        logger.log_system(f"계좌 조회 - 테스트 모드: {is_test_mode} (환경 변수 TEST_MODE: '{test_mode_str}')")
        
        # 거래소코드 설정 (모의투자 또는 실전투자)
        tr_id = "VTTC8434R" if is_test_mode else "TTTC8434R"  # 모의투자(V) vs 실전투자(T)
        
        headers = {
            "tr_id": tr_id
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
        
        try:
            # API 요청 전 유효한 토큰 확보
            if not self.access_token or not self.token_expire_time:
                logger.log_system("계좌 정보 조회 전 토큰 발급이 필요합니다.")
                self._get_access_token()
                
            # API 요청 실행
            result = self._make_request("GET", path, headers=headers, params=params)
            
            # 응답 로깅 (디버깅용)
            if result and result.get("rt_cd") == "0":
                logger.log_system(f"계좌 정보 조회 성공: {result.get('msg1', '정상')}")
            else:
                error_msg = result.get("msg1", "알 수 없는 오류") if result else "응답 없음"
                logger.log_system(f"계좌 정보 조회 실패: {error_msg}", level="ERROR")
                
            return result
        except Exception as e:
            logger.log_error(e, "계좌 정보 조회 중 예외 발생")
            return {"rt_cd": "9999", "msg1": str(e), "output": {}}
    
    def get_account_info(self) -> Dict[str, Any]:
        """
        계좌 정보 조회
        텔레그램 봇 핸들러와의 호환성을 위한 메서드
        """
        # 기존 get_account_balance 함수 호출
        return self.get_account_balance()
    
    async def get_account_info(self) -> Dict[str, Any]:
        """
        계좌 정보 조회 (비동기 버전)
        텔레그램 봇 핸들러와의 호환성을 위한 메서드
        """
        try:
            # 동기 함수를 비동기적으로 실행
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, self.get_account_balance)
            return result
        except Exception as e:
            logger.log_error(e, "비동기 계좌 정보 조회 중 오류 발생")
            # 오류 발생 시 에러 정보 반환
            return {
                "rt_cd": "9999", 
                "msg1": f"계좌 정보 조회 실패: {str(e)}", 
                "output": {}
            }
    
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
            order_id = result.get("output", {}).get("ODNO")
            logger.log_system(f"[💰 주문성공] {symbol} {side} 주문 성공! - 가격: {price:,}원, 수량: {quantity}주, 주문ID: {order_id}")
            logger.log_trade(
                action=f"{side}_API", 
                symbol=symbol,
                price=price,
                quantity=quantity,
                order_id=order_id,
                order_type=order_type,
                status="SUCCESS",
                reason="API 주문 전송 성공"
            )
        else:
            error_msg = result.get("msg1", "Unknown error")
            logger.log_system(f"[🚸 주문실패] {symbol} {side} 주문 실패 - 오류: {error_msg}")
            logger.log_error(
                Exception(f"Order failed: {error_msg}"),
                f"Place order for {symbol}"
            )
            logger.log_trade(
                action=f"{side}_API_FAILED",
                symbol=symbol,
                price=price,
                quantity=quantity,
                reason=f"API 주문 실패: {error_msg}",
                status="FAILED"
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
        """시장 전체 거래량 조회
        
        Args:
            market (str): 시장 구분 (ALL: 전체, KOSPI: 코스피, KOSDAQ: 코스닥)
        
        Returns:
            Dict[str, Any]: API 응답 데이터
        """
        path = "/uapi/domestic-stock/v1/quotations/volume-rank"
        headers = {
            "tr_id": "FHPST01710000"
        }
        
        # 시장 코드 변환
        market_code = {
            "ALL": "0000",     # 전체
            "KOSPI": "0001",   # 코스피
            "KOSDAQ": "1001"   # 코스닥
        }.get(market, "0000")  # 기본값은 전체
        
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_COND_SCR_DIV_CODE": "20171",
            "FID_INPUT_ISCD": market_code,
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

    def check_token_status(self) -> Dict[str, Any]:
        """토큰 상태 확인"""
        current_time = datetime.now().timestamp()
        
        if not self.access_token or not self.token_expire_time:
            return {
                "status": "not_initialized",
                "message": "토큰이 초기화되지 않았습니다."
            }
        
        time_remaining = self.token_expire_time - current_time
        
        if time_remaining <= 0:
            return {
                "status": "expired",
                "message": "토큰이 만료되었습니다."
            }
        
        hours_remaining = time_remaining / 3600
        
        if hours_remaining <= 1:
            return {
                "status": "expires_soon",
                "message": f"토큰이 곧 만료됩니다. ({hours_remaining:.1f}시간 남음)"
            }
        
        return {
            "status": "valid",
            "message": f"토큰이 유효합니다. ({hours_remaining:.1f}시간 남음)",
            "expires_in_hours": hours_remaining
        }

    def force_token_refresh(self) -> Dict[str, Any]:
        """토큰 강제 갱신"""
        try:
            self.access_token = None  # 토큰 초기화
            self.token_expire_time = None
            new_token = self._get_access_token()
            
            return {
                "status": "success",
                "message": "토큰이 성공적으로 갱신되었습니다.",
                "token_status": self.check_token_status()
            }
        except Exception as e:
            return {
                "status": "error",
                "message": f"토큰 갱신 실패: {str(e)}"
            }

    async def is_token_valid(self, min_hours: float = 0.5) -> bool:
        """토큰이 유효한지 확인
        
        Args:
            min_hours (float): 최소 유효 시간 (시간 단위, 기본값 30분)
            
        Returns:
            bool: 토큰 유효 여부 (True: 유효, False: 만료 또는 없음)
        """
        async with self._token_lock:
            # 토큰이 없으면 유효하지 않음
            if not self.access_token or not self.token_expire_time:
                return False
                
            # 토큰 만료 시간을 확인
            try:
                current_time = datetime.now().timestamp()
                time_remaining = self.token_expire_time - current_time
                
                # 최소 유효 시간 이상 남았는지 확인
                if time_remaining > (min_hours * 3600):
                    hours_remaining = time_remaining / 3600
                    logger.log_debug(f"토큰이 유효함. 만료까지 {hours_remaining:.1f}시간 남음")
                    return True
                
                # 만료 시간이 min_hours 이내로 남았거나 이미 만료됨
                if time_remaining <= 0:
                    logger.log_debug("토큰이 만료됨")
                else:
                    minutes_remaining = time_remaining / 60
                    logger.log_debug(f"토큰 만료가 임박함. {minutes_remaining:.1f}분 남음")
                return False
                
            except Exception as e:
                logger.log_error(e, "토큰 유효성 확인 중 오류 발생")
                return False
    
    async def ensure_token(self) -> str:
        """토큰이 있고 유효한지 확인하고, 없거나 유효하지 않으면 새로 발급"""
        async with self._token_lock:
            # 토큰 유효성 먼저 확인
            if await self.is_token_valid():
                logger.log_system("토큰이 유효함. 새로 발급하지 않고 기존 토큰 사용")
                return self.access_token
            
            # 토큰이 유효하지 않으면 새로 발급
            logger.log_system("토큰이 없거나 만료됨. 새로 발급 진행")
            await self.issue_token()
            return self.access_token
    
    async def issue_token(self) -> str:
        """비동기적으로 토큰 발급 (Python 3.7+ 호환)"""
        async with self._token_lock:
            try:
                # 동기 함수 _get_access_token을 실행하여 토큰 발급 (run_in_executor 사용)
                loop = asyncio.get_event_loop()
                token = await loop.run_in_executor(None, self._get_access_token)
                logger.log_system("토큰 발급 성공")
                return token
            except Exception as e:
                logger.log_error(e, "토큰 발급 실패")
                raise

    def load_token_from_db(self):
        # 앱 시작 시 DB에서 유효한 토큰 로드
        try:
            logger.log_system("앱 시작 시 저장된 토큰 확인 중...")
            latest_token = db.get_latest_token()
            
            if latest_token and latest_token.get('token') and latest_token.get('expire_time'):
                expire_time = datetime.fromisoformat(latest_token['expire_time']).timestamp()
                current_time = datetime.now().timestamp()
                
                # 토큰이 아직 유효한지 확인 (만료 1시간 이상 남은 경우)
                if current_time < expire_time - 3600:
                    self.access_token = latest_token['token']
                    self.token_expire_time = expire_time
                    self.token_issue_time = datetime.fromisoformat(latest_token['issue_time']).timestamp() if latest_token.get('issue_time') else None
                    
                    logger.log_system(f"저장된 유효한 토큰을 로드했습니다. 만료까지 {((expire_time - current_time) / 3600):.1f}시간 남음")
                else:
                    logger.log_system("저장된 토큰이 곧 만료되거나 이미 만료되었습니다. 새 토큰을 발급합니다.")
            else:
                logger.log_system("저장된 유효한 토큰이 없습니다. 필요시 새 토큰을 발급합니다.")
        except Exception as e:
            logger.log_error(e, "저장된 토큰 로드 중 오류 발생. 필요시 새 토큰을 발급합니다.")

# 싱글톤 인스턴스
api_client = KISAPIClient()
