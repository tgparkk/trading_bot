import sys
import os
from pathlib import Path

# 프로젝트 루트 디렉토리를 Python 경로에 추가
sys.path.append(str(Path(__file__).parent.parent))

from flask import Flask, jsonify, request
from flask_cors import CORS  # CORS 추가
from utils.database import db
from core.api_client import api_client
from core.order_manager import order_manager
from core.stock_explorer import stock_explorer
import asyncio
from datetime import datetime
from monitoring.telegram_bot_handler import telegram_bot_handler
from utils.logger import logger
import threading
import time
import atexit

app = Flask(__name__)
# CORS 설정 추가 - 모든 오리진에서의 요청 허용
CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=True)

# 텔레그램 봇 상태 관리 전역 변수
telegram_bot_initialized = False
telegram_lock_file = os.path.join(Path(__file__).parent.parent, "telegram_bot.lock")

# KIS API 접속 테스트 및 결과 텔레그램 전송 함수
async def test_kis_api_connection():
    try:
        # KIS API 접속 시도 전 메시지 전송
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        pre_message = f"""
        *KIS API 접속 시도* 🔄
        시도 시간: {current_time}
        
        한국투자증권 API 서버에 접속을 시도합니다.
        """
        
        logger.log_system("KIS API 접속 시도 전 메시지 전송 중...")
        await telegram_bot_handler.send_message(pre_message)
        logger.log_system("KIS API 접속 시도 전 메시지 전송 완료")
        
        # 접속 시도 시간 기록을 위해 1초 대기
        await asyncio.sleep(1)
        
        # KIS API 접속 시도
        logger.log_system("KIS API 접속 시도 (계좌 잔고 조회)...")
        result = api_client.get_account_balance()
        
        # 현재 시간 갱신
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # 결과 확인 및 메시지 전송
        if result and result.get("rt_cd") == "0":
            # 성공 메시지
            logger.log_system("KIS API 접속 성공")
            success_message = f"""
            *KIS API 접속 성공* ✅
            접속 시간: {current_time}
            
            한국투자증권 API 서버에 성공적으로 접속했습니다.
            응답 메시지: {result.get("msg1", "정상")}
            """
            
            logger.log_system("KIS API 접속 성공 메시지 전송 중...")
            await telegram_bot_handler.send_message(success_message)
            logger.log_system("KIS API 접속 성공 메시지 전송 완료")
            return True
        else:
            # 실패 메시지
            error_msg = result.get("msg1", "알 수 없는 오류") if result else "응답 없음"
            logger.log_system(f"KIS API 접속 실패: {error_msg}")
            
            fail_message = f"""
            *KIS API 접속 실패* ❌
            시도 시간: {current_time}
            
            한국투자증권 API 서버 접속에 실패했습니다.
            오류: {error_msg}
            """
            
            logger.log_system("KIS API 접속 실패 메시지 전송 중...")
            await telegram_bot_handler.send_message(fail_message)
            logger.log_system("KIS API 접속 실패 메시지 전송 완료")
            return False
            
    except Exception as e:
        # 예외 발생 시 메시지
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logger.log_error(e, "KIS API 접속 중 예외 발생")
        
        error_message = f"""
        *KIS API 접속 중 오류 발생* ❌
        시도 시간: {current_time}
        
        한국투자증권 API 서버 접속 중 예외가 발생했습니다.
        오류 내용: {str(e)}
        """
        
        try:
            logger.log_system("KIS API 접속 오류 메시지 전송 중...")
            await telegram_bot_handler.send_message(error_message)
            logger.log_system("KIS API 접속 오류 메시지 전송 완료")
        except Exception as msg_error:
            logger.log_error(msg_error, "KIS API 접속 오류 메시지 전송 실패")
        
        return False

# 텔레그램 봇 핸들러 초기화 및 시작 메시지 전송
async def init_telegram_handler():
    global telegram_bot_initialized
    
    # 이미 초기화된 경우 건너뛰기
    if telegram_bot_initialized:
        logger.log_system("텔레그램 봇이 이미 초기화되어 있습니다.")
        return
    
    try:
        # 텔레그램 봇 핸들러 준비 대기
        logger.log_system("대시보드 백엔드: 텔레그램 봇 핸들러 준비 대기...")
        await telegram_bot_handler.wait_until_ready(timeout=10)
        
        # 대시보드 시작 알림 전송
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        dashboard_message = f"""
        *트레이딩 봇 대시보드 시작* 🚀
        시작 시간: {current_time}

        웹 기반 대시보드 모드로 트레이딩 봇이 시작되었습니다.
        백엔드 API 서버가 실행 중입니다.
        """
        
        logger.log_system("대시보드 시작 알림 전송 시도...")
        await telegram_bot_handler.send_message(dashboard_message)
        logger.log_system("대시보드 시작 알림 전송 완료")
        
        # KIS API 접속 테스트 및 결과 전송
        await test_kis_api_connection()
        
        # 초기화 완료 표시
        telegram_bot_initialized = True
        
        # 잠금 파일 생성
        with open(telegram_lock_file, "w") as f:
            f.write(str(datetime.now().timestamp()))
            
    except Exception as e:
        logger.log_error(e, "대시보드 시작 알림 전송 실패")

# 텔레그램 봇 종료 처리
def cleanup_telegram_bot():
    # 잠금 파일 제거
    try:
        if os.path.exists(telegram_lock_file):
            os.remove(telegram_lock_file)
            logger.log_system("텔레그램 봇 잠금 파일 제거 완료")
    except Exception as e:
        logger.log_error(e, "텔레그램 봇 잠금 파일 제거 실패")

# 프로그램 종료 시 정리 작업 등록
atexit.register(cleanup_telegram_bot)

# 텔레그램 봇 잠금 파일 확인
def check_telegram_lock():
    if os.path.exists(telegram_lock_file):
        try:
            # 파일이 있지만 5분(300초) 이상 지난 경우 무시하고 제거
            file_time = os.path.getmtime(telegram_lock_file)
            current_time = time.time()
            if current_time - file_time > 300:
                logger.log_system("오래된 텔레그램 봇 잠금 파일 발견. 제거합니다.")
                os.remove(telegram_lock_file)
                return False
            return True
        except Exception as e:
            logger.log_error(e, "텔레그램 봇 잠금 파일 확인 실패")
            return False
    return False

# 비동기 작업을 처리하기 위한 이벤트 루프 생성 및 실행
def start_telegram_handler():
    global telegram_bot_initialized
    
    # 이미 실행 중인 텔레그램 봇이 있는지 확인
    if check_telegram_lock():
        logger.log_system("다른 프로세스에서 실행 중인 텔레그램 봇이 감지되었습니다. 텔레그램 초기화를 건너뜁니다.")
        telegram_bot_initialized = True
        return
    
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        # 텔레그램 폴링 태스크 시작
        telegram_task = loop.create_task(telegram_bot_handler.start_polling())
        
        # 초기화 및 시작 메시지 전송 (및 KIS API 접속 테스트)
        loop.run_until_complete(init_telegram_handler())
        
        # 이벤트 루프 계속 실행 (백그라운드 스레드에서)
        def run_event_loop(loop):
            asyncio.set_event_loop(loop)
            try:
                loop.run_forever()
            except Exception as e:
                logger.log_error(e, "텔레그램 이벤트 루프 실행 오류")
            finally:
                if not loop.is_closed():
                    loop.close()
                    logger.log_system("텔레그램 이벤트 루프가 정상적으로 종료되었습니다.")
            
        thread = threading.Thread(target=run_event_loop, args=(loop,), daemon=True)
        thread.start()
        
        logger.log_system("텔레그램 핸들러가 백그라운드에서 실행 중입니다.")
    except Exception as e:
        logger.log_error(e, "텔레그램 핸들러 시작 오류")

# 서버 시작 시 텔레그램 핸들러 초기화
if __name__ == "__main__":
    start_telegram_handler()
    app.run(host='0.0.0.0', port=5050, debug=False)
else:
    # WSGI 서버에서 실행될 때도 텔레그램 핸들러 시작
    start_telegram_handler()

@app.route('/')
def home():
    return jsonify({"status": "Trading Bot API is running"})

@app.route('/api/status')
def api_status():
    return jsonify(db.get_latest_system_status())

@app.route('/api/trades')
def api_trades():
    return jsonify(db.get_trades()[:50])

@app.route('/api/token_logs')
def api_token_logs():
    return jsonify(db.get_token_logs()[:50])

@app.route('/api/symbol_search_logs')
def api_symbol_search_logs():
    return jsonify(db.get_symbol_search_logs()[:50])

@app.route('/api/account')
def api_account():
    # 계좌 정보 반환
    try:
        # KIS API 접속 시도
        raw_info = api_client.get_account_balance()
        
        # API 응답 결과 저장
        is_success = raw_info.get("rt_cd") == "0" if raw_info else False
        
        # 응답 데이터 가공 (프론트엔드용)
        info = {}
        if is_success:
            # 데이터 추출 (output1이 리스트이거나 비어 있는 경우 output2 사용)
            output1 = raw_info.get("output1", {})
            output2 = raw_info.get("output2", [])
            
            if isinstance(output1, list) and not output1 and isinstance(output2, list) and len(output2) > 0:
                # output2에서 첫 번째 항목 사용
                account_data = output2[0]
            elif isinstance(output1, dict) and output1:
                # output1이 딕셔너리인 경우 직접 사용
                account_data = output1
            elif isinstance(output2, list) and len(output2) > 0:
                # output1이 비어있고 output2가 있는 경우
                account_data = output2[0]
            else:
                # 기본값
                account_data = {}
            
            # 가공된 정보
            info = {
                "status": "success",
                "balance": {
                    "totalAssets": float(account_data.get("tot_evlu_amt", "0")),
                    "cashBalance": float(account_data.get("dnca_tot_amt", "0")),
                    "stockValue": float(account_data.get("scts_evlu_amt", "0")),
                    "availableAmount": float(account_data.get("nass_amt", "0"))
                },
                "timestamp": datetime.now().isoformat(),
                "message": raw_info.get("msg1", "정상")
            }
        else:
            # 실패 시 오류 메시지
            info = {
                "status": "error",
                "message": raw_info.get("msg1", "알 수 없는 오류") if raw_info else "응답 없음",
                "timestamp": datetime.now().isoformat()
            }
        
        # 비동기 작업을 동기적으로 실행하여 결과 텔레그램 전송
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # 결과에 따라 메시지 준비
        if is_success:
            message = f"""
            *계좌 정보 조회 성공* ✅
            조회 시간: {current_time}
            
            한국투자증권 API 계좌 정보 조회에 성공했습니다.
            응답 메시지: {raw_info.get("msg1", "정상")}
            """
        else:
            error_msg = raw_info.get("msg1", "알 수 없는 오류") if raw_info else "응답 없음"
            message = f"""
            *계좌 정보 조회 실패* ❌
            조회 시간: {current_time}
            
            한국투자증권 API 계좌 정보 조회에 실패했습니다.
            오류: {error_msg}
            """
        
        # 텔레그램 메시지 전송 (백그라운드에서 비동기로 실행)
        loop = asyncio.new_event_loop()
        
        async def send_account_result():
            try:
                await telegram_bot_handler.send_message(message)
                logger.log_system("계좌 정보 조회 결과 메시지 전송 완료")
            except Exception as e:
                logger.log_error(e, "계좌 정보 조회 결과 메시지 전송 실패")
        
        # 비동기 함수를 백그라운드 스레드에서 실행
        def run_background_task():
            asyncio.set_event_loop(loop)
            loop.run_until_complete(send_account_result())
            loop.close()
        
        thread = threading.Thread(target=run_background_task, daemon=True)
        thread.start()
        
        return jsonify(info)
    except Exception as e:
        # 예외 발생 시 오류 메시지 텔레그램 전송
        error_message = f"""
        *계좌 정보 조회 중 오류 발생* ❌
        조회 시간: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        
        한국투자증권 API 계좌 정보 조회 중 예외가 발생했습니다.
        오류 내용: {str(e)}
        """
        
        # 백그라운드에서 오류 메시지 전송
        try:
            loop = asyncio.new_event_loop()
            
            async def send_error_message():
                try:
                    await telegram_bot_handler.send_message(error_message)
                    logger.log_system("계좌 정보 조회 오류 메시지 전송 완료")
                except Exception as msg_error:
                    logger.log_error(msg_error, "계좌 정보 조회 오류 메시지 전송 실패")
            
            def run_background_task():
                asyncio.set_event_loop(loop)
                loop.run_until_complete(send_error_message())
                loop.close()
            
            thread = threading.Thread(target=run_background_task, daemon=True)
            thread.start()
        except Exception as thread_error:
            logger.log_error(thread_error, "텔레그램 메시지 스레드 생성 실패")
        
        # 사용자 친화적인 오류 응답
        return jsonify({
            "status": "error",
            "message": str(e),
            "timestamp": datetime.now().isoformat()
        }), 500

@app.route('/api/positions')
def api_positions():
    # 보유 종목 리스트 반환
    try:
        positions = db.get_all_positions()
        return jsonify(positions)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/candidates')
def api_candidates():
    # 매수 후보 리스트 반환 (최근 탐색 결과)
    try:
        # 최근 symbol_search_logs에서 마지막 성공 결과의 filtered_symbols
        logs = db.get_symbol_search_logs()
        for log in logs:
            if log['status'] == 'SUCCESS' and log['filtered_symbols']:
                # filtered_symbols는 심볼 리스트가 아니라 개수이므로, 실제 후보는 따로 관리 필요
                # 임시로 최근 거래량 상위 종목 반환
                break
        # 실제 후보 리스트는 TradingBot에서 관리하는 것이 가장 정확
        # 여기서는 임시로 최근 거래량 상위 종목 반환
        vol_data = api_client.get_market_trading_volume()
        if vol_data.get('rt_cd') == '0':
            raw_list = vol_data['output2'][:50]
            candidates = [item['mksc_shrn_iscd'] for item in raw_list]
            return jsonify(candidates)
        return jsonify([])
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/refresh_candidates', methods=['POST'])
def api_refresh_candidates():
    # 매수 후보 리스트 갱신 (비동기 트리거)
    try:
        # StockExplorer 호출
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        symbols = loop.run_until_complete(stock_explorer.get_tradable_symbols())
        loop.close()
        return jsonify({'candidates': symbols})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# 텔레그램 메시지 직접 전송하는 API 엔드포인트 추가
@app.route('/api/send_telegram', methods=['POST'])
def api_send_telegram():
    try:
        data = request.json
        message = data.get('message', '')
        if not message:
            return jsonify({'error': 'No message provided'}), 400
            
        # 비동기 작업을 동기적으로 실행
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        message_id = loop.run_until_complete(telegram_bot_handler.send_message(message))
        loop.close()
        
        return jsonify({'success': True, 'message_id': message_id})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# KIS API 접속 테스트 엔드포인트 추가
@app.route('/api/test_kis_connection', methods=['GET'])
def api_test_kis_connection():
    try:
        # 비동기 작업을 동기적으로 실행
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        # KIS API 접속 테스트 실행
        is_success = loop.run_until_complete(test_kis_api_connection())
        loop.close()
        
        if is_success:
            return jsonify({'success': True, 'message': 'KIS API 접속 성공'})
        else:
            return jsonify({'success': False, 'message': 'KIS API 접속 실패'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500 