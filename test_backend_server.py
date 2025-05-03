"""
백엔드 서버의 텔레그램 메시지 전송 테스트
"""
import requests
import json
from datetime import datetime

# 백엔드 서버 URL
API_URL = "http://localhost:5050"

def test_backend_status():
    """백엔드 서버 상태 확인"""
    try:
        response = requests.get(f"{API_URL}/")
        if response.status_code == 200:
            print(f"✅ 백엔드 서버 응답 상태: {response.json()}")
            return True
        else:
            print(f"❌ 백엔드 서버 응답 오류: {response.status_code}")
            return False
    except Exception as e:
        print(f"❌ 백엔드 서버 연결 오류: {str(e)}")
        return False

def test_telegram_api():
    """텔레그램 메시지 API 테스트"""
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    message = f"""
*백엔드 텔레그램 API 테스트*
시간: {current_time}

이 메시지가 보이면 백엔드 서버를 통한 텔레그램 메시지 전송이 성공적으로 작동합니다.
"""
    
    try:
        response = requests.post(
            f"{API_URL}/api/send_telegram",
            json={"message": message},
            headers={"Content-Type": "application/json"}
        )
        
        if response.status_code == 200:
            print(f"✅ 텔레그램 메시지 전송 성공: {response.json()}")
            return True
        else:
            print(f"❌ 텔레그램 메시지 전송 실패: {response.status_code}, {response.text}")
            return False
    except Exception as e:
        print(f"❌ 텔레그램 API 호출 오류: {str(e)}")
        return False

def test_kis_api_message_flow():
    """KIS API 접속 시도 메시지 테스트"""
    # 1. KIS API 접속 시도 전 메시지
    pre_message = f"""
*KIS API 접속 시도 (백엔드 테스트)*
시도 시간: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

백엔드 서버에서 한국투자증권 API 서버에 접속을 시도합니다.
"""
    
    try:
        print("\n[1] KIS API 접속 시도 전 메시지 전송...")
        response = requests.post(
            f"{API_URL}/api/send_telegram",
            json={"message": pre_message},
            headers={"Content-Type": "application/json"}
        )
        
        if response.status_code == 200:
            print(f"✅ 접속 시도 메시지 전송 성공: {response.json()}")
        else:
            print(f"❌ 접속 시도 메시지 전송 실패: {response.status_code}, {response.text}")
            return False
            
        # 2. 계좌 정보 조회 (KIS API 접속 테스트)
        print("\n[2] 계좌 정보 조회 (KIS API 접속 테스트)...")
        account_response = requests.get(f"{API_URL}/api/account")
        
        if account_response.status_code == 200:
            account_data = account_response.json()
            is_success = account_data.get("rt_cd") == "0"
            
            # 3. 접속 결과 메시지 전송
            if is_success:
                print(f"✅ KIS API 접속 성공: {account_data.get('msg1', '성공')}")
                success_message = f"""
*KIS API 접속 성공 (백엔드 테스트)* ✅
접속 시간: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

백엔드 서버에서 한국투자증권 API 서버에 성공적으로 접속했습니다.
"""
                message_response = requests.post(
                    f"{API_URL}/api/send_telegram",
                    json={"message": success_message},
                    headers={"Content-Type": "application/json"}
                )
                
                if message_response.status_code == 200:
                    print(f"✅ 접속 성공 메시지 전송 성공: {message_response.json()}")
                    return True
                else:
                    print(f"❌ 접속 성공 메시지 전송 실패: {message_response.status_code}, {message_response.text}")
                    return False
            else:
                print(f"❌ KIS API 접속 실패: {account_data.get('msg1', '오류 발생')}")
                fail_message = f"""
*KIS API 접속 실패 (백엔드 테스트)* ❌
시도 시간: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

백엔드 서버에서 한국투자증권 API 서버 접속에 실패했습니다.
오류: {account_data.get('msg1', '알 수 없는 오류')}
"""
                message_response = requests.post(
                    f"{API_URL}/api/send_telegram",
                    json={"message": fail_message},
                    headers={"Content-Type": "application/json"}
                )
                
                if message_response.status_code == 200:
                    print(f"✅ 접속 실패 메시지 전송 성공: {message_response.json()}")
                    return False
                else:
                    print(f"❌ 접속 실패 메시지 전송 실패: {message_response.status_code}, {message_response.text}")
                    return False
        else:
            print(f"❌ 계좌 정보 조회 실패: {account_response.status_code}, {account_response.text}")
            fail_message = f"""
*KIS API 접속 실패 (백엔드 테스트)* ❌
시도 시간: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

백엔드 서버에서 한국투자증권 API 서버 접속에 실패했습니다.
오류: API 서버 응답 오류 ({account_response.status_code})
"""
            message_response = requests.post(
                f"{API_URL}/api/send_telegram",
                json={"message": fail_message},
                headers={"Content-Type": "application/json"}
            )
            
            if message_response.status_code == 200:
                print(f"✅ 접속 실패 메시지 전송 성공: {message_response.json()}")
                return False
            else:
                print(f"❌ 접속 실패 메시지 전송 실패: {message_response.status_code}, {message_response.text}")
                return False
    except Exception as e:
        print(f"❌ KIS API 메시지 흐름 테스트 오류: {str(e)}")
        return False

def main():
    """메인 함수"""
    print("=" * 50)
    print("백엔드 서버 텔레그램 메시지 전송 테스트")
    print("=" * 50)
    
    # 1. 백엔드 서버 상태 확인
    print("\n[1] 백엔드 서버 상태 확인")
    if not test_backend_status():
        print("\n❌ 백엔드 서버가 실행 중이지 않거나 응답하지 않습니다.")
        print("백엔드 서버를 시작한 후 다시 시도하세요.")
        return
    
    # 2. 텔레그램 메시지 API 테스트
    print("\n[2] 텔레그램 메시지 API 테스트")
    if not test_telegram_api():
        print("\n❌ 텔레그램 메시지 전송 테스트에 실패했습니다.")
        print("백엔드 서버의 텔레그램 핸들러 설정을 확인하세요.")
        return
    
    # 3. KIS API 접속 시도 메시지 흐름 테스트
    print("\n[3] KIS API 접속 시도 메시지 흐름 테스트")
    if test_kis_api_message_flow():
        print("\n✅ KIS API 접속 메시지 흐름 테스트가 성공적으로 완료되었습니다.")
        print("이제 start_full_dashboard.bat으로 프로그램을 시작할 때 텔레그램 메시지가 정상적으로 전송될 것입니다.")
    else:
        print("\n❌ KIS API 접속 메시지 흐름 테스트에 실패했습니다.")
        print("로그 확인 후 필요한 조치를 취하세요.")

if __name__ == "__main__":
    main() 