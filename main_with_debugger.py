# 디버거 연결을 위한 main.py 변형
import debugpy

# 디버거 연결 대기
debugpy.listen(5678)
print("디버거 연결 대기 중... (포트: 5678)")
debugpy.wait_for_client()
print("디버거가 연결되었습니다!")

# 기존 main.py 코드를 여기에 포함
from main import main
import asyncio

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("프로그램 종료 (Ctrl+C)")
