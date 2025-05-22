"""
데이터베이스 스키마 업데이트 스크립트
"""
import sqlite3
import os
import sys
from pathlib import Path

# 필요한 디렉토리를 sys.path에 추가
current_dir = Path(__file__).parent.absolute()
sys.path.append(str(current_dir))

from utils.logger import logger
from utils.database import database_manager

def main():
    """메인 함수"""
    print("데이터베이스 스키마 업데이트 스크립트 시작...")
    
    try:
        # 데이터베이스 경로 가져오기
        db_path = database_manager.db_path
        print(f"데이터베이스 경로: {db_path}")
        
        if not os.path.exists(db_path):
            print(f"오류: 데이터베이스 파일이 존재하지 않습니다: {db_path}")
            return False
            
        # 스키마 업데이트 실행
        success = database_manager.update_database_schema()
        
        if success:
            print("데이터베이스 스키마 업데이트가 성공적으로 완료되었습니다.")
            return True
        else:
            print("데이터베이스 스키마 업데이트에 실패했습니다. 로그를 확인하세요.")
            return False
            
    except Exception as e:
        print(f"오류 발생: {str(e)}")
        return False

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1) 