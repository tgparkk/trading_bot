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

# 토큰 정보 저장 파일 경로
TOKEN_FILE_PATH = os.path.join(os.path.abspath(os.getcwd()), "token_info.json")

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
        
        # 앱 시작 시 파일에서 유효한 토큰 로드
        self.load_token_from_file()
        
    def _get_access_token(self) -> str:
        """접근 토큰 발급/갱신"""
        current_time = datetime.now().timestamp()
        
        # 토큰이 있고 만료되지 않았으면 재사용
        if self.access_token and self.token_expire_time:
            # 만료 1시간 전까지는 기존 토큰 재사용
            if current_time < self.token_expire_time - 3600:
                logger.log_system(f"[토큰재사용] 기존 토큰이 유효하여 재사용합니다. (만료까지 {(self.token_expire_time - current_time)/3600:.1f}시간 남음)")
                return self.access_token
            
            # 만료 1시간 전이면 토큰 갱신
            logger.log_system("[토큰갱신] 토큰이 곧 만료되어 갱신합니다.")
        
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
            
            # 토큰 정보를 파일에 저장
            self.save_token_to_file(
                token=self.access_token,
                issue_time=current_time,
                expire_time=self.token_expire_time
            )
            
            logger.log_system("Access token refreshed successfully")
            return self.access_token
            
        except Exception as e:
            # 토큰 발급 실패 로그
            logger.log_error(e, "Failed to get access token")
            # 토큰 발급 실패 정보를 파일에 저장
            self.save_token_to_file(
                token=None,
                issue_time=current_time,
                expire_time=None,
                status="FAIL",
                error_message=str(e)
            )
            raise

    def save_token_to_file(self, token: str = None, issue_time: float = None, 
                         expire_time: float = None, status: str = "SUCCESS", 
                         error_message: str = None):
        """토큰 정보를 파일에 저장"""
        try:
            # 파일이 존재하면 기존 내용 로드
            token_info = {}
            if os.path.exists(TOKEN_FILE_PATH):
                try:
                    with open(TOKEN_FILE_PATH, 'r') as f:
                        token_info = json.load(f)
                        # 기존 정보 보존을 위해 'history' 키가 없으면 생성
                        if 'history' not in token_info:
                            token_info['history'] = []
                except (json.JSONDecodeError, FileNotFoundError):
                    # 파일이 손상되었거나 없으면 새로 생성
                    token_info = {'current': {}, 'history': []}
            else:
                token_info = {'current': {}, 'history': []}
            
            # 현재 시간
            current_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            # 현재 토큰 정보 업데이트
            if status == "SUCCESS" and token:
                token_info['current'] = {
                    'token': token,
                    'issue_time': issue_time,
                    'issue_time_str': datetime.fromtimestamp(issue_time).strftime("%Y-%m-%d %H:%M:%S") if issue_time else None,
                    'expire_time': expire_time,
                    'expire_time_str': datetime.fromtimestamp(expire_time).strftime("%Y-%m-%d %H:%M:%S") if expire_time else None,
                    'status': status,
                    'updated_at': current_time_str
                }
            
            # 히스토리에 추가
            history_entry = {
                'token': token[:10] + '...' if token else None,  # 보안상 전체 토큰은 저장하지 않음
                'issue_time_str': datetime.fromtimestamp(issue_time).strftime("%Y-%m-%d %H:%M:%S") if issue_time else None,
                'expire_time_str': datetime.fromtimestamp(expire_time).strftime("%Y-%m-%d %H:%M:%S") if expire_time else None,
                'status': status,
                'error_message': error_message,
                'recorded_at': current_time_str
            }
            token_info['history'].append(history_entry)
            
            # 히스토리 최대 50개로 제한
            if len(token_info['history']) > 50:
                token_info['history'] = token_info['history'][-50:]
            
            # 파일에 저장
            with open(TOKEN_FILE_PATH, 'w') as f:
                json.dump(token_info, f, indent=2)
            
            logger.log_system(f"토큰 정보를 파일에 저장했습니다: {TOKEN_FILE_PATH}")
            
        except Exception as e:
            logger.log_error(e, "토큰 정보를 파일에 저장하는 중 오류 발생")

    def load_token_from_file(self):
        """파일에서 토큰 정보 로드"""
        try:
            if not os.path.exists(TOKEN_FILE_PATH):
                logger.log_system(f"토큰 파일이 존재하지 않습니다: {TOKEN_FILE_PATH}")
                return False
            
            with open(TOKEN_FILE_PATH, 'r') as f:
                token_info = json.load(f)
            
            if 'current' not in token_info or not token_info['current']:
                logger.log_system("토큰 파일에 유효한 토큰 정보가 없습니다.")
                return False
            
            current_token = token_info['current']
            
            if 'token' not in current_token or 'expire_time' not in current_token:
                logger.log_system("토큰 파일에 필수 정보(토큰 또는 만료 시간)가 없습니다.")
                return False
            
            self.access_token = current_token['token']
            self.token_expire_time = current_token['expire_time']
            self.token_issue_time = current_token.get('issue_time')
            
            # 현재 시간과 만료 시간 비교하여 유효성 검사
            current_time = datetime.now().timestamp()
            
            if current_time < self.token_expire_time:
                hours_remaining = (self.token_expire_time - current_time) / 3600
                logger.log_system(f"파일에서 유효한 토큰을 로드했습니다. 만료까지 {hours_remaining:.1f}시간 남음")
                return True
            else:
                expire_time_str = datetime.fromtimestamp(self.token_expire_time).strftime("%Y-%m-%d %H:%M:%S")
                logger.log_system(f"파일에서 로드한 토큰이 만료되었습니다. 만료 시간: {expire_time_str}")
                self.access_token = None
                self.token_expire_time = None
                self.token_issue_time = None
                return False
                
        except Exception as e:
            logger.log_error(e, "파일에서 토큰 정보를 로드하는 중 오류 발생")
            return False
    
    async def _get_access_token_async(self):
        """비동기 방식으로 액세스 토큰 획득"""
        async with self._token_lock:
            # 파일에서 토큰 정보 다시 확인
            self.load_token_from_file()
            
            current_time = datetime.now().timestamp()
            
            # 토큰이 있고 유효한지 확인
            if self.access_token and self.token_expire_time:
                # 현재 시간이 만료 시간보다 3시간 이상 남았으면 기존 토큰 사용 (1시간에서 3시간으로 변경)
                remaining_hours = (self.token_expire_time - current_time) / 3600
                if current_time < self.token_expire_time - 10800:  # 3시간(10800초)으로 수정
                    logger.log_system(f"[토큰재사용] 유효한 토큰이 있습니다. 현재={datetime.fromtimestamp(current_time).strftime('%Y-%m-%d %H:%M:%S')}, "
                                     f"만료={datetime.fromtimestamp(self.token_expire_time).strftime('%Y-%m-%d %H:%M:%S')}, "
                                     f"남은시간={remaining_hours:.1f}시간")
                    return self.access_token
                
                # 만료 3시간 이내인 경우에만 갱신 시도
                logger.log_system(f"[토큰갱신] 토큰 만료 3시간 이내, 갱신 필요: 현재={datetime.fromtimestamp(current_time).strftime('%Y-%m-%d %H:%M:%S')}, "
                                 f"만료={datetime.fromtimestamp(self.token_expire_time).strftime('%Y-%m-%d %H:%M:%S')}, "
                                 f"남은시간={remaining_hours:.1f}시간")
            else:
                logger.log_system("[토큰없음] 유효한 토큰이 없어 새로 발급합니다.")
            
            # 새 토큰 발급 필요
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, self._get_access_token)

    async def is_token_valid(self, min_hours: float = 0.5) -> bool:
        """토큰이 유효한지 확인
        
        Args:
            min_hours (float): 최소 유효 시간 (시간 단위, 기본값 30분)
            
        Returns:
            bool: 토큰 유효 여부 (True: 유효, False: 만료 또는 없음)
        """
        async with self._token_lock:
            # 먼저 파일에서 최신 토큰 정보 로드
            self.load_token_from_file()
            
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

    def check_token_status(self) -> Dict[str, Any]:
        """토큰 상태 확인"""
        # 먼저 파일에서 최신 토큰 정보 로드
        self.load_token_from_file()
        
        current_time = datetime.now().timestamp()
        
        if not self.access_token or not self.token_expire_time:
            return {
                "status": "not_initialized",
                "message": "토큰이 초기화되지 않았습니다.",
                "file_path": TOKEN_FILE_PATH
            }
        
        time_remaining = self.token_expire_time - current_time
        
        if time_remaining <= 0:
            return {
                "status": "expired",
                "message": "토큰이 만료되었습니다.",
                "expired_at": datetime.fromtimestamp(self.token_expire_time).strftime("%Y-%m-%d %H:%M:%S")
            }
        
        hours_remaining = time_remaining / 3600
        
        if hours_remaining <= 1:
            return {
                "status": "expires_soon",
                "message": f"토큰이 곧 만료됩니다. ({hours_remaining:.1f}시간 남음)",
                "expires_in_hours": hours_remaining
            }
        
        return {
            "status": "valid",
            "message": f"토큰이 유효합니다. ({hours_remaining:.1f}시간 남음)",
            "expires_in_hours": hours_remaining,
            "expire_time": datetime.fromtimestamp(self.token_expire_time).strftime("%Y-%m-%d %H:%M:%S"),
            "issue_time": datetime.fromtimestamp(self.token_issue_time).strftime("%Y-%m-%d %H:%M:%S") if self.token_issue_time else None
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
                "token_status": self.check_token_status(),
                "file_path": TOKEN_FILE_PATH
            }
        except Exception as e:
            return {
                "status": "error",
                "message": f"토큰 갱신 실패: {str(e)}"
            }
    
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
                
    def get_token_file_info(self) -> Dict[str, Any]:
        """토큰 파일 정보 반환"""
        try:
            if not os.path.exists(TOKEN_FILE_PATH):
                return {
                    "exists": False,
                    "message": f"토큰 파일이 존재하지 않습니다: {TOKEN_FILE_PATH}"
                }
            
            # 파일 정보 가져오기
            file_stats = os.stat(TOKEN_FILE_PATH)
            file_size = file_stats.st_size
            modified_time = datetime.fromtimestamp(file_stats.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            
            # 파일 내용 확인
            with open(TOKEN_FILE_PATH, 'r') as f:
                try:
                    token_info = json.load(f)
                    
                    # 현재 토큰 정보
                    current_token = token_info.get('current', {})
                    has_valid_token = bool(current_token.get('token') and current_token.get('expire_time'))
                    
                    # 히스토리 정보
                    history_count = len(token_info.get('history', []))
                    
                    return {
                        "exists": True,
                        "file_path": TOKEN_FILE_PATH,
                        "file_size_bytes": file_size,
                        "last_modified": modified_time,
                        "has_valid_token": has_valid_token,
                        "history_entries": history_count,
                        "token_expires_at": current_token.get('expire_time_str') if has_valid_token else None
                    }
                    
                except json.JSONDecodeError:
                    return {
                        "exists": True,
                        "file_path": TOKEN_FILE_PATH,
                        "file_size_bytes": file_size,
                        "last_modified": modified_time,
                        "error": "파일이 유효한 JSON 형식이 아닙니다."
                    }
        
        except Exception as e:
            return {
                "exists": os.path.exists(TOKEN_FILE_PATH),
                "file_path": TOKEN_FILE_PATH,
                "error": f"파일 정보 조회 중 오류 발생: {str(e)}"
            }

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
    
    async def get_symbol_info(self, symbol: str) -> Dict[str, Any]:
        """종목 정보 조회"""
        try:
            # 디버깅 로그 추가
            logger.log_system(f"[API] 종목 정보 조회 시작: {symbol}")
            
            # 타임아웃 설정 (3초)
            timeout = 3
            
            url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-price"
            headers = {
                "content-type": "application/json; charset=utf-8",
                "authorization": f"Bearer {await self._get_access_token_async()}",
                "appkey": self.app_key,
                "appsecret": self.app_secret,
                "tr_id": "FHKST01010100"
            }
            params = {
                "fid_cond_mrkt_div_code": "J",
                "fid_input_iscd": symbol
            }
            
            # 타임아웃 적용하여 API 요청
            response = requests.get(url, headers=headers, params=params, timeout=timeout)
            response.raise_for_status()
            data = response.json()
            
            if data["rt_cd"] == "0":
                result = {
                    "symbol": symbol,
                    "name": data["output"]["hts_kor_isnm"],
                    "current_price": float(data["output"]["stck_prpr"]),  # 현재가
                    "open_price": float(data["output"]["stck_oprc"]),     # 시가
                    "high_price": float(data["output"]["stck_hgpr"]),     # 고가
                    "low_price": float(data["output"]["stck_lwpr"]),      # 저가
                    "prev_close": float(data["output"]["stck_sdpr"]),     # 전일 종가
                    "volume": int(data["output"]["acml_vol"]),            # 누적 거래량
                    "change_rate": float(data["output"]["prdy_ctrt"]),    # 전일 대비 변동율
                    "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
                logger.log_system(f"[API] {symbol} 종목 정보 조회 성공: 현재가 {result['current_price']:,}원")
                return result
            else:
                error_msg = data.get("msg1", "알 수 없는 오류")
                logger.log_system(f"[API] {symbol} 종목 정보 조회 실패: {error_msg}")
                # API 호출 실패 시 테스트 데이터 생성
                return self._generate_test_price_data(symbol)
        
        except requests.Timeout:
            logger.log_system(f"[API] {symbol} 종목 정보 조회 타임아웃 발생 (3초)")
            # 타임아웃 발생 시 테스트 데이터 생성
            return self._generate_test_price_data(symbol)
        except Exception as e:
            logger.log_error(e, f"[API] {symbol} 종목 정보 조회 중 오류 발생")
            # 예외 발생 시 테스트 데이터 생성
            return self._generate_test_price_data(symbol)

    def _generate_test_price_data(self, symbol: str) -> Dict[str, Any]:
        """API 호출 실패 시 테스트용 가격 데이터 생성"""
        # 가격 랜덤 생성 (실제 종목 가격대를 고려하여 조정 가능)
        import random
        
        try:
            # 이전 거래 데이터가 파일에 있는지 확인
            price_data_path = os.path.join(os.path.abspath(os.getcwd()), "price_data")
            # 디렉토리가 없으면 생성
            if not os.path.exists(price_data_path):
                os.makedirs(price_data_path)
                
            symbol_file = os.path.join(price_data_path, f"{symbol}_price.json")
            
            if os.path.exists(symbol_file):
                try:
                    with open(symbol_file, 'r') as f:
                        price_data = json.load(f)
                        if 'last_price' in price_data:
                            base_price = float(price_data['last_price'])
                            # 이전 가격의 ±2% 범위 내에서 랜덤 가격 생성
                            current_price = int(base_price * random.uniform(0.98, 1.02))
                            logger.log_system(f"[API] {symbol} 테스트 데이터: 이전 거래 가격({base_price:,}원) 기반 생성 -> {current_price:,}원")
                            
                            # 나머지 가격 정보 계산
                            change_percent = random.uniform(-1.5, 1.5)
                            change_amount = int(current_price * change_percent / 100)
                            prev_close = current_price - change_amount
                            open_price = int(current_price * random.uniform(0.99, 1.01))
                            high_price = max(current_price, open_price) * random.uniform(1.0, 1.01)
                            low_price = min(current_price, open_price) * random.uniform(0.99, 1.0)
                            volume = random.randint(50000, 500000)
                            
                            # 새 가격 정보 저장
                            updated_price_data = {
                                "symbol": symbol,
                                "last_price": current_price,
                                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "history": price_data.get('history', [])[-9:] + [{
                                    "price": current_price,
                                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                }]
                            }
                            
                            with open(symbol_file, 'w') as f:
                                json.dump(updated_price_data, f, indent=2)
                            
                            return {
                                "symbol": symbol,
                                "name": f"테스트_{symbol}",
                                "current_price": float(current_price),
                                "open_price": float(open_price),
                                "high_price": float(high_price),
                                "low_price": float(low_price),
                                "prev_close": float(prev_close),
                                "volume": int(volume),
                                "change_rate": float(change_percent),
                                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "is_test_data": True
                            }
                except (json.JSONDecodeError, FileNotFoundError, KeyError):
                    logger.log_system(f"[API] {symbol} 이전 가격 정보 파일 읽기 실패, 새로 생성합니다.")
                    # 파일이 손상되었거나 필요한 데이터가 없으면 새로 생성
        except Exception as e:
            logger.log_error(e, f"{symbol} 가격 데이터 파일 처리 중 오류 발생")
        
        # 기본 로직: 종목코드에 따라 적절한 가격대 생성
        first_digit = int(symbol[0]) if symbol[0].isdigit() else 5
        price_range = {
            0: (1000, 10000),    # 1천~1만원대
            1: (5000, 30000),    # 5천~3만원대
            2: (10000, 50000),   # 1만~5만원대
            3: (20000, 100000),  # 2만~10만원대
            4: (50000, 200000),  # 5만~20만원대
            5: (100000, 500000), # 10만~50만원대
            6: (200000, 800000), # 20만~80만원대
            7: (300000, 1000000),# 30만~100만원대
            8: (500000, 1500000),# 50만~150만원대
            9: (800000, 2000000) # 80만~200만원대
        }
        
        base_range = price_range.get(first_digit, (10000, 100000))
        current_price = random.randint(base_range[0], base_range[1])
        
        # 변동폭은 현재가의 -3% ~ +3% 범위
        change_percent = random.uniform(-3.0, 3.0)
        change_amount = int(current_price * change_percent / 100)
        prev_close = current_price - change_amount
        
        # 당일 시/고/저가는 현재가 기준으로 적절히 설정
        open_price = int(current_price * random.uniform(0.97, 1.03))
        high_price = max(current_price, open_price) * random.uniform(1.0, 1.05)
        low_price = min(current_price, open_price) * random.uniform(0.95, 1.0)
        
        # 거래량은 종목에 따라 다양하게 설정
        volume = random.randint(10000, 1000000)
        
        logger.log_system(f"[API] {symbol} 테스트 데이터 생성: 현재가 {current_price:,}원 (변동률: {change_percent:.2f}%)")
        
        # 가격 정보 파일에 저장
        try:
            price_data_path = os.path.join(os.path.abspath(os.getcwd()), "price_data")
            if not os.path.exists(price_data_path):
                os.makedirs(price_data_path)
                
            symbol_file = os.path.join(price_data_path, f"{symbol}_price.json")
            price_data = {
                "symbol": symbol,
                "last_price": current_price,
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "history": [{
                    "price": current_price,
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }]
            }
            
            with open(symbol_file, 'w') as f:
                json.dump(price_data, f, indent=2)
        except Exception as e:
            logger.log_error(e, f"{symbol} 가격 정보 파일 저장 중 오류 발생")
        
        return {
            "symbol": symbol,
            "name": f"테스트_{symbol}",
            "current_price": float(current_price),
            "open_price": float(open_price),
            "high_price": float(high_price),
            "low_price": float(low_price),
            "prev_close": float(prev_close),
            "volume": int(volume),
            "change_rate": float(change_percent),
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "is_test_data": True  # 테스트 데이터임을 표시
        }

# 싱글톤 인스턴스
api_client = KISAPIClient()
