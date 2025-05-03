import asyncio
from datetime import datetime
from monitoring.alert_system import alert_system

async def run_comprehensive_test():
    """알림 시스템 종합 테스트"""
    print("========== 알림 시스템 종합 테스트 시작 ==========")
    
    # 1. 기본 알림 테스트
    print("\n1. 기본 알림 테스트")
    await alert_system.send_alert(
        "기본 알림 테스트 메시지입니다.",
        level="INFO",
        channel="telegram"
    )
    
    # 2. 거래 알림 테스트
    print("\n2. 거래 알림 테스트")
    test_trade = {
        "symbol": "삼성전자",
        "side": "매수",
        "price": 68500,
        "quantity": 10,
        "strategy": "스캘핑",
        "reason": "급등 포착"
    }
    await alert_system.notify_trade(test_trade)
    
    # 3. 오류 알림 테스트
    print("\n3. 오류 알림 테스트")
    test_error = ValueError("테스트용 오류 메시지")
    await alert_system.notify_error(test_error, "테스트 컨텍스트")
    
    # 4. 대규모 가격 변동 알림 테스트
    print("\n4. 대규모 가격 변동 알림 테스트")
    await alert_system.notify_large_movement("NAVER", 0.05, 2.3)  # 5% 상승, 거래량 2.3배
    
    # 5. 시스템 상태 알림 테스트
    print("\n5. 시스템 상태 알림 테스트")
    await alert_system.notify_system_status("RUNNING", "시스템이 정상적으로 실행 중입니다.")
    
    # 6. 일일 보고서 테스트
    print("\n6. 일일 보고서 테스트")
    today = datetime.now().strftime("%Y-%m-%d")
    test_report = {
        "date": today,
        "total_trades": 15,
        "win_rate": 0.67,  # 67%
        "total_pnl": 320000,
        "top_gainers": [
            {"symbol": "현대차", "change": 0.028, "pnl": 150000},
            {"symbol": "카카오", "change": 0.022, "pnl": 80000},
            {"symbol": "SK하이닉스", "change": 0.018, "pnl": 60000},
        ],
        "top_losers": [
            {"symbol": "셀트리온", "change": -0.015, "pnl": -40000},
            {"symbol": "POSCO", "change": -0.008, "pnl": -30000},
        ],
        "portfolio_value": 12500000
    }
    await alert_system.send_daily_report(test_report)
    
    print("\n========== 모든 테스트가 완료되었습니다. ==========")
    print("텔레그램에서 수신된 메시지를 확인하세요.")

if __name__ == "__main__":
    asyncio.run(run_comprehensive_test()) 