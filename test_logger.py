"""
로그 시스템 테스트 스크립트
"""
import os
import time
from datetime import datetime
from utils.logger import logger

def test_daily_logging():
    """일별 로깅 테스트"""
    print("="*60)
    print("날짜별 로그 시스템 테스트")
    print("="*60)
    
    # 현재 날짜
    today = datetime.now().strftime("%Y-%m-%d")
    print(f"현재 날짜: {today}")
    
    # 로그 디렉토리 확인
    log_dir = os.path.join("logs", today)
    if os.path.exists(log_dir):
        print(f"오늘의 로그 디렉토리 확인: {log_dir} (존재함)")
    else:
        print(f"오늘의 로그 디렉토리 확인: {log_dir} (생성 예정)")
    
    # 다양한 로그 메시지 기록
    print("\n1. 시스템 로그 기록 중...")
    logger.log_system("시스템 로그 테스트 - INFO 레벨")
    logger.log_system("시스템 경고 테스트", level="WARNING")
    logger.log_system("시스템 오류 테스트", level="ERROR")
    logger.log_system("시스템 디버그 테스트", level="DEBUG")
    
    print("\n2. 에러 로그 기록 중...")
    try:
        # 일부러 에러 발생
        result = 1 / 0
    except Exception as e:
        logger.log_error(e, "테스트 중 의도적인 오류 발생")
    
    print("\n3. 거래 로그 기록 중...")
    logger.log_trade("BUY", "005930", 70000, 10, reason="테스트 매수")
    logger.log_trade("SELL", "005930", 72000, 5, reason="테스트 매도")
    
    print("\n4. 성과 로그 기록 중...")
    logger.log_performance("일간 성과", 15000, 0.65, 10)
    
    # 로그 파일 확인
    print("\n로그 파일 목록 확인:")
    if os.path.exists(log_dir):
        log_files = os.listdir(log_dir)
        for file in log_files:
            file_path = os.path.join(log_dir, file)
            file_size = os.path.getsize(file_path)
            print(f" - {file} ({file_size} bytes)")
    else:
        print("로그 디렉토리가 아직 생성되지 않았습니다.")
    
    print("\n로그 파일 내용 미리보기:")
    if os.path.exists(log_dir):
        system_log_path = os.path.join(log_dir, "system.log")
        if os.path.exists(system_log_path):
            with open(system_log_path, "r") as f:
                print("\n시스템 로그 (처음 5줄):")
                for i, line in enumerate(f):
                    if i >= 5:
                        break
                    print(f"  {line.strip()}")
    
    print("\n테스트 완료!")
    print("="*60)

if __name__ == "__main__":
    test_daily_logging() 