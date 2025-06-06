"""
한국투자증권 API 클라이언트
"""
import requests
import json
import hashlib
import time
from datetime import datetime, timedelta, time as time_class
from typing import Dict, Any, Optional, List
from config.settings import config, APIConfig
from utils.logger import logger
import os
from dotenv import load_dotenv
from pathlib import Path
from utils.database import database_manager
import asyncio
import threading
import socket

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
            
            # 환경 변수에서 읽어오기
            self.base_url = os.getenv("KIS_BASE_URL")
            self.app_key = os.getenv("KIS_APP_KEY")
            self.app_secret = os.getenv("KIS_APP_SECRET")
            self.account_no = os.getenv("KIS_ACCOUNT_NO")
            
            # 설정에서 값 가져오기
            #self.config = config.get("api", APIConfig.from_env())
            #self.base_url = self.config.base_url
            #self.app_key = self.config.app_key
            #self.app_secret = self.config.app_secret
            #self.account_no = self.config.account_no
            
            # 앱 시작 시 파일에서 유효한 토큰 로드
            self.load_token_from_file()
            
            self._initialized = True
        
    def _get_access_token(self) -> str:
        """접근 토큰 발급/갱신"""
        current_time = datetime.now().timestamp()
        
        # 토큰 재발급 임계값 (12시간 = 43200초)
        # KIS 토큰은 24시간 유효하므로 12시간으로 설정
        TOKEN_RENEWAL_THRESHOLD = 43200
        
        # 먼저 파일에서 토큰 정보 로드 시도
        token_loaded = self.load_token_from_file()
        
        # 파일에서 토큰을 성공적으로 로드했고, 토큰이 유효한 경우
        if token_loaded and self.access_token and self.token_expire_time:
            # 만료까지 남은 시간
            remaining_seconds = self.token_expire_time - current_time
            remaining_hours = remaining_seconds / 3600
            
            # 임계값 이상 시간이 남았으면 기존 토큰 재사용
            if remaining_seconds > TOKEN_RENEWAL_THRESHOLD:
                # 시간당 한번만 로깅하도록 제한
                #if int(remaining_hours) % 2 == 0 or remaining_hours < 1.0:
                #    logger.log_system(f"[토큰재사용] 기존 토큰이 유효하여 재사용합니다. (만료까지 {remaining_hours:.1f}시간 남음)")
                return self.access_token
            else:
                # 만료 임계값 이하로 남았을 때만 갱신
                logger.log_system(f"[토큰갱신] 토큰이 {remaining_hours:.1f}시간 후 만료 예정, 갱신합니다. (현재={datetime.fromtimestamp(current_time).strftime('%Y-%m-%d %H:%M:%S')}, 만료={datetime.fromtimestamp(self.token_expire_time).strftime('%Y-%m-%d %H:%M:%S')})")
        else:
            # 토큰 발급/갱신 작업 로그
            if not token_loaded:
                logger.log_system("[토큰발급] 파일에서 유효한 토큰을 찾을 수 없어 새로운 토큰을 발급합니다...")
            else:
                logger.log_system("[토큰발급] 새로운 KIS API 토큰 발급을 시작합니다...")
        
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
        """파일에서 토큰 정보 로드
        
        Returns:
            bool: 토큰 로드 성공 여부 (True: 유효한 토큰 로드 성공, False: 실패)
        """
        # 토큰 재발급 임계값 (12시간 = 43200초)
        # KIS 토큰은 24시간 유효하므로 12시간으로 설정
        TOKEN_RENEWAL_THRESHOLD = 43200
        
        # 현재 시간 가져오기 (한 번만 계산)
        current_time = datetime.now().timestamp()
        
        try:
            # 1. 파일 존재 여부 확인
            if not os.path.exists(TOKEN_FILE_PATH):
                logger.log_debug(f"토큰 파일이 존재하지 않습니다: {TOKEN_FILE_PATH}")
                return False
            
            # 2. 파일 읽기 시도
            try:
                with open(TOKEN_FILE_PATH, 'r') as f:
                    token_info = json.load(f)
            except (json.JSONDecodeError, FileNotFoundError) as e:
                logger.log_error(e, f"토큰 파일 데이터 파싱 오류: {TOKEN_FILE_PATH}")
                return False
            
            # 3. 토큰 정보 구조 확인
            if 'current' not in token_info or not token_info['current']:
                logger.log_debug("토큰 파일에 'current' 필드가 없습니다")
                return False
            
            current_token = token_info['current']
            
            # 4. 필수 필드 확인
            if 'token' not in current_token or 'expire_time' not in current_token:
                logger.log_debug("토큰 파일에 필수 필드(token/expire_time)가 없습니다")
                return False
            
            # 5. 토큰 정보 로드
            self.access_token = current_token['token']
            self.token_expire_time = current_token['expire_time']
            self.token_issue_time = current_token['issue_time']
            
            # 6. 토큰 유효성 검사
            if current_time >= self.token_expire_time:
                # 이미 만료된 토큰
                expire_time_str = datetime.fromtimestamp(self.token_expire_time).strftime("%Y-%m-%d %H:%M:%S")
                logger.log_debug(f"만료된 토큰이 발견됨. 만료 시간: {expire_time_str}")
                self.access_token = None
                self.token_expire_time = None
                self.token_issue_time = None
                return False
            
            # 7. 만료 임박성 검사 - 로그 수준 조절
            hours_remaining = (self.token_expire_time - current_time) / 3600
            time_remaining = self.token_expire_time - current_time
            
            # 만료 시간이 임계값 미만이면 경고 로그 출력
            if time_remaining < TOKEN_RENEWAL_THRESHOLD:
                logger.log_system(f"유효한 토큰이지만 만료까지 {hours_remaining:.1f}시간만 남았습니다. (임계값: {TOKEN_RENEWAL_THRESHOLD/3600:.1f}시간)")
            #else:
                # 토큰이 유효하고 충분한 시간이 남은 경우 로그 최소화 (시간당 1번만 출력)
                #if int(hours_remaining) % 6 == 0 or hours_remaining < 1.0:
                #    logger.log_system(f"파일에서 유효한 토큰을 로드했습니다. 만료까지 {hours_remaining:.1f}시간 남음")
            
            return True
                
        except Exception as e:
            logger.log_error(e, "파일에서 토큰 정보를 로드하는 중 예상치 못한 오류 발생")
            # 오류 발생 시 토큰 정보 초기화
            self.access_token = None
            self.token_expire_time = None
            self.token_issue_time = None
            return False
    
    async def _get_access_token_async(self):
        """비동기 방식으로 액세스 토큰 획득"""
        # 현재 스레드에 이벤트 루프가 없는 경우 새로 생성
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            # 현재 스레드에 이벤트 루프가 없는 경우 새로 생성
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
        # 스레드별 락 생성 (기존 _token_lock 대신 임시 락 생성)
        async with asyncio.Lock():
            # 파일에서 토큰 정보 다시 확인
            token_loaded = self.load_token_from_file()
            
            current_time = datetime.now().timestamp()
            
            # 토큰 재발급 임계값 (12시간 = 43200초)
            # KIS 토큰은 24시간 유효하므로 12시간으로 설정
            TOKEN_RENEWAL_THRESHOLD = 43200
            
            # 토큰이 있고 유효한지 확인
            if self.access_token and self.token_expire_time:
                # 토큰 만료까지 남은 시간 계산 (시간 단위)
                remaining_seconds = self.token_expire_time - current_time
                remaining_hours = remaining_seconds / 3600
                
                # 만료 시간이 임계값 이상 남았으면 기존 토큰 사용
                if remaining_seconds > TOKEN_RENEWAL_THRESHOLD:
                    # 디버깅 과도한 로깅 방지: 1시간마다 또는 30분 미만일 때만 로깅
                    # if int(remaining_hours) % 1 == 0 or remaining_hours < 0.5:
                    #     logger.log_system(f"[토큰재사용] 유효한 토큰이 있습니다. 현재={datetime.fromtimestamp(current_time).strftime('%Y-%m-%d %H:%M:%S')}, "
                    #                      f"만료={datetime.fromtimestamp(self.token_expire_time).strftime('%Y-%m-%d %H:%M:%S')}, "
                    #                      f"남은시간={remaining_hours:.1f}시간")
                    return self.access_token
                else:
                    # 만료 임계값 이내인 경우에만 갱신 시도
                    logger.log_system(f"[토큰갱신] 토큰 만료 {remaining_hours:.1f}시간 이내, 갱신 필요: 현재={datetime.fromtimestamp(current_time).strftime('%Y-%m-%d %H:%M:%S')}, "
                                     f"만료={datetime.fromtimestamp(self.token_expire_time).strftime('%Y-%m-%d %H:%M:%S')}")
            else:
                # 파일에서 로드 시도 결과에 따라 다른 메시지 표시
                if token_loaded:
                    logger.log_system("[토큰오류] 파일에서 토큰을 로드했지만 유효하지 않습니다.")
                else:
                    logger.log_system("[토큰없음] 유효한 토큰이 없어 새로 발급합니다.")
            
            # 새 토큰 발급 필요
            try:
                logger.log_system("[토큰발급] 새 토큰 발급 시작...")
                # 현재 스레드에 이벤트 루프가 없는 경우 새로 생성
                try:
                    loop = asyncio.get_event_loop()
                except RuntimeError:
                    # 현재 스레드에 이벤트 루프가 없는 경우 새로 생성
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                
                new_token = await loop.run_in_executor(None, self._get_access_token)
                logger.log_system("[토큰발급] 새 토큰 발급 완료")
                return new_token
            except Exception as e:
                logger.log_error(e, "[토큰발급] 새 토큰 발급 중 오류 발생")
                raise

    async def is_token_valid(self, min_hours: float = 0.5) -> bool:
        """토큰이 유효한지 확인
        
        Args:
            min_hours (float): 최소 유효 시간 (시간 단위, 기본값 30분)
            
        Returns:
            bool: 토큰 유효 여부 (True: 유효, False: 만료 또는 없음)
        """
        # 로직 쏘하 및 성능 향상을 위해 전체 lock을 추가하지 않음
        # 경처 노드에서는 토큰 유효성만 확인하면 됨
        try:
            # 토큰이 없으면 유효하지 않음 - 파일 읽기 불필요
            if not self.access_token or not self.token_expire_time:
                return False
                
            # 토큰 만료 시간을 확인
            current_time = datetime.now().timestamp()
            time_remaining = self.token_expire_time - current_time
            
            # 최소 유효 시간 이상 남았는지 확인
            if time_remaining > (min_hours * 3600):
                # 로그 수량 최소화 - 디버그 레벨에서만 프린트
                # 파일/DB 접근 최소화를 위해 로그 우선순위 낮춤
                if time_remaining < 7200:  # 2시간 이내로 남았을 때만 로그 출력
                    hours_remaining = time_remaining / 3600
                    logger.log_debug(f"토큰이 유효함. 만료까지 {hours_remaining:.1f}시간 남음")
                return True
            
            # 만료 시간이 min_hours 이내로 남았거나 이미 만료됨
            if time_remaining <= 0:
                logger.log_system("토큰이 만료되어 새로 발급이 필요합니다.")
            else:
                minutes_remaining = time_remaining / 60
                logger.log_system(f"토큰 만료가 임박합니다. {minutes_remaining:.1f}분 남음, 갱신 필요")
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
        # 현재 스레드에 이벤트 루프가 없는 경우 새로 생성
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            # 현재 스레드에 이벤트 루프가 없는 경우 새로 생성
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
        # 스레드별 락 생성
        async with asyncio.Lock():
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
        # 현재 스레드에 이벤트 루프가 없는 경우 새로 생성
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            # 현재 스레드에 이벤트 루프가 없는 경우 새로 생성
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
        # 스레드별 락 생성
        async with asyncio.Lock():
            try:
                # 현재 스레드에 이벤트 루프가 없는 경우 새로 생성
                try:
                    loop = asyncio.get_event_loop()
                except RuntimeError:
                    # 현재 스레드에 이벤트 루프가 없는 경우 새로 생성
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                
                # 동기 함수 _get_access_token을 실행하여 토큰 발급 (run_in_executor 사용)
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
                     params: Dict = None, data: Dict = None, max_retries: int = 3,
                     raise_on_error: bool = False) -> Dict[str, Any]:
        """API 요청 실행
        
        Args:
            method: HTTP 메서드 (GET, POST 등)
            path: API 경로
            headers: 요청 헤더
            params: URL 파라미터 (GET 요청)
            data: 요청 바디 (POST 요청)
            max_retries: 최대 재시도 횟수
            raise_on_error: True인 경우 API 오류 시 예외 발생, False인 경우 오류 정보가 포함된 응답 반환
        
        Returns:
            API 응답 딕셔너리
        """
        url = f"{self.base_url}{path}"
        
        # 분봉 데이터 조회를 위한 특수 처리 추가
        is_minute_chart_request = False
        if path == "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice":
            is_minute_chart_request = True
            #logger.log_system("[특수처리] 분봉 데이터 조회 요청 감지")
        
        # 상수 정의
        TOKEN_RENEWAL_THRESHOLD = 43200  # 12시간 (43200초)
        MAX_TOKEN_REFRESH_ATTEMPTS = 2
        REQUEST_TIMEOUT = 30  # 요청 타임아웃 (초)
        
        # 상세 로그 추가 - 요청 정보
        # logger.log_system(f"API 요청: method={method}, path={path}, url={url}")
        # if params:
        #     logger.log_system(f"API 파라미터: {params}")
        # if headers:
        #     safe_headers = {k: v for k, v in headers.items() if k.lower() not in ["appkey", "appsecret", "authorization"]}
        #     logger.log_system(f"API 헤더: {safe_headers}")
            
        # 토큰 유효성 확인 (KIS 토큰은 24시간 유효)
        current_time = datetime.now().timestamp()
        
        # 파일에서 최신 토큰 정보 로드
        self.load_token_from_file()
        
        # 토큰이 있고 아직 유효한지 확인 (만료 12시간 전까지 유효)
        token_valid = (
            self.access_token and 
            self.token_expire_time and 
            current_time < self.token_expire_time - TOKEN_RENEWAL_THRESHOLD
        )
        
        # 토큰이 유효하지 않으면 새로 발급
        if not token_valid:
            logger.log_system("토큰이 유효하지 않아 새로 발급합니다.")
            try:
                token = self._get_access_token()
            except Exception as e:
                logger.log_error(e, "토큰 발급 실패")
                if raise_on_error:
                    raise
                return {
                    "rt_cd": "9999",
                    "msg1": f"토큰 발급 실패: {str(e)}",
                    "error_type": "token_issue_error"
                }
        else:
            token = self.access_token
            # remaining_hours = (self.token_expire_time - current_time) / 3600
            # logger.log_debug(f"유효한 토큰 사용 (만료까지 {remaining_hours:.1f}시간 남음)")
        
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
                    #logger.log_system(f"GET 요청 시도 ({attempt+1}/{max_retries}): {url}")
                    response = requests.get(
                        url, 
                        headers=default_headers, 
                        params=params, 
                        timeout=REQUEST_TIMEOUT
                    )
                else:
                    logger.log_system(f"POST 요청 시도 ({attempt+1}/{max_retries}): {url}")
                    response = requests.post(
                        url, 
                        headers=default_headers, 
                        json=data, 
                        timeout=REQUEST_TIMEOUT
                    )
                
                # 응답 상태 코드 로그
                #logger.log_system(f"API 응답 상태 코드: {response.status_code}")
                
                # # 응답 내용 로그 (디버깅용)
                # try:
                #     resp_text = response.text[:1000] + "..." if len(response.text) > 1000 else response.text
                #     logger.log_system(f"API 응답 내용: {resp_text}")
                # except Exception as log_e:
                #     logger.log_system(f"응답 내용 로깅 중 오류: {str(log_e)}")
                
                # 500 에러 처리 (서버 내부 오류)
                if response.status_code == 500:
                    if token_refresh_attempts >= MAX_TOKEN_REFRESH_ATTEMPTS:
                        error_msg = f"최대 토큰 갱신 시도 횟수({MAX_TOKEN_REFRESH_ATTEMPTS}회) 초과"
                        logger.log_error(Exception(error_msg), error_msg)
                        if raise_on_error:
                            raise Exception(error_msg)
                        return {
                            "rt_cd": "9999",
                            "msg1": error_msg,
                            "error_type": "token_refresh_limit_exceeded",
                            "http_status": 500
                        }
                    
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
                            if raise_on_error:
                                raise Exception("Token refresh failed after multiple attempts")
                            return {
                                "rt_cd": "9999",
                                "msg1": "토큰 갱신 시도 실패",
                                "error_type": "token_refresh_failed",
                                "http_status": 500
                            }
                        token_refresh_attempts += 1
                        continue
                
                # 응답 내용 로그 (200이 아닌 경우)
                if response.status_code != 200:
                    try:
                        logger.log_system(f"API 오류 응답 내용: {response.text[:500]}")
                    except:
                        logger.log_system("API 오류 응답 텍스트 추출 실패")
                    
                    if raise_on_error:
                        response.raise_for_status()
                    return {
                        "rt_cd": "9999",
                        "msg1": f"HTTP 에러: {response.status_code}",
                        "error_type": "http_error",
                        "http_status": response.status_code,
                        "response_text": response.text[:1000] if response.text else ""
                    }
                
                # JSON 파싱 시도
                try:
                    result = response.json()
                except json.JSONDecodeError as json_err:
                    logger.log_error(json_err, f"JSON 파싱 실패: {response.text[:300]}")
                    if raise_on_error:
                        raise
                    return {
                        "rt_cd": "9999",
                        "msg1": "JSON 파싱 실패",
                        "error_type": "json_parse_error",
                        "http_status": response.status_code,
                        "response_text": response.text[:1000] if response.text else ""
                    }
                
                # rt_cd 누락 처리 (현재 분봉 데이터가 특히 이런 문제가 있음)
                if "rt_cd" not in result:
                    logger.log_system(f"API 응답에 rt_cd 필드가 없습니다. 응답 키: {list(result.keys())}")
                    
                    # 성공 응답 여부 확인 (일부 API는 다른 필드로 성공 여부를 나타냄)
                    success_indicator = False
                    
                    # 분봉 데이터 요청의 경우 output1과 output2 필드가 있으면 성공으로 간주
                    if is_minute_chart_request and "output1" in result and "output2" in result:
                        success_indicator = True
                        result["rt_cd"] = "0"  # 성공 코드 설정
                        logger.log_system("분봉 데이터 응답 구조 확인: output1/output2 필드가 존재하여 성공으로 처리")
                    # 현재가 조회의 경우 output 필드가 있고 그 안에 필수 필드가 있으면 성공으로 간주
                    elif "output" in result and isinstance(result["output"], dict):
                        if "stck_prpr" in result["output"]:  # 현재가 필드
                            success_indicator = True
                            result["rt_cd"] = "0"  # 성공 코드 설정
                            logger.log_system("현재가 응답 구조 확인: output 필드 내 stck_prpr 필드가 존재하여 성공으로 처리")
                    
                    if not success_indicator:
                        logger.log_system(f"API 응답에 성공 여부를 판단할 필드가 없습니다. 응답: {json.dumps(result, indent=2, ensure_ascii=False)[:500]}...")
                        result["rt_cd"] = "9995"
                        result["msg1"] = "API 응답 구조 비정상 (rt_cd 필드 없음)"
                        result["error_type"] = "invalid_response_structure"
                
                # API 응답 코드 체크
                if result.get("rt_cd") != "0":
                    # rt_cd 값이 없거나 '0'이 아닌 경우 에러 메시지 처리
                    error_msg = result.get("msg1", "Unknown error")
                    if not error_msg or error_msg.strip() == "":
                        error_msg = "Unknown error (Empty error message)"
                    
                    # 응답 데이터 추가 로깅
                    logger.log_system(f"API 응답 디버깅: {json.dumps(result, indent=2, ensure_ascii=False)[:1000]}...")
                    
                    # 토큰 관련 에러 키워드
                    token_error_keywords = ["token", "auth", "unauthorized", "인증", "토큰"]
                    
                    # 토큰 관련 에러인 경우 토큰 갱신
                    if error_msg.lower() != "unknown error" and any(keyword in error_msg.lower() for keyword in token_error_keywords):
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
                    rt_cd_value = result.get("rt_cd", "알 수 없음")
                    logger.log_error(Exception(f"API error: {error_msg}"), f"API 응답 오류 (rt_cd: {rt_cd_value})")
                    
                    # 예외 발생 대신 오류 정보가 포함된 응답 반환
                    if raise_on_error:
                        raise Exception(f"API error: {error_msg}")
                    
                    # API 에러 정보를 포함한 응답 반환
                    result["error_type"] = "api_error"
                    return result
                
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
                    if raise_on_error:
                        raise
                    return {
                        "rt_cd": "9998",
                        "msg1": error_msg,
                        "error_type": "timeout"
                    }
                    
            except requests.exceptions.RequestException as e:
                if attempt < max_retries - 1:
                    wait_time = (attempt + 1) * 2  # 지수 백오프
                    logger.log_warning(f"요청 실패 ({type(e).__name__}): {str(e)[:200]}, {wait_time}초 후 재시도...")
                    time.sleep(wait_time)
                else:
                    logger.log_error(e, f"요청 실패 (최대 재시도 횟수 {max_retries}회 초과)")
                    if raise_on_error:
                        raise
                    return {
                        "rt_cd": "9997",
                        "msg1": f"요청 실패: {str(e)}",
                        "error_type": "request_exception",
                        "exception_type": type(e).__name__
                    }
            
        # 모든 재시도 실패
        error_msg = f"최대 재시도 횟수({max_retries}회) 초과"
        logger.log_error(Exception(error_msg), error_msg)
        if raise_on_error:
            raise Exception(error_msg)
        return {
            "rt_cd": "9996",
            "msg1": error_msg,
            "error_type": "max_retries_exceeded"
        }
    
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
        
        #logger.log_system(f"[현재가조회] {symbol} 현재가 조회 시도")
        # raise_on_error=False로 설정하여 예외를 발생시키지 않고 오류 정보 반환
        result = self._make_request("GET", path, headers=headers, params=params, raise_on_error=False)
        
        # 결과에 추가 정보 포함
        result["symbol"] = symbol
        result["query_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        if result.get("rt_cd") == "0":
            logger.log_system(f"[현재가조회성공] {symbol} 현재가 조회 성공")
        else:
            logger.log_system(f"[현재가조회실패] {symbol} 현재가 조회 실패: {result.get('msg1', '알 수 없는 오류')}")
        
        return result
    
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
        """계좌 잔고 조회
        
        Returns:
            Dict[str, Any]: 계좌 잔고 정보를 담은 딕셔너리
                - output1 (list): 보유 종목 목록 (데이터 타입 변환 처리됨)
                - output2 (list): 계좌 요약 정보 (예수금 총액, 평가금액 등) (데이터 타입 변환 처리됨)
        """
        path = "/uapi/domestic-stock/v1/trading/inquire-balance"
        
        # 모의투자 여부 확인
        is_dev = self._is_virtual_trade()
        
        # 거래소코드 설정 (모의투자 또는 실전투자)
        tr_id = "VTTC8434R" if is_dev else "TTTC8434R"  # 모의투자(V) vs 실전투자(T)
        
        headers = {"tr_id": tr_id}
        
        params = {
            "CANO": self.account_no[:8],
            "ACNT_PRDT_CD": self.account_no[8:10],  
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
            self._ensure_token()
                    
            # API 요청 실행
            result = self._make_request("GET", path, headers=headers, params=params)
            
            # 응답 데이터 처리
            if result and result.get("rt_cd") == "0":
                logger.log_system(f"계좌 정보 조회 성공: {result.get('msg1', '정상')}")
                
                # 응답 데이터 구조 검증 및 표준화
                standardized_result = self._standardize_balance_result(result)
                
                # 데이터 타입 변환 - 문자열 -> 숫자
                standardized_result = self._convert_balance_data_types(standardized_result)
                
                return standardized_result
            else:
                error_msg = result.get("msg1", "알 수 없는 오류")
                logger.log_system(f"계좌 정보 조회 실패: {error_msg}", level="ERROR")
                
                # 오류 응답에도 표준 구조 제공
                return self._create_default_balance_result(error_msg=error_msg)
        except Exception as e:
            logger.log_error(e, "계좌 정보 조회 중 예외 발생")
            return self._create_default_balance_result(error_msg=str(e))
            
    def _is_virtual_trade(self) -> bool:
        """모의투자 모드 여부 확인"""
        try:
            test_mode_str = os.getenv("TEST_MODE", "False").strip()
            is_dev = test_mode_str.lower() in ['true', '1', 't', 'y', 'yes']
            logger.log_debug(f"모의투자 모드: {is_dev} (환경 변수 TEST_MODE: '{test_mode_str}')")
            return is_dev
        except Exception as e:
            logger.log_error(e, "TEST_MODE 환경 변수 확인 중 오류")
            return False

    def _ensure_token(self):
        """토큰 유효성 확인 및 필요시 갱신"""
        if not self.access_token or not self.token_expire_time:
            logger.log_system("계좌 정보 조회 전 토큰 발급이 필요합니다.")
            self._get_access_token()

    def _standardize_balance_result(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """응답 결과 표준화"""
        # output1과 output2가 모두 있는지 확인
        if "output1" not in result:
            result["output1"] = []
        elif not isinstance(result["output1"], list):
            result["output1"] = [result["output1"]]
            
        if "output2" not in result:
            # output2 구조 생성 (보유종목이 있으면 첫 항목의 계좌정보 활용)
            if result["output1"] and isinstance(result["output1"][0], dict):
                result["output2"] = [{
                    "dnca_tot_amt": result["output1"][0].get("dnca_tot_amt", "0"),  # 예수금 총액
                    "tot_evlu_amt": result["output1"][0].get("tot_evlu_amt", "0"),  # 총 평가금액
                    "scts_evlu_amt": result["output1"][0].get("scts_evlu_amt", "0"),  # 유가증권 평가금액
                    "nass_amt": result["output1"][0].get("nass_amt", "0")  # 순자산금액
                }]
            else:
                # 빈 output2 생성
                result["output2"] = [{
                    "dnca_tot_amt": "0",  # 예수금 총액
                    "tot_evlu_amt": "0",  # 총 평가금액
                    "scts_evlu_amt": "0",  # 유가증권 평가금액
                    "nass_amt": "0"  # 순자산금액
                }]
        elif not isinstance(result["output2"], list):
            result["output2"] = [result["output2"]]
        
        return result

    def _convert_balance_data_types(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """잔고 데이터 타입 변환 (문자열 -> 숫자)"""
        # output1 변환 (보유 주식 목록)
        for item in result.get("output1", []):
            if isinstance(item, dict):
                # 숫자 필드 변환
                for numeric_field in ["hldg_qty", "ord_psbl_qty", "pchs_avg_pric", "evlu_pfls_rt", 
                                    "evlu_pfls_amt", "evlu_amt", "pchs_amt"]:
                    if numeric_field in item:
                        try:
                            item[numeric_field] = float(item[numeric_field])
                            # 정수로 표현 가능한 경우 정수 변환
                            if item[numeric_field].is_integer():
                                item[numeric_field] = int(item[numeric_field])
                        except (ValueError, TypeError):
                            # 변환 실패 시 원래 값 유지
                            pass
        
        # output2 변환 (계좌 요약 정보)
        for item in result.get("output2", []):
            if isinstance(item, dict):
                # 숫자 필드 변환
                for numeric_field in ["dnca_tot_amt", "tot_evlu_amt", "scts_evlu_amt", "nass_amt"]:
                    if numeric_field in item:
                        try:
                            item[numeric_field] = float(item[numeric_field])
                            # 정수로 표현 가능한 경우 정수 변환
                            if item[numeric_field].is_integer():
                                item[numeric_field] = int(item[numeric_field])
                        except (ValueError, TypeError):
                            # 변환 실패 시 원래 값 유지
                            pass
        
        return result

    def _create_default_balance_result(self, error_msg: str = "알 수 없는 오류") -> Dict[str, Any]:
        """기본 잔고 결과 구조 생성 (오류 발생 시)"""
        return {
            "rt_cd": "9999", 
            "msg1": error_msg, 
            "output1": [],
            "output2": [{
                "dnca_tot_amt": 0,  # 예수금 총액
                "tot_evlu_amt": 0,  # 총 평가금액
                "scts_evlu_amt": 0,  # 유가증권 평가금액
                "nass_amt": 0  # 순자산금액
            }]
        }

    # 비동기 구현 개선 버전
    async def get_account_balance_async(self) -> Dict[str, Any]:
        """계좌 잔고 조회 (비동기 버전)"""
        try:
            # 비동기 토큰 확보
            token = await self._get_access_token_async()
            
            # 현재 스레드에 이벤트 루프가 없는 경우 새로 생성
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                # 현재 스레드에 이벤트 루프가 없는 경우 새로 생성
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            
            # 동기 함수를 비동기적으로 실행
            result = await loop.run_in_executor(None, self.get_account_balance)
            
            # 결과 반환
            return result
        except Exception as e:
            logger.log_error(e, "비동기 계좌 정보 조회 중 오류 발생")
            return self._create_default_balance_result(error_msg=str(e))
    
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
            # 현재 스레드에 이벤트 루프가 없는 경우 새로 생성
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                # 현재 스레드에 이벤트 루프가 없는 경우 새로 생성
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            
            # 동기 함수를 비동기적으로 실행
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
        # 주문 수량 검증 (추가)
        if quantity <= 0:
            logger.log_system(f"[주문거부] {symbol} {side} 주문 거부 - 수량이 0 이하입니다: {quantity}주")
            return {
                "rt_cd": "9999",
                "msg1": "주문 수량이 0 이하입니다",
                "output": {}
            }
        
        # 최대 주문 수량 제한 (한국투자증권 기준 100,000주로 설정)
        # 주문 수량이 너무 클 경우 API에서 거부되므로 사전에 체크
        MAX_ORDER_QUANTITY = 10000  # 기본 최대 주문 수량을 10만주에서 1만주로 보수적 설정
        
        if quantity > MAX_ORDER_QUANTITY:
            logger.log_system(f"[주문거부] {symbol} {side} 주문 거부 - 최대 주문 수량({MAX_ORDER_QUANTITY:,}주)을 초과: {quantity:,}주")
            return {
                "rt_cd": "9999",
                "msg1": f"최대 주문 수량({MAX_ORDER_QUANTITY:,}주)을 초과했습니다",
                "output": {}
            }
        
        # 주문 수량 검증 - 추가 조건
        # 한국 시장에서는 일부 종목이 10주, 100주 단위로 거래될 수 있음
        try:
            # 최소 주문 단위 확인 (기본값 1주)
            min_order_unit = 1
            # 주문 단위 확인 (기본값 1주)
            order_unit = 1
            
            # 종목 정보 조회를 통해 주문 단위 확인 가능하면 조회
            stock_info = self.get_stock_info(symbol)
            if stock_info.get("rt_cd") == "0" and "output" in stock_info:
                # 종목별 주문 단위 정보가 있으면 적용 (API에 따라 필드명이 다를 수 있음)
                # 필드명 예시: 'unit_trade_qty', 'ord_unit_qty', 'mktd_ord_unpr_unit' 등
                for field in ["unit_trade_qty", "ord_unit_qty", "mktd_ord_unpr_unit"]:
                    if field in stock_info["output"] and stock_info["output"][field]:
                        try:
                            order_unit = int(stock_info["output"][field])
                            if order_unit > 0:
                                break
                        except (ValueError, TypeError):
                            pass
            
            # 주문 단위로 조정
            if order_unit > 1 and quantity % order_unit != 0:
                adjusted_quantity = (quantity // order_unit) * order_unit
                logger.log_system(f"[주문수량조정] {symbol} {side} 주문 - 주문 단위({order_unit}주) 조정: {quantity}주 → {adjusted_quantity}주")
                
                if adjusted_quantity <= 0:
                    logger.log_system(f"[주문거부] {symbol} {side} 주문 거부 - 주문 단위 조정 후 수량이 0입니다.")
                    return {
                        "rt_cd": "9999",
                        "msg1": "주문 단위 조정 후 수량이 0입니다",
                        "output": {}
                    }
                
                quantity = adjusted_quantity
        
        except Exception as unit_e:
            # 주문 단위 검증 실패 시 로그만 남기고 계속 진행
            logger.log_warning(f"주문 단위 검증 중 오류 발생: {str(unit_e)}")
        
        # 매수일 경우 계좌 잔고 확인 (최대 구매 가능 수량 검증)
        if side.upper() == "BUY" and price > 0:
            try:
                # 계좌 잔고 조회 - 주문 직전 최신 정보로 강제 갱신
                balance_data = self.get_account_balance()
                
                # 주문가능금액 초기화
                available_cash = 0
                ord_psbl_cash = 0  # 실제 주문가능금액
                
                # 다양한 형식의 응답 처리
                if isinstance(balance_data, dict):
                    # 주문가능금액(ord_psbl_cash) 필드 찾기 시도 - output2에서 우선 확인
                    if "output2" in balance_data and balance_data["output2"]:
                        output2 = balance_data["output2"]
                        if isinstance(output2, list) and output2:
                            item = output2[0]
                            # 주문가능금액 필드 확인
                            if "ord_psbl_cash" in item:
                                ord_psbl_cash = float(item.get("ord_psbl_cash", "0"))
                                logger.log_system(f"[주문검증] {symbol} {side} - API 주문가능금액: {ord_psbl_cash:,.0f}원")
                            # 예수금 필드 확인
                            if "dnca_tot_amt" in item:
                                available_cash = float(item.get("dnca_tot_amt", "0"))
                        elif isinstance(output2, dict):
                            # 주문가능금액 필드 확인
                            if "ord_psbl_cash" in output2:
                                ord_psbl_cash = float(output2.get("ord_psbl_cash", "0"))
                                logger.log_system(f"[주문검증] {symbol} {side} - API 주문가능금액: {ord_psbl_cash:,.0f}원")
                            # 예수금 필드 확인
                            if "dnca_tot_amt" in output2:
                                available_cash = float(output2.get("dnca_tot_amt", "0"))
                    
                    # output1에서도 확인
                    if "output1" in balance_data:
                        output1 = balance_data["output1"]
                        if isinstance(output1, list) and output1:
                            for item in output1:
                                if isinstance(item, dict):
                                    # 주문가능금액 필드 확인
                                    if "ord_psbl_cash" in item and ord_psbl_cash == 0:
                                        ord_psbl_cash = float(item.get("ord_psbl_cash", "0"))
                                        logger.log_system(f"[주문검증] {symbol} {side} - output1 주문가능금액: {ord_psbl_cash:,.0f}원")
                                    # 예수금 필드 확인
                                    if "dnca_tot_amt" in item and available_cash == 0:
                                        available_cash = float(item.get("dnca_tot_amt", "0"))
                        elif isinstance(output1, dict):
                            # 주문가능금액 필드 확인
                            if "ord_psbl_cash" in output1 and ord_psbl_cash == 0:
                                ord_psbl_cash = float(output1.get("ord_psbl_cash", "0"))
                                logger.log_system(f"[주문검증] {symbol} {side} - output1 주문가능금액: {ord_psbl_cash:,.0f}원")
                            # 예수금 필드 확인
                            if "dnca_tot_amt" in output1 and available_cash == 0:
                                available_cash = float(output1.get("dnca_tot_amt", "0"))
                
                # 최종 주문가능금액 결정 (API가 제공하는 주문가능금액 우선 사용)
                final_available_cash = ord_psbl_cash if ord_psbl_cash > 0 else available_cash * 0.98
                
                # 주문 금액 계산 (수수료 고려)
                order_amount = price * quantity * 1.005  # 0.5% 수수료 고려
                
                # 최대 주문 금액 제한 (100만원)
                MAX_ORDER_VALUE = 1000000  # 100만원
                if order_amount > MAX_ORDER_VALUE:
                    max_units_by_value = int(MAX_ORDER_VALUE / price)
                    max_units_by_value = (max_units_by_value // order_unit) * order_unit if order_unit > 1 else max_units_by_value
                    
                    if max_units_by_value <= 0:
                        logger.log_system(f"[주문거부] {symbol} {side} - 최대 주문 금액({MAX_ORDER_VALUE:,.0f}원) 제한으로 주문 불가: {order_amount:,.0f}원")
                        return {
                            "rt_cd": "9999",
                            "msg1": f"최대 주문 금액({MAX_ORDER_VALUE:,.0f}원)을 초과했습니다",
                            "output": {}
                        }
                    
                    logger.log_system(f"[주문수량조정] {symbol} {side} - 최대 주문 금액 제한으로 수량 조정: {quantity}주({order_amount:,.0f}원) → {max_units_by_value}주({max_units_by_value*price:,.0f}원)")
                    quantity = max_units_by_value
                    order_amount = price * quantity * 1.005
                
                # 주문가능금액 검증
                if order_amount > final_available_cash:
                    # 주문가능금액 내에서 최대 수량 계산
                    max_quantity = int((final_available_cash / price) / 1.005)
                    max_quantity = (max_quantity // order_unit) * order_unit if order_unit > 1 else max_quantity
                    
                    if max_quantity <= 0:
                        logger.log_system(f"[주문거부] {symbol} {side} - 주문가능금액 부족: 필요={order_amount:,.0f}원, 가능={final_available_cash:,.0f}원")
                        return {
                            "rt_cd": "9999",
                            "msg1": "주문가능금액이 부족합니다",
                            "output": {}
                        }
                    
                    # 수량 자동 조정
                    logger.log_system(f"[주문수량조정] {symbol} {side} - 주문가능금액 기준 수량 조정: {quantity}주 → {max_quantity}주")
                    quantity = max_quantity
                    order_amount = price * quantity * 1.005
                
                logger.log_system(f"[주문검증] {symbol} {side} - 주문 검증 성공: 주문금액={order_amount:,.0f}원, 주문가능금액={final_available_cash:,.0f}원")
            
            except Exception as balance_e:
                # 계좌 잔고 조회 실패 시 경고만 로깅하고 진행
                logger.log_warning(f"계좌 잔고 확인 중 오류 발생: {str(balance_e)}")
        
        # 매도인 경우 추가 검증 (보유 수량 확인)
        elif side.upper() == "SELL":
            try:
                # 보유 종목 조회
                holdings = self.get_account_balance()
                positions = []
                
                # 응답 형식에 따른 보유 종목 정보 추출
                if isinstance(holdings, dict) and "output1" in holdings:
                    positions = holdings.get("output1", [])
                elif isinstance(holdings, list):
                    positions = holdings
                
                # 보유 수량 확인
                available_quantity = 0
                for position in positions:
                    if isinstance(position, dict) and position.get("pdno") == symbol:
                        available_quantity = int(position.get("hldg_qty", "0"))
                        break
                
                if available_quantity <= 0:
                    logger.log_system(f"[주문거부] {symbol} {side} - 보유 수량이 없습니다")
                    return {
                        "rt_cd": "9999",
                        "msg1": "보유 수량이 없습니다",
                        "output": {}
                    }
                
                if quantity > available_quantity:
                    logger.log_system(f"[주문거부] {symbol} {side} - 보유 수량({available_quantity}주)을 초과하는 주문입니다: {quantity}주")
                    return {
                        "rt_cd": "9999",
                        "msg1": "보유 수량을 초과했습니다",
                        "output": {}
                    }
                
            except Exception as position_e:
                # 보유 종목 조회 실패 시 경고만 로깅
                logger.log_warning(f"보유 종목 확인 중 오류 발생: {str(position_e)}")
        
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
        # https://apiportal.koreainvestment.com/apiservice-apiservice?/uapi/domestic-stock/v1/trading/order-cash
        if side.upper() == "BUY":
            tr_id = "TTTC0012U"
        else:
            tr_id = "TTTC0011U"
        
        # 주문 유형 (00: 지정가, 01: 시장가)
        ord_dvsn = "01" if order_type.upper() == "MARKET" else "00"
        
        # 요청 데이터 준비 - 모든 KEY는 대문자로 작성
        data = {
            "CANO": self.account_no[:8],
            "ACNT_PRDT_CD": self.account_no[8:10],  # 정확히 2자리만 가져오기
            "PDNO": symbol,
            "ORD_DVSN": ord_dvsn,
            "ORD_QTY": str(quantity),  # 숫자를 문자열로 변환
            "ORD_UNPR": str(price) if ord_dvsn == "00" else "0",  # 숫자를 문자열로 변환
            "CTAC_TLNO": "", # 연락전화번호(널값 가능)
            "SLL_TYPE": "01", # 매도유형(01: 일반매도, 02: 원의매매, 05: 대차매도)
            "ALGO_NO": ""     # 알고리즘 주문번호(선택값)
        }
        
        # 헤더 설정
        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self.access_token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id,
            "custtype": "P"  # 개인
        }
        
        # 해시키 생성 및 헤더에 추가
        hashkey = self._get_hashkey(data)
        headers["hashkey"] = hashkey
        
        # API 요청 실행 - data를 JSON 문자열로 변환
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
        
        # 모의투자 여부 확인
        is_dev = False
        try:
            # 테스트 모드 확인 (TEST_MODE=True이면 모의투자, 아니면 실전투자)
            test_mode_str = os.getenv("TEST_MODE", "False").strip()
            is_dev = test_mode_str.lower() in ['true', '1', 't', 'y', 'yes']
            logger.log_system(f"주문 취소 - 모의투자 모드: {is_dev} (환경 변수 TEST_MODE: '{test_mode_str}')")
        except Exception as e:
            logger.log_error(e, "TEST_MODE 환경 변수 확인 중 오류")
        
        # TR ID 설정 (모의투자:VTTC0803U / 실거래:TTTC0803U)
        tr_id = "TTTC0803U" if not is_dev else "VTTC0803U"
        
        # 요청 데이터 준비 - 모든 KEY는 대문자로 작성
        data = {
            "CANO": self.account_no[:8],
            "ACNT_PRDT_CD": self.account_no[8:10],  # 정확히 2자리만 가져오기
            "KRX_FWDG_ORD_ORGNO": "",  # 주문 시 받은 한국거래소전송주문조직번호
            "ORGN_ODNO": order_id,  # 원주문번호
            "ORD_DVSN": "00",  # 주문구분
            "RVSE_CNCL_DVSN_CD": "02",  # 취소
            "ORD_QTY": "0",  # 전량 취소
            "ORD_UNPR": "0",
            "QTY_ALL_ORD_YN": "Y"  # 전량주문여부
        }
        
        # 헤더 설정
        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self.access_token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id,
            "custtype": "P"  # 개인
        }
        
        # 해시키 생성 및 헤더에 추가
        hashkey = self._get_hashkey(data)
        headers["hashkey"] = hashkey
        
        # API 요청 실행
        result = self._make_request("POST", path, headers=headers, data=data)
        
        # 주문 취소 결과 로깅
        if result.get("rt_cd") == "0":
            logger.log_system(f"[💰 주문취소성공] 주문ID: {order_id} 취소 성공!")
            logger.log_trade(
                action="CANCEL_ORDER", 
                symbol=symbol,
                quantity=quantity,
                order_id=order_id,
                status="SUCCESS",
                reason="API 주문 취소 성공"
            )
        else:
            error_msg = result.get("msg1", "Unknown error")
            logger.log_system(f"[🚸 주문취소실패] 주문ID: {order_id} 취소 실패 - 오류: {error_msg}")
            logger.log_error(
                Exception(f"Order cancellation failed: {error_msg}"),
                f"Cancel order {order_id}"
            )
            logger.log_trade(
                action="CANCEL_ORDER_FAILED",
                symbol=symbol,
                quantity=quantity,
                order_id=order_id,
                reason=f"API 주문 취소 실패: {error_msg}",
                status="FAILED"
            )
        
        return result
    
    def get_order_history(self, start_date: str = None, end_date: str = None) -> Dict[str, Any]:
        """주문 내역 조회"""
        if not start_date:
            start_date = datetime.now().strftime("%Y%m%d")
        if not end_date:
            end_date = datetime.now().strftime("%Y%m%d")
            
        path = "/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
        
        # 모의투자 여부 확인
        is_dev = False
        try:
            # 테스트 모드 확인 (TEST_MODE=True이면 모의투자, 아니면 실전투자)
            test_mode_str = os.getenv("TEST_MODE", "False").strip()
            is_dev = test_mode_str.lower() in ['true', '1', 't', 'y', 'yes']
            logger.log_system(f"주문 내역 조회 - 모의투자 모드: {is_dev} (환경 변수 TEST_MODE: '{test_mode_str}')")
        except Exception as e:
            logger.log_error(e, "TEST_MODE 환경 변수 확인 중 오류")
        
        # TR ID 설정 (모의투자:VTTC8001R / 실거래:TTTC8001R)
        tr_id = "TTTC8001R" if not is_dev else "VTTC8001R"
        
        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self.access_token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id,
            "custtype": "P"  # 개인
        }
        
        params = {
            "CANO": self.account_no[:8],
            "ACNT_PRDT_CD": self.account_no[8:10],  # 정확히 2자리만 가져오기
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
        
        try:
            # API 요청 실행
            result = self._make_request("GET", path, headers=headers, params=params)
            
            if result and result.get("rt_cd") == "0":
                logger.log_system(f"주문 내역 조회 성공: {start_date}~{end_date}")
            else:
                error_msg = result.get("msg1", "알 수 없는 오류")
                logger.log_system(f"주문 내역 조회 실패: {error_msg}")
            
            return result
        except Exception as e:
            logger.log_error(e, "주문 내역 조회 중 오류 발생")
            return {
                "rt_cd": "9999", 
                "msg1": f"주문 내역 조회 실패: {str(e)}", 
                "output": [],
                "ctx_area_fk100": "",
                "ctx_area_nk100": ""
            }
    
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
        """
        분봉 차트 데이터 조회 (한국투자증권 API)
        Args:
            symbol: 종목코드
            time_unit: 분봉 단위 (1, 3, 5, 10, 15, 30, 60)
        Returns:
            API 응답 데이터
        """
        path = "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"
        headers = {
            "tr_id": "FHKST03010200",
            "custtype": "P",
            "content-type": "application/json"
        }
        
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",  # 시장구분코드 J:주식, ETF, ETN
            "FID_INPUT_ISCD": symbol,        # 종목코드
            "FID_INPUT_HOUR_1": time_unit,   # 입력 시간1 (분봉단위)
            "FID_PW_DATA_INCU_YN": "Y",     # 과거데이터 포함여부 (Y:포함, N:미포함)
            "FID_ETC_CLS_CODE": ""          # 기타 구분 코드 (필수 파라미터)
        }
        
        try:
            # 모든 설정 정보 로깅
            # logger.log_system(f"[분봉조회설정] 기본URL: {self.base_url}, 앱키 존재: {bool(self.app_key)}, 계좌번호 존재: {bool(self.account_no)}")
            # logger.log_system(f"[분봉조회설정] 전체 URL: {self.base_url}{path}")
            
            # 요청 전에 현재 인증 상태 확인
            if hasattr(self, 'access_token'):
                logger.log_system(f"현재 액세스 토큰: {self.access_token[:10]}... (토큰 만료: {datetime.fromtimestamp(self.token_expire_time).strftime('%Y-%m-%d %H:%M:%S') if self.token_expire_time else 'None'})")
            
            # 종목 정보 로그
            logger.log_system(f"[분봉조회] {symbol} {time_unit}분봉 데이터 조회 시도")
            
            # API 요청 전에 토큰 확인
            if not self.access_token or not self.token_expire_time:
                logger.log_system("[분봉조회] 토큰이 없어 새로 발급합니다")
                self._get_access_token()
            
            # 요청 매개변수 로깅
            # logger.log_system(f"[분봉조회] 요청 헤더: {headers}")
            # logger.log_system(f"[분봉조회] 요청 파라미터: {params}")
            
            # API 요청 실행 - raise_on_error=False로 설정하여 예외 대신 오류 정보 반환
            result = self._make_request("GET", path, headers=headers, params=params, raise_on_error=False)
            
            # API 오류 확인 및 처리
            if result.get("rt_cd") != "0":
                logger.log_system(f"[분봉조회실패] {symbol} {time_unit}분봉 데이터 조회 실패. 오류: {result.get('msg1', '알 수 없음')}, 오류 유형: {result.get('error_type', '알 수 없음')}")
                
                # 결과에 종목 및 시간 정보 추가
                result["symbol"] = symbol
                result["time_unit"] = time_unit
                result["query_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                return result
            
            # 응답 로깅 (디버깅용)
            if 'output2' in result and isinstance(result['output2'], list):
                #logger.log_system(f"[분봉조회] 분봉 데이터 건수: {len(result['output2'])}")
                # 첫 번째 데이터 항목 구조만 로깅
                if result['output2']:
                    logger.log_system(f"[분봉조회] 첫 번째 데이터 항목: {result['output2'][0] if len(result['output2']) > 0 else '없음'}")
            else:
                # output2가 없는 경우 전체 응답 구조 로깅
                logger.log_system(f"[분봉조회] 응답 구조: {list(result.keys())}")
            
            # 성공 로그 기록
            #chart_items = result.get("output2", [])
            #logger.log_system(f"[분봉조회성공] {symbol} {time_unit}분봉 데이터 조회 성공: {len(chart_items)}개 데이터")
            
            # 메타데이터 추가
            result["symbol"] = symbol
            result["time_unit"] = time_unit
            result["query_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            return result
            
        except Exception as e:
            # 예외 발생 시 디버깅 정보 추가
            logger.log_error(e, f"[분봉조회예외] {symbol} {time_unit}분봉 데이터 조회 중 예외 발생: {str(e)}, 유형: {type(e)}")
            
            # 상세 예외 정보 출력
            import traceback
            logger.log_error(e, f"[분봉조회예외] 상세 예외 정보: {traceback.format_exc()}")
            
            # 오류 발생 시 구조화된 응답 반환
            return {
                "rt_cd": "9999", 
                "msg1": str(e),
                "error_type": str(type(e)),
                "symbol": symbol,
                "time_unit": time_unit,
                "query_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "output1": {},
                "output2": []
            }
    
    def get_daily_price(self, symbol: str, period: str = "D", count: int = 30) -> Dict[str, Any]:
        """
        일봉 차트 데이터 조회 (한국투자증권 API)
        Args:
            symbol: 종목코드
            period: 일봉 구분 (D:일봉, W:주봉, M:월봉)
            count: 요청할 데이터 개수 (최대 100)
        Returns:
            API 응답 데이터
        """
        path = "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
        headers = {
            "tr_id": "FHKST03010100",
            "custtype": "P"
        }
        
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",  # 시장구분코드 J:주식, ETF, ETN
            "FID_INPUT_ISCD": symbol,        # 종목코드
            "FID_PERIOD_DIV_CODE": period,   # 기간분류코드 (D:일봉, W:주봉, M:월봉)
            "FID_ORG_ADJ_PRC": "0",          # 수정주가 원주가 가격 여부 (0:수정주가, 1:원주가)
            "FID_INPUT_DATE_1": "",          # 입력일자1 (YYYYMMDD)
            "FID_INPUT_DATE_2": "",          # 입력일자2 (YYYYMMDD)
            "FID_DAY_1": count               # 요청 데이터 개수 (최대 100)
        }
        
        try:
            result = self._make_request("GET", path, headers=headers, params=params)
            
            # 성공 로그 기록
            if result.get("rt_cd") == "0":
                output1 = result.get("output1", {})
                chart_items = result.get("output2", [])
                logger.log_system(f"{symbol} 일봉 데이터 조회 성공: {len(chart_items)}개 데이터")
            else:
                error_msg = result.get("msg1", "Unknown error")
                logger.log_error(Exception(error_msg), f"{symbol} 일봉 데이터 조회 실패: {error_msg}")
            
            return result
            
        except Exception as e:
            logger.log_error(e, f"{symbol} 일봉 데이터 조회 중 예외 발생")
            # 오류 발생 시 빈 응답 반환
            return {
                "rt_cd": "9999", 
                "msg1": str(e),
                "output1": {},
                "output2": []
            }


    def get_market_trading_volume(self, 
                                market_code: str = "J", 
                                screen_code: str = "20171", 
                                symbol: str = "0000",  # 0000(전체) 기타(업종코드)
                                div_cls_code: str = "0", # 0(전체) 1(보통주) 2(우선주)
                                blng_cls_code: str = "4", # 0 : 평균거래량 1:거래증가율 2:평균거래회전율 3:거래금액순 4:평균거래금액회전율
                                trgt_cls_code: str = "111111111",  # 1 or 0 9자리 (차례대로 증거금 30% 40% 50% 60% 100% 신용보증금 30% 40% 50% 60%) ex) "111111111"
                                trgt_exls_cls_code: str = "0000000000", 
                                input_price_1: str = "", 
                                input_price_2: str = "", 
                                vol_cnt: str = "", 
                                input_date_1: str = "") -> Dict[str, Any]:

        path = "/uapi/domestic-stock/v1/quotations/volume-rank"
        headers = {
            "tr_id": "FHPST01710000",
            "custtype": "P"
        }
        
        params = {
            "FID_COND_MRKT_DIV_CODE": market_code,      # 조건 시장 분류 코드
            "FID_COND_SCR_DIV_CODE": screen_code,       # 조건 화면 분류 코드
            "FID_INPUT_ISCD": symbol,                   # 입력 종목코드
            "FID_DIV_CLS_CODE": div_cls_code,           # 분류 구분 코드
            "FID_BLNG_CLS_CODE": blng_cls_code,         # 소속 구분 코드
            "FID_TRGT_CLS_CODE": trgt_cls_code,         # 대상 구분 코드
            "FID_TRGT_EXLS_CLS_CODE": trgt_exls_cls_code, # 대상 제외 구분 코드
            "FID_INPUT_PRICE_1": input_price_1,         # 입력 가격1
            "FID_INPUT_PRICE_2": input_price_2,         # 입력 가격2
            "FID_VOL_CNT": vol_cnt,                     # 거래량 수
            "FID_INPUT_DATE_1": input_date_1            # 입력 날짜1
        }
        
        # 재시도 횟수 증가 (기본 3회 대신 5회로 증가)
        api_max_retries = 5
        
        try:
            #logger.log_system(f"거래량 상위 조회 API 요청: market_code={market_code}, screen_code={screen_code}, top_n={vol_cnt}")
            result = self._make_request("GET", path, headers=headers, params=params, max_retries=api_max_retries)
            
            # API 응답 구조 로그
            #logger.log_system(f"거래량 상위 API 원본 응답: {json.dumps(result, indent=2, ensure_ascii=False)[:1000]}...")
            #logger.log_system(f"거래량 상위 API 응답 구조: 키={list(result.keys())}")
            
            # 성공 로그 기록
            if result.get("rt_cd") == "0":
                # API 문서에 따른 올바른 키로 접근
                output = result.get("output", [])
                if output:
                    logger.log_system(f"거래량 상위 종목 조회 성공: {len(output)}개 데이터")
                    # 첫 번째 항목 구조 로그
                    logger.log_system(f"첫 항목 구조: {output[0] if output else '{}'}")  
                else:
                    logger.log_system("거래량 상위 종목 조회: 데이터 없음")
                    # 출력 구조 로그
                    for key in result.keys():
                        logger.log_system(f"API 응답 {key}: {result[key]}")
            else:
                error_msg = result.get("msg1", "Unknown error")
                logger.log_error(Exception(error_msg), f"거래량 상위 종목 조회 실패: {error_msg}")
                # API 오류 응답 구조 기록
                logger.log_system(f"API 오류 응답: {result}")
            
            return result
            
        except Exception as e:
            logger.log_error(e, "거래량 상위 종목 조회 중 예외 발생")
            # 오류 발생 시 API 응답 형식에 맞는 빈 응답 반환
            logger.log_system(f"거래량 상위 조회 예외 발생: {str(e)}")
            return {
                "rt_cd": "9999", 
                "msg_cd": "9999",
                "msg1": str(e),
                "output": []  # 빈 배열로 초기화 (API 응답 형식에 맞춤)
            }

    async def get_symbol_info(self, symbol: str) -> Dict[str, Any]:
        """종목 정보 조회 (비동기 버전)"""
        # 기본 응답 생성 헬퍼 함수
        def create_default_response(error_suffix="", error_msg=""):
            return {
                "symbol": symbol,
                "name": f"{symbol} ({error_suffix})" if error_suffix else symbol,
                "current_price": 0,
                "open_price": 0,
                "high_price": 0,
                "low_price": 0,
                "prev_close": 0,
                "volume": 0,
                "change_rate": 0,
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "error": error_msg
            }
        
        # 안전하게 숫자 변환하는 헬퍼 함수
        def safe_convert(value, converter, default=0):
            try:
                return converter(value) if value else default
            except (ValueError, TypeError):
                return default
        
        try:
            # 디버깅 로그 추가
            logger.log_system(f"[API] 종목 정보 조회 시작: {symbol}")
            
            # 비동기로 토큰 확보
            await self._get_access_token_async()
            
            path = "/uapi/domestic-stock/v1/quotations/inquire-price"
            headers = {"tr_id": "FHKST01010100"}
            params = {
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": symbol
            }
            
            # 현재 스레드에 이벤트 루프가 없는 경우 새로 생성
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                # 현재 스레드에 이벤트 루프가 없는 경우 새로 생성
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            
            # 동기 함수를 비동기적으로 실행
            result = await loop.run_in_executor(
                None, 
                lambda: self._make_request("GET", path, headers=headers, params=params, max_retries=3)
            )
            
            # 응답 검증
            if result.get("rt_cd") != "0":
                error_msg = result.get("msg1", "알 수 없는 오류")
                logger.log_system(f"[API] {symbol} 종목 정보 조회 실패: {error_msg}")
                return create_default_response("조회실패", error_msg)
            
            # output 필드 확인
            if "output" not in result:
                logger.log_system(f"[API] {symbol} 응답에 'output' 필드가 없습니다. 응답 키: {list(result.keys())}")
                return create_default_response("형식오류", "API 응답에 'output' 필드가 없습니다")
            
            output = result["output"]
            
            try:
                # 데이터 안전하게 추출 및 변환
                response_data = {
                    "symbol": symbol,
                    "name": output.get("rprs_mrkt_kor_name", "Unknown"),
                    "current_price": safe_convert(output.get("stck_prpr"), float),
                    "open_price": safe_convert(output.get("stck_oprc"), float),
                    "high_price": safe_convert(output.get("stck_hgpr"), float),
                    "low_price": safe_convert(output.get("stck_lwpr"), float),
                    "prev_close": safe_convert(output.get("stck_sdpr"), float),
                    "volume": safe_convert(output.get("acml_vol"), int),
                    "change_rate": safe_convert(output.get("prdy_ctrt"), float),
                    "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
                
                logger.log_system(f"[API] {symbol} 종목 정보 조회 성공: 종목명 '{response_data['name']}', 현재가 {response_data['current_price']:,}원")
                return response_data
                
            except Exception as e:
                logger.log_error(e, f"[API] {symbol} 가격 데이터 변환 오류")
                # 오류 정보 자세히 기록
                for field_name, field_value in [
                    ("rprs_mrkt_kor_name", output.get("rprs_mrkt_kor_name")),
                    ("stck_prpr", output.get("stck_prpr")),
                    ("stck_oprc", output.get("stck_oprc")),
                    ("stck_hgpr", output.get("stck_hgpr")),
                    ("stck_lwpr", output.get("stck_lwpr")),
                    ("stck_sdpr", output.get("stck_sdpr")),
                    ("acml_vol", output.get("acml_vol")),
                    ("prdy_ctrt", output.get("prdy_ctrt"))
                ]:
                    logger.log_system(f"[API] {field_name}: {field_value} (타입: {type(field_value)})")
                
                return create_default_response("변환오류", f"데이터 변환 오류: {str(e)}")
        
        except requests.Timeout:
            logger.log_system(f"[API] {symbol} 종목 정보 조회 타임아웃 발생")
            return create_default_response("타임아웃", "API 요청 타임아웃")
        
        except requests.RequestException as e:
            logger.log_error(e, f"[API] {symbol} 종목 정보 조회 요청 오류")
            return create_default_response("요청오류", f"API 요청 오류: {str(e)}")
        
        except Exception as e:
            logger.log_error(e, f"[API] {symbol} 종목 정보 조회 중 오류 발생")
            return create_default_response("오류", f"예외 발생: {str(e)}")

    def get_trading_status(self, symbol: str) -> Dict[str, Any]:
        """종목의 거래 정지 여부 확인
        
        Args:
            symbol: 종목 코드
            
        Returns:
            Dict[str, Any]: API 응답 결과. 주요 필드:
                - rt_cd: 응답 코드 ("0": 정상)
                - output: 종목 거래 상태 정보
                    - status: 거래 상태 ("NORMAL": 정상, "SUSPENDED": 거래 정지)
        """
        try:
            # 토큰 확인
            self._ensure_token()
            
            # 종목 정보 API로 거래 정지 여부 확인
            stock_info = self.get_stock_info(symbol)
            
            # 응답 코드 확인
            if stock_info.get("rt_cd") != "0":
                # API 요청 자체가 실패한 경우
                return {
                    "rt_cd": stock_info.get("rt_cd", "9999"),
                    "msg1": stock_info.get("msg1", "종목 정보 조회 실패"),
                    "output": {"status": "UNKNOWN"}
                }
            
            # 종목 정보에서 거래 정지 여부 확인
            output = stock_info.get("output", {})
            
            # 거래 정지 관련 필드들 확인 (다양한 필드명으로 시도)
            is_suspended = False
            
            # 가능한 필드명들 시도
            suspend_fields = [
                "hts_kor_isnm", "prdt_status", "stck_sdpr", "temp_stop_yn", "trade_status", "is_trading_suspended"
            ]
            
            # 주식 상태 텍스트 확인 (필드에 "거래정지" 등의 텍스트가 있는지)
            for field in suspend_fields:
                if field in output:
                    field_value = str(output[field]).lower()
                    if any(kw in field_value for kw in ["정지", "suspend", "halt", "stop"]):
                        is_suspended = True
                        break
            
            # 현재가 데이터로 추가 확인
            try:
                price_data = self.get_current_price(symbol)
                if price_data.get("rt_cd") == "0":
                    price_output = price_data.get("output", {})
                    # 거래 정지 관련 추가 필드 확인
                    for field in ["tempStopYn", "tradeStopYn", "trdSuspStop"]:
                        if field in price_output:
                            field_value = str(price_output[field]).lower()
                            if field_value in ["y", "1", "true", "yes"]:
                                is_suspended = True
                                break
                else:
                    # 현재가 조회 자체가 실패하면 매도 위험 회피를 위해 정지 간주
                    logger.log_warning(f"{symbol} 현재가 조회 실패, 안전을 위해 거래 정지로 간주")
                    is_suspended = True
            except Exception as price_err:
                logger.log_error(price_err, f"{symbol} 현재가 조회 중 오류")
                # 조회 오류 시 안전을 위해 거래 정지로 간주
                is_suspended = True
            
            # 최종 결과 반환
            return {
                "rt_cd": "0",
                "msg1": "정상 처리되었습니다.",
                "output": {
                    "status": "SUSPENDED" if is_suspended else "NORMAL",
                    "symbol": symbol
                }
            }
            
        except Exception as e:
            logger.log_error(e, f"종목 {symbol} 거래 상태 확인 중 오류")
            # 오류 발생 시 안전을 위해 거래 정지로 간주
            return {
                "rt_cd": "9999",
                "msg1": str(e),
                "output": {"status": "SUSPENDED"}  # 안전을 위해 거래 정지로 취급
            }
    
    def get_trading_restrictions(self, symbol: str) -> Dict[str, Any]:
        """종목의 매도 제한 여부 확인
        
        Args:
            symbol: 종목 코드
            
        Returns:
            Dict[str, Any]: API 응답 결과. 주요 필드:
                - rt_cd: 응답 코드 ("0": 정상)
                - output: 종목 매도 제한 정보
                    - sell_restricted: 매도 제한 여부 (True/False)
                    - reason: 매도 제한 사유 (있는 경우)
        """
        try:
            # 토큰 확인
            self._ensure_token()
            
            # 종목 정보 API로 매도 제한 여부 확인
            stock_info = self.get_stock_info(symbol)
            
            # 응답 코드 확인
            if stock_info.get("rt_cd") != "0":
                # API 요청 자체가 실패한 경우
                return {
                    "rt_cd": stock_info.get("rt_cd", "9999"),
                    "msg1": stock_info.get("msg1", "종목 정보 조회 실패"),
                    "output": {"sell_restricted": True}  # 안전을 위해 매도 제한으로 간주
                }
            
            # 종목 정보에서 매도 제한 관련 정보 확인
            output = stock_info.get("output", {})
            
            # 매도 제한 관련 필드들 확인
            is_restricted = False
            restriction_reason = ""
            
            # 가능한 필드명들 시도
            restriction_fields = [
                "short_sell_restricted", "lmtd_ord_yn", "risk_mgmt_yn", "caution_yn", 
                "warning_yn", "sell_restriction", "is_sell_restricted"
            ]
            
            # 필드값 확인
            for field in restriction_fields:
                if field in output:
                    field_value = str(output[field]).lower()
                    if field_value in ["y", "1", "true", "yes"]:
                        is_restricted = True
                        restriction_reason = f"{field} 필드 값: {output[field]}"
                        break
            
            # 시장 정보로 추가 확인 (거래 이전/이후, 휴장 등)
            current_time = datetime.now().time()
            if current_time < time_class(8, 30) or current_time > time_class(16, 0):
                # 장 시간 외에는 매도 제한으로 간주할 수 있음
                # 단, 미국장과 같은 야간 거래 종목은 예외적으로 처리 필요
                pass
            
            # 결과 반환
            return {
                "rt_cd": "0",
                "msg1": "정상 처리되었습니다.",
                "output": {
                    "sell_restricted": is_restricted,
                    "reason": restriction_reason if is_restricted else "",
                    "symbol": symbol
                }
            }
            
        except Exception as e:
            logger.log_error(e, f"종목 {symbol} 매도 제한 확인 중 오류")
            # 오류 발생 시 안전을 위해 매도 제한으로 간주
            return {
                "rt_cd": "9999",
                "msg1": str(e),
                "output": {"sell_restricted": True}  # 안전을 위해 매도 제한으로 취급
            }
            
    async def get_symbol_info(self, symbol: str) -> Dict[str, Any]:
        """종목 정보 조회 (비동기 버전)"""
        # 기본 응답 생성 헬퍼 함수
        def create_default_response(error_suffix="", error_msg=""):
            return {
                "symbol": symbol,
                "name": f"{symbol} ({error_suffix})" if error_suffix else symbol,
                "current_price": 0,
                "open_price": 0,
                "high_price": 0,
                "low_price": 0,
                "prev_close": 0,
                "volume": 0,
                "change_rate": 0,
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "error": error_msg
            }
        
        # 안전하게 숫자 변환하는 헬퍼 함수
        def safe_convert(value, converter, default=0):
            try:
                return converter(value) if value else default
            except (ValueError, TypeError):
                return default
        
        try:
            # 디버깅 로그 추가
            logger.log_system(f"[API] 종목 정보 조회 시작: {symbol}")
            
            # 비동기로 토큰 확보
            await self._get_access_token_async()
            
            path = "/uapi/domestic-stock/v1/quotations/inquire-price"
            headers = {"tr_id": "FHKST01010100"}
            params = {
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": symbol
            }
            
            # 동기 함수를 비동기적으로 실행
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None, 
                lambda: self._make_request("GET", path, headers=headers, params=params, max_retries=3)
            )
            
            # 응답 검증
            if result.get("rt_cd") != "0":
                error_msg = result.get("msg1", "알 수 없는 오류")
                logger.log_system(f"[API] {symbol} 종목 정보 조회 실패: {error_msg}")
                return create_default_response("조회실패", error_msg)
            
            # output 필드 확인
            if "output" not in result:
                logger.log_system(f"[API] {symbol} 응답에 'output' 필드가 없습니다. 응답 키: {list(result.keys())}")
                return create_default_response("형식오류", "API 응답에 'output' 필드가 없습니다")
            
            output = result["output"]
            
            try:
                # 데이터 안전하게 추출 및 변환
                response_data = {
                    "symbol": symbol,
                    "name": output.get("rprs_mrkt_kor_name", "Unknown"),
                    "current_price": safe_convert(output.get("stck_prpr"), float),
                    "open_price": safe_convert(output.get("stck_oprc"), float),
                    "high_price": safe_convert(output.get("stck_hgpr"), float),
                    "low_price": safe_convert(output.get("stck_lwpr"), float),
                    "prev_close": safe_convert(output.get("stck_sdpr"), float),
                    "volume": safe_convert(output.get("acml_vol"), int),
                    "change_rate": safe_convert(output.get("prdy_ctrt"), float),
                    "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
                
                logger.log_system(f"[API] {symbol} 종목 정보 조회 성공: 종목명 '{response_data['name']}', 현재가 {response_data['current_price']:,}원")
                return response_data
                
            except Exception as e:
                logger.log_error(e, f"[API] {symbol} 가격 데이터 변환 오류")
                # 오류 정보 자세히 기록
                for field_name, field_value in [
                    ("rprs_mrkt_kor_name", output.get("rprs_mrkt_kor_name")),
                    ("stck_prpr", output.get("stck_prpr")),
                    ("stck_oprc", output.get("stck_oprc")),
                    ("stck_hgpr", output.get("stck_hgpr")),
                    ("stck_lwpr", output.get("stck_lwpr")),
                    ("stck_sdpr", output.get("stck_sdpr")),
                    ("acml_vol", output.get("acml_vol")),
                    ("prdy_ctrt", output.get("prdy_ctrt"))
                ]:
                    logger.log_system(f"[API] {field_name}: {field_value} (타입: {type(field_value)})")
                
                return create_default_response("변환오류", f"데이터 변환 오류: {str(e)}")
        
        except requests.Timeout:
            logger.log_system(f"[API] {symbol} 종목 정보 조회 타임아웃 발생")
            return create_default_response("타임아웃", "API 요청 타임아웃")
        
        except requests.RequestException as e:
            logger.log_error(e, f"[API] {symbol} 종목 정보 조회 요청 오류")
            return create_default_response("요청오류", f"API 요청 오류: {str(e)}")
        
        except Exception as e:
            logger.log_error(e, f"[API] {symbol} 종목 정보 조회 중 오류 발생")
            return create_default_response("오류", f"예외 발생: {str(e)}")



# 싱글톤 인스턴스
api_client = KISAPIClient()
