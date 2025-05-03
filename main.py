"""
주식 자동매매 프로그램 메인
"""
import asyncio
import signal
import sys
import os
from dotenv import load_dotenv
import os
from pathlib import Path
from datetime import datetime, time, timedelta
from typing import List
from config.settings import config
from core.api_client import api_client
from core.websocket_client import ws_client
from core.order_manager import order_manager
from strategies.scalping_strategy import scalping_strategy
from utils.logger import logger
from utils.database import db
from monitoring.alert_system import alert_system

# 1) .env 파일 경로 지정 (현재 파일 기준)
env_path = Path(__file__).parent / '.env'

# 2) .env 안의 키=값 쌍을 모두 os.environ에 로드
load_dotenv(dotenv_path=env_path)

print("Loaded KIS_ACCOUNT_NO:", os.getenv("KIS_ACCOUNT_NO"))

class TradingBot:
    """자동매매 봇"""
    
    def __init__(self):
        self.running = False
        self.trading_config = config["trading"]
        
    async def initialize(self):
        """초기화"""
        try:
            logger.log_system("Initializing trading bot...")
            
            # DB 초기화
            db.update_system_status("INITIALIZING")
            
            # 주문 관리자 초기화
            await order_manager.initialize()
            
            # 웹소켓 연결
            await ws_client.connect()
            
            # 시스템 상태 업데이트
            db.update_system_status("RUNNING")
            
            # 시작 알림
            await alert_system.notify_system_status(
                "RUNNING", 
                "Trading bot initialized successfully"
            )
            
            logger.log_system("Trading bot initialized successfully")
            
        except Exception as e:
            logger.log_error(e, "Failed to initialize trading bot")
            await self.shutdown(error=str(e))
            raise
    
    async def run(self):
        """실행"""
        try:
            self.running = True
            await self.initialize()
            
            # 거래 가능 종목 조회
            symbols = await self._get_tradable_symbols()
            logger.log_system(f"Found {len(symbols)} tradable symbols")
            
            # 전략 시작
            await scalping_strategy.start(symbols[:50])  # 상위 50종목만
            
            # 메인 루프
            while self.running:
                current_time = datetime.now().time()
                
                # 장 시간 체크
                if self._is_market_open(current_time):
                    # 포지션 체크
                    await order_manager.check_positions()
                    
                    # 시스템 상태 업데이트
                    db.update_system_status("RUNNING")
                    
                else:
                    # 장 마감 처리
                    if current_time > self.trading_config.market_close:
                        await self._handle_market_close()
                
                await asyncio.sleep(10)  # 10초 대기
                
        except Exception as e:
            logger.log_error(e, "Trading bot error")
            await self.shutdown(error=str(e))
    
    async def _get_tradable_symbols(self) -> List[str]:
        """거래 가능 종목 조회 (필터링 포함)"""
        # 1) 거래량 순위 API 호출
        vol_data = api_client.get_market_trading_volume()  # :contentReference[oaicite:0]{index=0}:contentReference[oaicite:1]{index=1}
        if vol_data.get("rt_cd") != "0":
            logger.log_error("거래량 순위 조회 실패")
            return []

        raw_list = vol_data["output2"][:200]  # 상위 200개만 우선 추출 :contentReference[oaicite:2]{index=2}:contentReference[oaicite:3]{index=3}
        f = self.trading_config.filters

        end_date   = datetime.now().strftime("%Y%m%d")
        start_date = (datetime.now() - timedelta(days=f["avg_vol_days"])).strftime("%Y%m%d")

        candidates = []
        for item in raw_list:
            symbol = item["mksc_shrn_iscd"]
            today_vol = int(item.get("prdy_vrss_vol", 0))  # 실제 필드명 확인 필요

            # 2) 30일 일별 시세 조회 → 거래량·종가 리스트 확보
            daily = api_client.get_daily_price(symbol, start_date, end_date)
            if daily.get("rt_cd") != "0":
                continue
            days = daily["output2"]
            vols   = [int(d["acml_vol"])  for d in days]      # 일별 누적거래량
            closes = [float(d["stck_clpr"]) for d in days]     # 일별 종가

            # 3) 평균 거래량 필터
            avg_vol = sum(vols) / len(vols)
            if avg_vol < f["min_avg_volume"]:
                continue

            # 4) 거래량 급증 필터
            if today_vol / avg_vol < f["vol_spike_ratio"]:
                continue

            # 5) 현재가 필터
            price_data = api_client.get_current_price(symbol)
            if price_data.get("rt_cd") != "0":
                continue
            price = float(price_data["output"]["stck_prpr"])
            if not (f["price_min"] <= price <= f["price_max"]):
                continue

            # 6) 단기 변동성 필터 (절대 수익률의 평균)
            changes = [
                abs((closes[i] - closes[i-1]) / closes[i-1])
                for i in range(1, len(closes))
            ]
            volat = sum(changes) / len(changes)
            if volat > f["max_volatility"]:
                continue

            candidates.append((symbol, today_vol))

        # 7) 거래량 기준 내림차순 정렬 후 상위 N개 심볼 반환
        candidates.sort(key=lambda x: x[1], reverse=True)
        return [sym for sym, _ in candidates[: f["max_symbols"]]]
    
    def _is_market_open(self, current_time: time) -> bool:
        """장 시간 확인"""
        return (
            self.trading_config.market_open <= current_time <= 
            self.trading_config.market_close
        )
    
    async def _handle_market_close(self):
        """장 마감 처리"""
        try:
            # 일일 요약
            summary = await order_manager.get_daily_summary()
            
            # 성과 기록
            db.save_performance(summary)
            
            # 일일 리포트 전송
            await alert_system.send_daily_report(summary)
            
            # 데이터베이스 백업
            db.backup_database()
            
            logger.log_system("Market closed. Daily process completed")
            
        except Exception as e:
            logger.log_error(e, "Market close handling error")
    
    async def shutdown(self, error: str = None):
        """종료"""
        try:
            self.running = False
            logger.log_system("Shutting down trading bot...")
            
            # 전략 중지
            await scalping_strategy.stop()
            
            # 웹소켓 종료
            await ws_client.close()
            
            # 시스템 상태 업데이트
            if error:
                db.update_system_status("ERROR", error)
                await alert_system.notify_system_status("ERROR", error)
            else:
                db.update_system_status("STOPPED")
                await alert_system.notify_system_status("STOPPED")
            
            logger.log_system("Trading bot shut down completed")
            
        except Exception as e:
            logger.log_error(e, "Shutdown error")
        finally:
            sys.exit(0 if not error else 1)

async def main():
    """메인 함수"""
    bot = TradingBot()
    
    # 시그널 핸들러 설정
    def signal_handler(signum, frame):
        asyncio.create_task(bot.shutdown())
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    try:
        await bot.run()
    except KeyboardInterrupt:
        await bot.shutdown()
    except Exception as e:
        logger.log_error(e, "Unexpected error")
        await bot.shutdown(error=str(e))

if __name__ == "__main__":


    # 환경 변수 체크
    required_env_vars = [
        "KIS_BASE_URL",
        "KIS_APP_KEY",
        "KIS_APP_SECRET",
        "KIS_ACCOUNT_NO"
    ]
    
    missing_vars = []
    for var in required_env_vars:
        if not os.getenv(var):
            missing_vars.append(var)
    
    if missing_vars:
        print(f"Missing required environment variables: {', '.join(missing_vars)}")
        print("Please set these environment variables before running the bot.")
        sys.exit(1)
    
    # 이벤트 루프 실행
    asyncio.run(main())
