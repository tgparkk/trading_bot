"""
.env 파일을 안전하게 로드하고 관리하는 유틸리티
"""
import os
from pathlib import Path
from dotenv import load_dotenv
from typing import Dict, Optional, List, Union

class DotEnvHelper:
    """환경 변수 관리 헬퍼 클래스 (싱글톤 패턴)"""
    
    _instance = None
    
    def __new__(cls, *args, **kwargs):
        """싱글톤 패턴 구현을 위한 __new__ 메서드 오버라이드"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        """초기화 메서드 (한 번만 실행됨)"""
        if getattr(self, '_initialized', False):
            return
            
        self._is_loaded = False
        self._env_path = Path(__file__).parents[1] / '.env'  # 프로젝트 루트의 .env 파일 경로
        self._initialized = True
    
    def load_env(self, force_reload: bool = False) -> bool:
        """환경 변수 로드"""
        if self._is_loaded and not force_reload:
            return True
        
        if not self._env_path.exists():
            print(f"WARNING: .env 파일이 {self._env_path}에 존재하지 않습니다.")
            return False
        
        # .env 파일 로드
        load_dotenv(dotenv_path=self._env_path)
        self._is_loaded = True
        
        # logger 대신 print 사용
        print(f"INFO: .env 파일을 로드했습니다: {self._env_path}")
        return True
    
    def get_value(self, key: str, default: str = None) -> Optional[str]:
        """환경 변수 값 가져오기"""
        self.load_env()  # 확실하게 로드되었는지 체크
        return os.getenv(key, default)
    
    def check_required_keys(self, required_keys: List[str]) -> List[str]:
        """필수 키 확인"""
        self.load_env()
        missing_keys = []
        
        for key in required_keys:
            if not os.getenv(key):
                missing_keys.append(key)
        
        return missing_keys
    
    def create_sample_env(self, template_path: str = None) -> bool:
        """샘플 .env 파일 생성"""
        if self._env_path.exists():
            print(f".env 파일이 이미 존재합니다: {self._env_path}")
            return False
        
        # 기본 템플릿 또는 제공된 템플릿 사용
        template = template_path or Path(__file__).parents[1] / '.env.example'
        
        if template_path and not Path(template_path).exists():
            print(f"템플릿 파일이 존재하지 않습니다: {template_path}")
            return False
        
        try:
            if Path(template).exists():
                # 템플릿 파일이 있으면 복사
                with open(template, 'r', encoding='utf-8') as src:
                    with open(self._env_path, 'w', encoding='utf-8') as dest:
                        dest.write(src.read())
            else:
                # 없으면 기본 템플릿 생성
                with open(self._env_path, 'w', encoding='utf-8') as f:
                    f.write("""# KIS API 설정
KIS_BASE_URL=https://openapi.koreainvestment.com:9443
KIS_APP_KEY=your_app_key
KIS_APP_SECRET=your_app_secret
KIS_ACCOUNT_NO=your_account_no
KIS_WS_URL=ws://ops.koreainvestment.com:21000

# 알림 설정
TELEGRAM_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_telegram_chat_id

# 이메일 설정
EMAIL_SENDER=your_email@gmail.com
EMAIL_PASSWORD=your_app_password
EMAIL_RECEIVER=your_email@gmail.com
""")
            
            print(f".env 파일이 생성되었습니다: {self._env_path}")
            return True
            
        except Exception as e:
            print(f".env 파일 생성 중 오류 발생: {e}")
            return False

# 싱글톤 인스턴스 접근 함수
def get_dotenv_helper() -> DotEnvHelper:
    """DotEnvHelper 싱글톤 인스턴스를 반환하는 함수"""
    return DotEnvHelper()

# 기본 싱글톤 인스턴스 (역호환성 유지)
dotenv_helper = get_dotenv_helper() 