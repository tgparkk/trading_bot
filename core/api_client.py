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
from utils.database import database_manager
import asyncio
import threading

# 토큰 정보 저장 파일 경로
TOKEN_FILE_PATH = os.path.join(os.path.abspath(os.getcwd()), "token_info.json")

class KISAPIClient:
    """한국투자증권 REST API 클라이언트"""
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls, *args, **kwargs):
        """싱글톤 패턴 구현을 위한 __new__ 메서드 오버라이드"""
        with cls._lock:  # 스레드 안전성을 위한 락 사용
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        """생성자는 인스턴스가 처음 생성될 때만 실행됨을 보장"""
        if not hasattr(self, '_initialized') or not self._initialized:
            # .env 로드 (필요하다면)
            env_path = Path(__file__).parent.parent / ".env"
            load_dotenv(dotenv_path=env_path)
            
            # 초기화
            self.access_token = None
            self.token_expire_time = None  # 만료 시간 (Unix timestamp)
            self.token_issue_time = None   # 발급 시간 (Unix timestamp)
            self._token_lock = asyncio.Lock()  # 토큰 발급 및 검증을 위한 락
            
            # 환경 변수에서 읽어오기
            self.base_url = os.getenv("KIS_BASE_URL")
            self.app_key = os.getenv("KIS_APP_KEY")
            self.app_secret = os.getenv("KIS_APP_SECRET")
            self.account_no = os.getenv("KIS_ACCOUNT_NO")
            
            # 설정에서 값 가져오기
            self.config = config.get("api", APIConfig.from_env())
            self.base_url = self.config.base_url
            self.app_key = self.config.app_key
            self.app_secret = self.config.app_secret
            self.account_no = self.config.account_no
            
            # 앱 시작 시 파일에서 유효한 토큰 로드
            self.load_token_from_file()
            
            self._initialized = True
        
    def _get_access_token(self) -> str:
        """접근 토큰 발급/갱신"""
        current_time = datetime.now().timestamp()
        
        # 토큰이 있고 만료되지 않았으면 재사용
        if self.access_token and self.token_expire_time:
            # 만료 1시간 전까지는 기존 토큰 재사용 (더 여유 있게 설정)
            if current_time < self.token_expire_time - 7200:  # 2시간(7200초) 전까지 재사용
                logger.log_system(f"[토큰재사용] 기존 토큰이 유효하여 재사용합니다. (만료까지 {(self.token_expire_time - current_time)/3600:.1f}시간 남음)")
                return self.access_token
            
            # 만료 2시간 전이면 토큰 갱신
            logger.log_system(f"[토큰갱신] 토큰이 곧 만료되어 갱신합니다. (만료까지 {(self.token_expire_time - current_time)/3600:.1f}시간 남음)")
        else:
            # 토큰 발급/갱신 작업 로그
            logger.log_system("[토큰없음] 새로운 KIS API 토큰 발급을 시작합니다...")
        
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
            token_loaded = self.load_token_from_file()
            
            current_time = datetime.now().timestamp()
            
            # 토큰이 있고 유효한지 확인
            if self.access_token and self.token_expire_time:
                # 토큰 만료까지 남은 시간 계산 (시간 단위)
                remaining_hours = (self.token_expire_time - current_time) / 3600
                
                # 만료 시간이 1시간 이상 남았으면 기존 토큰 사용
                if current_time < self.token_expire_time - 3600:  # 1시간(3600초)으로 수정
                    logger.log_system(f"[토큰재사용] 유효한 토큰이 있습니다. 현재={datetime.fromtimestamp(current_time).strftime('%Y-%m-%d %H:%M:%S')}, "
                                     f"만료={datetime.fromtimestamp(self.token_expire_time).strftime('%Y-%m-%d %H:%M:%S')}, "
                                     f"남은시간={remaining_hours:.1f}시간")
                    return self.access_token
                
                # 만료 1시간 이내인 경우에만 갱신 시도
                logger.log_system(f"[토큰갱신] 토큰 만료 1시간 이내, 갱신 필요: 현재={datetime.fromtimestamp(current_time).strftime('%Y-%m-%d %H:%M:%S')}, "
                                 f"만료={datetime.fromtimestamp(self.token_expire_time).strftime('%Y-%m-%d %H:%M:%S')}, "
                                 f"남은시간={remaining_hours:.1f}시간")
            else:
                # 파일에서 로드 시도 결과에 따라 다른 메시지 표시
                if token_loaded:
                    logger.log_system("[토큰오류] 파일에서 토큰을 로드했지만 유효하지 않습니다.")
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
        
        # 상수 정의
        TOKEN_EXPIRY_BUFFER = 3600  # 1시간 (토큰 갱신 버퍼)
        MAX_TOKEN_REFRESH_ATTEMPTS = 2
        REQUEST_TIMEOUT = 30  # 요청 타임아웃 (초)
        
        # 토큰 유효성 확인 (KIS 토큰은 24시간 유효)
        current_time = datetime.now().timestamp()
        
        # 파일에서 최신 토큰 정보 로드
        self.load_token_from_file()
        
        # 토큰이 있고 아직 유효한지 확인 (만료 1시간 전까지 유효)
        token_valid = (
            self.access_token and 
            self.token_expire_time and 
            current_time < self.token_expire_time - TOKEN_EXPIRY_BUFFER
        )
        
        # 토큰이 유효하지 않으면 새로 발급
        if not token_valid:
            logger.log_system("토큰이 유효하지 않아 새로 발급합니다.")
            token = self._get_access_token()
        else:
            token = self.access_token
            remaining_hours = (self.token_expire_time - current_time) / 3600
            logger.log_debug(f"유효한 토큰 사용 (만료까지 {remaining_hours:.1f}시간 남음)")
        
        # 기본 헤더 설정
        default_headers = {
            "authorization": f"Bearer {token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_cont": "",
        }
        
        if headers:
            default_headers.update(headers)
        
        token_refresh_attempts = 0
        
        # 재시도 루프
        for attempt in range(max_retries):
            try:
                # HTTP 요청 실행 (타임아웃 적용)
                if method.upper() == "GET":
                    response = requests.get(
                        url, 
                        headers=default_headers, 
                        params=params, 
                        timeout=REQUEST_TIMEOUT
                    )
                else:
                    response = requests.post(
                        url, 
                        headers=default_headers, 
                        json=data, 
                        timeout=REQUEST_TIMEOUT
                    )
                
                # 500 에러 처리 (서버 내부 오류)
                if response.status_code == 500:
                    if token_refresh_attempts >= MAX_TOKEN_REFRESH_ATTEMPTS:
                        error_msg = f"최대 토큰 갱신 시도 횟수({MAX_TOKEN_REFRESH_ATTEMPTS}회) 초과"
                        logger.log_error(Exception(error_msg), error_msg)
                        raise Exception(error_msg)
                    
                    logger.log_warning(f"500 에러 발생, 토큰 강제 갱신 시도... (시도 {token_refresh_attempts + 1}/{MAX_TOKEN_REFRESH_ATTEMPTS})")
                    self.access_token = None  # 토큰 초기화
                    self.token_expire_time = None
                    
                    # 토큰 강제 갱신
                    try:
                        new_token = self._get_access_token()
                        logger.log_system("토큰 강제 갱신 성공")
                        default_headers["authorization"] = f"Bearer {new_token}"
                        token_refresh_attempts += 1
                        time.sleep(1)  # 토큰 갱신 후 짧은 대기
                        continue  # 새 토큰으로 재시도
                    except Exception as token_error:
                        logger.log_error(token_error, "토큰 강제 갱신 실패")
                        if token_refresh_attempts >= MAX_TOKEN_REFRESH_ATTEMPTS - 1:
                            raise Exception("Token refresh failed after multiple attempts")
                        token_refresh_attempts += 1
                        continue
                
                # HTTP 응답 상태 검사
                response.raise_for_status()
                result = response.json()
                
                # API 응답 코드 체크
                if result.get("rt_cd") != "0":
                    error_msg = result.get("msg1", "Unknown error")
                    
                    # 토큰 관련 에러 키워드
                    token_error_keywords = ["token", "auth", "unauthorized", "인증", "토큰"]
                    
                    # 토큰 관련 에러인 경우 토큰 갱신
                    if any(keyword in error_msg.lower() for keyword in token_error_keywords):
                        if token_refresh_attempts < MAX_TOKEN_REFRESH_ATTEMPTS:
                            logger.log_warning(f"토큰 관련 에러 발생 ({error_msg}), 토큰 갱신 시도...")
                            self.access_token = None  # 토큰 초기화
                            self.token_expire_time = None
                            
                            # 토큰 재발급 시도
                            try:
                                new_token = self._get_access_token()
                                default_headers["authorization"] = f"Bearer {new_token}"
                                token_refresh_attempts += 1
                                time.sleep(1)
                                continue
                            except Exception as token_error:
                                logger.log_error(token_error, "토큰 재발급 실패")
                    
                    # API 에러 로그
                    logger.log_error(Exception(f"API error: {error_msg}"), f"API 응답 오류 (rt_cd: {result.get('rt_cd')})")
                    raise Exception(f"API error: {error_msg}")
                
                # 성공 응답 반환
                return result
                
            except requests.exceptions.Timeout:
                error_msg = f"요청 타임아웃 ({REQUEST_TIMEOUT}초)"
                if attempt < max_retries - 1:
                    wait_time = (attempt + 1) * 2  # 지수 백오프
                    logger.log_warning(f"{error_msg}, {wait_time}초 후 재시도...")
                    time.sleep(wait_time)
                else:
                    logger.log_error(Exception(error_msg), "요청 타임아웃으로 실패")
                    raise
                    
            except requests.exceptions.RequestException as e:
                if attempt < max_retries - 1:
                    wait_time = (attempt + 1) * 2  # 지수 백오프
                    logger.log_warning(f"요청 실패 ({type(e).__name__}), {wait_time}초 후 재시도...")
                    time.sleep(wait_time)
                else:
                    logger.log_error(e, f"요청 실패 (최대 재시도 횟수 {max_retries}회 초과)")
                    raise
            
        # 모든 재시도 실패
        error_msg = f"최대 재시도 횟수({max_retries}회) 초과"
        logger.log_error(Exception(error_msg), error_msg)
        raise Exception(error_msg)
    
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
        
        # 모의투자 여부 확인
        is_dev = False
        try:
            # 테스트 모드 확인 (TEST_MODE=True이면 모의투자, 아니면 실전투자)
            test_mode_str = os.getenv("TEST_MODE", "False").strip()
            is_dev = test_mode_str.lower() in ['true', '1', 't', 'y', 'yes']
            logger.log_system(f"계좌 조회 - 모의투자 모드: {is_dev} (환경 변수 TEST_MODE: '{test_mode_str}')")
        except Exception as e:
            logger.log_error(e, "TEST_MODE 환경 변수 확인 중 오류")
        
        # 거래소코드 설정 (모의투자 또는 실전투자)
        tr_id = "VTTC8434R" if is_dev else "TTTC8434R"  # 모의투자(V) vs 실전투자(T)
        
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
                error_msg = result.get("msg1", "알 수 없는 오류")
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
        
        # 모의투자 여부 확인
        is_dev = False
        try:
            # 테스트 모드 확인 (TEST_MODE=True이면 모의투자, 아니면 실전투자)
            test_mode_str = os.getenv("TEST_MODE", "False").strip()
            is_dev = test_mode_str.lower() in ['true', '1', 't', 'y', 'yes']
            logger.log_system(f"주문 실행 - 모의투자 모드: {is_dev} (환경 변수 TEST_MODE: '{test_mode_str}')")
        except Exception as e:
            logger.log_error(e, "TEST_MODE 환경 변수 확인 중 오류")
        
        # 매수/매도 구분 (모의투자/실거래 TR_ID 구분)
        if side.upper() == "BUY":
            tr_id = "TTTC0012U" if not is_dev else "TTTC0012U"  # 매수 (모의투자:VTTC0012U / 실거래:TTTC0012U)
        else:
            tr_id = "TTTC0011U" if not is_dev else "TTTC0011U"  # 매도 (모의투자:VTTC0011U / 실거래:TTTC0011U)
        
        headers = {
            "tr_id": tr_id,
            "Content-Type": "application/json"
        }
        
        # 주문 유형 (00: 지정가, 01: 시장가)
        ord_dvsn = "01" if order_type.upper() == "MARKET" else "00"
        
        data = {
            "CANO": self.account_no[:8],
            "ACNT_PRDT_CD": self.account_no[8:],
            "PDNO": symbol,
            "ORD_DVSN": ord_dvsn,
            "ORD_QTY": str(quantity),
            "ORD_UNPR": str(price) if ord_dvsn == "00" else "0",
            "CTAC_TLNO": "", # 연락전화번호(널값 가능)
            "SLL_TYPE": "00", # 매도유형(00: 고정값)
            "ALGO_NO": ""     # 알고리즘 주문번호(선택값)
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
    
    def get_minute_price(self, symbol: str, time_unit: str = "1") -> Dict[str, Any]:
        """분 단위 가격 정보 조회
        
        Args:
            symbol (str): 종목 코드
            time_unit (str): 시간 단위 ("1":1분, "3":3분, "5":5분, "10":10분, "15":15분, "30":30분, "60":60분)
        """
        try:
            path = "/uapi/domestic-stock/v1/quotations/inquire-time-itemconclusion"  # 시간별 체결가 조회
            
            # 시간 단위별 FID_PW_DATA_INTP_HOUR_CLS_CODE 설정
            hour_cls_code_map = {
                "1": "0",   # 1분
                "3": "1",   # 3분 (지원 안됨)
                "5": "2",   # 5분
                "10": "3",  # 10분
                "15": "4",  # 15분 
                "30": "5",  # 30분
                "60": "6"   # 60분
            }
            
            hour_cls_code = hour_cls_code_map.get(time_unit, "0")
            
            headers = {
                "tr_id": "FHKST01010600"  # 분봉 조회 TR ID 
            }
            
            params = {
                "FID_COND_MRKT_DIV_CODE": "J",      # 시장구분 (J: 주식)
                "FID_INPUT_ISCD": symbol,            # 종목코드
                "FID_PW_DATA_INTP_HOUR_CLS_CODE": hour_cls_code,  # 시간 클래스 코드
                "FID_HOUR_CLS_CODE": hour_cls_code   # 시간 구분 코드 (중복인듯하지만 API 문서 따름)
            }
            
            result = self._make_request("GET", path, headers=headers, params=params)
            
            if result.get("rt_cd") == "0":
                logger.log_system(f"{symbol} {time_unit}분봉 데이터 조회 성공")
                
                # 응답 구조 통일화 (output.lst 형태로)
                if "output" in result and not isinstance(result.get("output"), dict):
                    # output이 리스트인 경우, lst 키로 변환
                    output_data = result.get("output", [])
                    result["output"] = {"lst": output_data}
                elif "output" not in result:
                    # output이 아예 없는 경우 빈 구조 생성
                    result["output"] = {"lst": []}
                
                # output1, output2 등의 데이터가 있으면 lst에 통합
                if "output1" in result and isinstance(result["output1"], list):
                    result["output"] = {"lst": result["output1"]}
                elif "output2" in result and isinstance(result["output2"], list):
                    result["output"] = {"lst": result["output2"]}
                    
            else:
                error_msg = result.get("msg1", "알 수 없는 오류")
                logger.log_system(f"{symbol} {time_unit}분봉 데이터 조회 실패: {error_msg}")
            
            return result
            
        except Exception as e:
            logger.log_error(e, f"{symbol} 분봉 데이터 조회 중 오류 발생")
            # 오류 발생 시에도 최소한의 응답 구조 제공
            return {
                "rt_cd": "9999",
                "msg1": str(e),
                "output": {"lst": []}
            }
    
    def get_daily_price(self, symbol: str, max_days: int = 30) -> Dict[str, Any]:
        """일별 가격 정보 조회"""
        try:
            path = "/uapi/domestic-stock/v1/quotations/inquire-daily-price"
            headers = {
                "tr_id": "FHKST01010400"  # 주식 일별 데이터 조회
            }
            params = {
                "FID_COND_MRKT_DIV_CODE": "J",  # 주식
                "FID_INPUT_ISCD": symbol,
                "FID_PERIOD_DIV_CODE": "D",  # 일봉
                "FID_ORG_ADJ_PRC": "1",  # 수정주가 여부
                "FID_INPUT_DATE_1": "",  # 조회 시작일 (빈값: 가장 최근)
                "FID_INPUT_DATE_2": "",  # 조회 종료일 (빈값: 가장 오래된)
                "FID_INPUT_DATE_PERIODIC_DIV_CODE": "0",  # 기간분류코드
                "FID_INCL_OPEN_START_PRC_YN": "Y",  # 시가 포함
            }
            
            result = self._make_request("GET", path, headers=headers, params=params)
            
            if result.get("rt_cd") == "0":
                logger.log_system(f"{symbol} 일별 가격 데이터 조회 성공")
            else:
                error_msg = result.get("msg1", "알 수 없는 오류")
                logger.log_system(f"{symbol} 일별 가격 데이터 조회 실패: {error_msg}")
            
            # 응답 구조 확인 및 데이터 처리
            if result.get("rt_cd") == "0" and "output1" in result and "output2" not in result:
                # output2가 누락된 경우, 데이터를 output2 형식으로 변환하여 추가
                result["output2"] = []
                if "output1" in result and isinstance(result["output1"], dict):
                    daily_items = []
                    # 최근 30일 데이터 생성 (실제 데이터가 없는 경우 대비)
                    for i in range(max_days):
                        date = (datetime.now() - timedelta(days=i)).strftime("%Y%m%d")
                        daily_items.append({
                            "stck_bsop_date": date,
                            "stck_clpr": result["output1"].get("stck_prpr", "0"),  # 종가
                            "acml_vol": result["output1"].get("acml_vol", "0"),  # 거래량
                            "stck_oprc": result["output1"].get("stck_oprc", "0"),  # 시가
                            "stck_hgpr": result["output1"].get("stck_hgpr", "0"),  # 고가
                            "stck_lwpr": result["output1"].get("stck_lwpr", "0")   # 저가
                        })
                    result["output2"] = daily_items
                    logger.log_system(f"{symbol} 일별 데이터 변환 처리 완료 (출력2 형식으로 생성)")
            
            return result
            
        except Exception as e:
            logger.log_error(e, f"{symbol} 일별 가격 데이터 조회 중 오류 발생")
            # 오류 발생 시에도 최소한의 응답 구조 제공
            return {
                "rt_cd": "9999",
                "msg1": str(e),
                "output1": {},
                "output2": []
            }
    
    def get_volume_ranking(self, market: str = "ALL") -> Dict[str, Any]:
        """거래량 순위 종목 조회
        
        Args:
            market (str): 시장 구분 ("ALL", "KOSPI", "KOSDAQ")
            
        Returns:
            Dict[str, Any]: API 응답 결과
                - rt_cd: 성공/실패 여부 ("0": 성공)
                - msg_cd: 응답코드
                - msg1: 응답메세지
                - output: 거래량 순위 데이터 리스트
        """
        try:
            logger.log_system(f"[API] 거래량 순위 조회 시작 - 시장: {market}")
            
            path = "/uapi/domestic-stock/v1/quotations/volume-rank"
            headers = {
                "tr_id": "FHPST01710000",  # 거래량 순위 TR ID
                "custtype": "P"  # 개인
            }
            
            # 시장 구분 코드 매핑
            market_code_map = {
                "ALL": "0000",     # 전체
                "KOSPI": "0001",   # 코스피
                "KOSDAQ": "1001",  # 코스닥
                "0": "0000",      # 전체
                "J": "0000"       # 전체 (주식)
            }
            
            market_code = market_code_map.get(market.upper(), "0000")
            logger.log_system(f"[API] 시장 코드 매핑: {market} -> {market_code}")
            
            # API 요청 파라미터 - API 문서에 맞춰 수정
            params = {
                "FID_COND_MRKT_DIV_CODE": "J",          # 조건 시장 분류 코드 (J: 주식)
                "FID_COND_SCR_DIV_CODE": "20171",       # 조건 화면 분류 코드 (20171: 거래량순위)
                "FID_INPUT_ISCD": market_code,          # 입력 종목코드 (시장구분)
                "FID_DIV_CLS_CODE": "0",                # 분류 구분 코드 (0: 전체)
                "FID_BLNG_CLS_CODE": "0",               # 소속 구분 코드 (0: 평균거래량)
                "FID_TRGT_CLS_CODE": "111111111",       # 대상 구분 코드 (증거금 30% 40% 50% 60% 100% 신용보증금 30% 40% 50% 60%)
                "FID_TRGT_EXLS_CLS_CODE": "0000000000", # 대상 제외 구분 코드 (10자리)
                "FID_INPUT_PRICE_1": "",                # 입력 가격1 (전체 가격 대상)
                "FID_INPUT_PRICE_2": "",                # 입력 가격2 (전체 가격 대상)
                "FID_VOL_CNT": "",                      # 거래량 수 (전체 거래량 대상)
                "FID_INPUT_DATE_1": ""                  # 입력 날짜1 (공란)
            }
            
            logger.log_system(f"[API] 요청 파라미터: {params}")
            
            # API 호출 전 토큰 상태 확인
            if not self.access_token or not self.token_expire_time:
                logger.log_system("[API] 거래량 순위 조회 전 - 토큰이 없어서 발급 필요")
                self._get_access_token()
            else:
                remaining_hours = (self.token_expire_time - datetime.now().timestamp()) / 3600
                logger.log_system(f"[API] 거래량 순위 조회 전 - 토큰 유효 (만료까지 {remaining_hours:.1f}시간)")
            
            # API 요청 실행
            result = self._make_request("GET", path, headers=headers, params=params)
            
            # 응답 로깅 (디버깅용)
            logger.log_system(f"[API] 응답 rt_cd: {result.get('rt_cd')}")
            logger.log_system(f"[API] 응답 msg_cd: {result.get('msg_cd', 'N/A')}")
            logger.log_system(f"[API] 응답 msg1: {result.get('msg1', 'N/A')}")
            
            if result.get("rt_cd") == "0":
                # 성공 처리
                logger.log_system(f"[API] 거래량 순위 조회 성공 (시장: {market})")
                
                # output 데이터 확인 및 처리
                output_data = result.get("output", [])
                if isinstance(output_data, list) and output_data:
                    logger.log_system(f"[API] 거래량 순위 데이터 수: {len(output_data)}")
                    # 상위 30개 종목만 로깅  
                    for i, item in enumerate(output_data[:30]):
                        if isinstance(item, dict):
                            # 첫번째 항목의 필드 정보 출력
                            if i == 0:
                                logger.log_system(f"[API] 첫번째 항목의 전체 필드: {list(item.keys())}")
                                
                            # 필드명 매핑 - API 문서에 맞춰 수정
                            symbol = item.get("mksc_shrn_iscd", "N/A")  # 유가증권 단축 종목코드
                            name = item.get("hts_kor_isnm", "N/A")      # HTS 한글 종목명
                            volume = item.get("acml_vol", "0")          # 누적 거래량
                            price = item.get("stck_prpr", "0")          # 주식 현재가
                            change_rate = item.get("prdy_ctrt", "0")    # 전일 대비율
                            rank = item.get("data_rank", str(i+1))      # 데이터 순위
                            
                            logger.log_system(f"[API] #{rank} {symbol} {name}: 가격 {price}원, 거래량 {volume}, 등락률 {change_rate}%")
                else:
                    logger.log_system(f"[API] output 데이터가 비어있거나 리스트가 아님: {type(output_data)}")
            else:
                # 실패 처리
                error_msg = result.get("msg1", "알 수 없는 오류")
                logger.log_system(f"[API] 거래량 순위 조회 실패: {error_msg}")
                logger.log_system(f"[API] 전체 응답: {result}")
            
            return result
            
        except Exception as e:
            logger.log_error(e, f"[API] 거래량 순위 조회 중 예외 발생 (시장: {market})")
            # 예외 타입과 메시지 로깅
            logger.log_system(f"[API] 예외 타입: {type(e).__name__}")
            logger.log_system(f"[API] 예외 메시지: {str(e)}")
            
            # 오류 발생 시에도 최소한의 응답 구조 제공
            return {
                "rt_cd": "9999",
                "msg_cd": "ERR",
                "msg1": str(e),
                "output": []
            }

    def get_market_trading_volume(self, market: str = "ALL") -> Dict[str, Any]:
        """시장 거래량 정보 조회"""
        try:
            path = "/uapi/domestic-stock/v1/quotations/inquire-total-market-price"
            headers = {
                "tr_id": "FHKST03030100"  # 전체시장시세
            }
            
            # 시장 코드 설정
            market_code = ""
            if market == "KOSPI":
                market_code = "0"
            elif market == "KOSDAQ":
                market_code = "1"
            # 기본값은 전체 시장
            
            params = {
                "FID_COND_MRKT_DIV_CODE": market_code,
                "FID_COND_SCR_DIV_CODE": "20171",  # 화면번호
                "FID_INPUT_ISCD": "0",
                "FID_DIV_CLS_CODE": "0",
                "FID_BLNG_CLS_CODE": "0",
                "FID_TRGT_CLS_CODE": "111111111",
                "FID_TRGT_EXLS_CLS_CODE": "000000",
                "FID_INPUT_PRICE_1": "0",
                "FID_INPUT_PRICE_2": "0",
                "FID_VOL_CNT": "0",
                "FID_INPUT_DATE_1": ""
            }
            
            result = self._make_request("GET", path, headers=headers, params=params)
            
            if result.get("rt_cd") == "0":
                logger.log_system(f"시장 거래량 데이터 조회 성공 (시장: {market})")
            else:
                error_msg = result.get("msg1", "알 수 없는 오류")
                logger.log_system(f"시장 거래량 데이터 조회 실패: {error_msg}")
            
            # 응답 구조가 없는 경우 기본 구조 생성
            if "output2" not in result or not result["output2"]:
                # 데이터가 없는 경우, 기본 구조 생성
                result["output2"] = [
                    {
                        "mksc_shrn_iscd": "TOTAL",
                        "prdy_vrss_vol": "1000000",  # 기본값 100만주
                        "acml_vol": "1000000",
                        "prdy_vrss_vol_rate": "0"
                    }
                ]
                logger.log_system("시장 거래량 데이터 없음, 기본 데이터 생성")
                
            return result
            
        except Exception as e:
            logger.log_error(e, "시장 거래량 데이터 조회 중 오류 발생")
            # 오류 발생 시에도 최소한의 응답 구조 제공
            return {
                "rt_cd": "9999",
                "msg1": str(e),
                "output1": {},
                "output2": [
                    {
                        "mksc_shrn_iscd": "ERROR",
                        "prdy_vrss_vol": "1000000",  # 기본값 100만주
                        "acml_vol": "1000000",
                        "prdy_vrss_vol_rate": "0"
                    }
                ]
            }

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
                raise Exception(f"API 응답 실패: {error_msg}")
        
        except requests.Timeout:
            logger.log_system(f"[API] {symbol} 종목 정보 조회 타임아웃 발생 (3초)")
            raise
        except Exception as e:
            logger.log_error(e, f"[API] {symbol} 종목 정보 조회 중 오류 발생")
            raise



# 싱글톤 인스턴스
api_client = KISAPIClient()
