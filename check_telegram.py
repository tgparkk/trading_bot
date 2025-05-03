import os
import sys
import requests
from dotenv import load_dotenv
from config.settings import config

# 환경 변수 로드
load_dotenv()

print("텔레그램 설정 확인 및 수정 도구")
print("-" * 50)

# 환경 변수에서 직접 확인
telegram_token = os.getenv("TELEGRAM_TOKEN")
telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")

print(f"1. 환경 변수에서 직접 읽은 값:")
print(f"   TELEGRAM_TOKEN = {telegram_token}")
print(f"   TELEGRAM_CHAT_ID = {telegram_chat_id}")
print()

# 설정 객체에서 확인
config_telegram_token = config["alert"].telegram_token
config_telegram_chat_id = config["alert"].telegram_chat_id

print(f"2. 설정 객체에서 읽은 값:")
print(f"   telegram_token = {config_telegram_token}")
print(f"   telegram_chat_id = {config_telegram_chat_id}")
print()

# 실제로 사용되는 값 확인
print(f"3. 실제 API URL:")
print(f"   https://api.telegram.org/bot{config_telegram_token}/sendMessage")
print()

# 테스트 메시지 전송
print("4. 테스트 메시지 전송 시도...")
try:
    url = f"https://api.telegram.org/bot{config_telegram_token}/sendMessage"
    data = {
        "chat_id": config_telegram_chat_id,
        "text": "테스트 메시지입니다. 이 메시지가 보이면 텔레그램 설정이 정상입니다.",
        "parse_mode": "Markdown"
    }
    response = requests.post(url, json=data)
    
    if response.status_code == 200 and response.json().get("ok"):
        print("   성공! 텔레그램 메시지가 전송되었습니다.")
        print(f"   Response: {response.json()}")
    else:
        print(f"   실패: {response.status_code}, {response.text}")
except Exception as e:
    print(f"   오류 발생: {str(e)}")
print()

# 값이 잘못된 경우 직접 수정 제안
if "your_telegram_bot_token" in config_telegram_token or not config_telegram_token:
    print("5. 텔레그램 토큰이 설정되지 않았거나 잘못된 형식입니다.")
    print("   .env 파일을 열어 TELEGRAM_TOKEN 값을 수정하거나, 다음 단계로 자동 수정을 진행하세요.")
    
    choice = input("   자동으로 .env 파일을 수정하시겠습니까? (y/n): ")
    if choice.lower() == 'y':
        new_token = input("   새 텔레그램 봇 토큰 입력: ")
        new_chat_id = input("   새 텔레그램 채팅 ID 입력: ")
        
        # .env 파일 백업
        try:
            with open('.env', 'r') as file:
                env_content = file.read()
            
            with open('.env.backup', 'w') as file:
                file.write(env_content)
                
            print("   .env 파일을 .env.backup으로 백업했습니다.")
            
            # 기존 내용에서 토큰과 채팅 ID 부분만 수정
            if "TELEGRAM_TOKEN" in env_content:
                env_content = env_content.replace(f'TELEGRAM_TOKEN="{telegram_token}"', f'TELEGRAM_TOKEN="{new_token}"')
                env_content = env_content.replace(f'TELEGRAM_CHAT_ID="{telegram_chat_id}"', f'TELEGRAM_CHAT_ID="{new_chat_id}"')
            else:
                # 기존에 없는 경우 추가
                env_content += f'\nTELEGRAM_TOKEN="{new_token}"\n'
                env_content += f'TELEGRAM_CHAT_ID="{new_chat_id}"\n'
            
            # 수정된 내용으로 파일 덮어쓰기
            with open('.env', 'w') as file:
                file.write(env_content)
                
            print("   .env 파일이 업데이트되었습니다. 프로그램을 다시 시작하세요.")
        except Exception as e:
            print(f"   .env 파일 수정 중 오류 발생: {str(e)}")
    else:
        print("   .env 파일 수정을 건너뛰었습니다.")
        print("   텔레그램 기능을 사용하려면 수동으로 .env 파일을 편집하고 프로그램을 다시 시작하세요.")
else:
    print("5. 텔레그램 설정이 유효해 보입니다.")
    print("   그럼에도 문제가 계속되면 다음을 확인하세요:")
    print("   - 봇 토큰이 유효한지")
    print("   - 채팅 ID가 올바른지")
    print("   - 네트워크 연결이 정상인지") 