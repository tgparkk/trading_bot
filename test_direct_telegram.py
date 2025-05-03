import requests
import sys

def test_telegram_directly():
    """텔레그램 API 직접 테스트"""
    print("텔레그램 API 직접 테스트를 시작합니다...")
    
    # 직접 텔레그램 정보 입력
    print("\n실제 텔레그램 봇 토큰과 채팅 ID를 입력하세요.")
    token = input("텔레그램 봇 토큰 (예: 123456789:ABCdefGhIJKlmnOPQRstUVwxYZ123456789): ")
    chat_id = input("텔레그램 채팅 ID (예: 123456789): ")
    
    if not token or not chat_id:
        print("오류: 토큰과 채팅 ID가 필요합니다.")
        sys.exit(1)
    
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    
    message = "이 메시지가 보이면 텔레그램 설정이 올바르게 되어 있는 것입니다."
    
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown"
    }
    
    try:
        print("\n텔레그램 API 호출 중...")
        response = requests.post(url, json=payload)
        print(f"API 응답 상태 코드: {response.status_code}")
        
        if response.status_code == 200:
            print(f"성공: 메시지가 전송되었습니다. 응답: {response.json()}")
            print("\n텔레그램 앱에서 메시지를 확인하세요.")
            print("\n=== 다음 단계 안내 ===")
            print("1. 테스트가 성공했다면, .env 파일을 생성하여 다음 내용을 추가하세요:")
            print(f"TELEGRAM_TOKEN={token}")
            print(f"TELEGRAM_CHAT_ID={chat_id}")
            print("\n2. 그 후에 다음 명령으로 전체 기능 테스트를 실행할 수 있습니다:")
            print("python test_alert_system.py")
        else:
            print(f"오류: {response.text}")
            print("\n텔레그램 토큰이나 채팅 ID가 올바른지 확인하세요.")
            
    except Exception as e:
        print(f"오류 발생: {e}")
        print("인터넷 연결이나 텔레그램 설정이 올바른지 확인하세요.")

def get_chat_id_guide():
    """채팅 ID 얻는 방법 안내"""
    print("\n=== 텔레그램 채팅 ID 얻는 방법 ===")
    print("1. 텔레그램에서 @userinfobot을 찾아 시작하세요.")
    print("2. 봇이 자동으로 당신의 ID를 알려줍니다.")
    print("또는")
    print("1. 텔레그램에서 만든 봇에게 메시지를 보내세요.")
    print("2. 브라우저에서 다음 URL 열기: https://api.telegram.org/bot[YOUR_BOT_TOKEN]/getUpdates")
    print("3. 응답에서 'chat' 객체 안의 'id' 값이 채팅 ID입니다.")
    print("\n채팅 ID를 확인하시겠습니까? (y/n)")
    
    if input().lower() == 'y':
        bot_token = input("\n봇 토큰을 입력하세요: ")
        if bot_token:
            print(f"\n다음 URL을 브라우저에서 열어보세요:\nhttps://api.telegram.org/bot{bot_token}/getUpdates")
            print("봇과 대화를 먼저 시작한 후 URL을 열어야 채팅 ID가 표시됩니다.")

if __name__ == "__main__":
    print("===== 텔레그램 설정 테스트 도구 =====")
    print("1. 텔레그램 API 직접 테스트")
    print("2. 채팅 ID 얻는 방법 안내")
    print("3. 종료")
    
    choice = input("\n선택하세요 (1-3): ")
    
    if choice == '1':
        test_telegram_directly()
    elif choice == '2':
        get_chat_id_guide()
    else:
        print("프로그램을 종료합니다.") 