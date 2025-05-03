import asyncio
from monitoring.alert_system import alert_system

async def test_alert():
    """텔레그램 알림 테스트"""
    print("텔레그램 알림 테스트를 시작합니다...")
    
    # 일반 정보 알림 테스트
    await alert_system.send_alert(
        "이것은 텔레그램 알림 테스트 메시지입니다.",
        level="INFO",
        channel="telegram"  # 텔레그램으로만 전송
    )
    print("INFO 알림 전송 완료")
    
    # 경고 알림 테스트
    await alert_system.send_alert(
        "이것은 텔레그램 경고 테스트 메시지입니다.",
        level="WARNING",
        channel="telegram"
    )
    print("WARNING 알림 전송 완료")
    
    # 에러 알림 테스트
    await alert_system.send_alert(
        "이것은 텔레그램 에러 테스트 메시지입니다.",
        level="ERROR",
        channel="telegram"
    )
    print("ERROR 알림 전송 완료")
    
    # 거래 알림 테스트
    test_trade_data = {
        "symbol": "TEST",
        "side": "BUY",
        "price": 50000,
        "quantity": 10,
        "strategy": "테스트 전략",
        "reason": "알림 테스트"
    }
    await alert_system.notify_trade(test_trade_data)
    print("거래 알림 전송 완료")
    
    print("모든 테스트가 완료되었습니다. 텔레그램에서 메시지를 확인하세요.")

if __name__ == "__main__":
    asyncio.run(test_alert()) 