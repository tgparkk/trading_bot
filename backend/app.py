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
import queue

app = Flask(__name__)
# CORS 설정 개선 - 모든 경로에 대해 모든 오리진에서의 요청 허용
CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=True)

# 텔레그램 봇 상태 관리 전역 변수
telegram_bot_initialized = False
telegram_lock_file = os.path.join(Path(__file__).parent.parent, "telegram_bot.lock")

# 텔레그램 메시지 큐 생성 (Thread-Safe)
telegram_message_queue = queue.Queue()

# 텔레그램 메시지를 큐에 추가하는 헬퍼 함수
def queue_telegram_message(text, reply_to=None, priority="normal"):
    try:
        # 큐에 메시지 추가
        message_data = {
            "text": text,
            "reply_to": reply_to,
            "priority": priority,
            "timestamp": datetime.now().timestamp()
        }
        telegram_message_queue.put(message_data)
        
        # 로깅을 위한 요약 메시지 생성 (처음 30자 정도)
        text_for_log = text[:30] + "..." if len(text) > 30 else text
        
        # 이모지가 있으면 로깅용으로 대체 - 기존 utils.logger에 정의된 함수 사용
        from utils.logger import sanitize_for_console
        text_for_log = sanitize_for_console(text_for_log)
        
        logger.log_system(f"텔레그램 메시지가 전송 큐에 추가됨: {text_for_log}")
        return True
    except Exception as e:
        logger.log_error(e, "메시지 큐 추가 중 오류 발생")
        return False

# 테스트용 API 엔드포인트 추가
@app.route('/test')
def test():
    return jsonify({"status": "OK", "message": "Test endpoint is working"})

@app.route('/api/test')
def api_test():
    return jsonify({"status": "OK", "message": "API test endpoint is working"})

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
        # 요청 출처 확인 - 웹 요청인지 아닌지 판단
        # 텔레그램 메시지를 보낼지 여부 결정 (쿼리 파라미터로 제어)
        send_telegram = request.args.get('notify', 'false').lower() == 'true'
        
        # User-Agent 확인하여 웹 브라우저에서의 요청인지 확인
        user_agent = request.headers.get('User-Agent', '')
        is_web_browser = 'mozilla' in user_agent.lower() or 'chrome' in user_agent.lower() or 'safari' in user_agent.lower()
        
        # 웹 브라우저 요청이고 명시적으로 notify=true가 아니면 알림 비활성화
        if is_web_browser and not send_telegram:
            logger.log_system("웹 브라우저에서 계좌 정보 조회 요청 - 텔레그램 알림 비활성화")
            send_telegram = False
        
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
        
        # 텔레그램으로 알림 보내기 (웹에서의 일반 요청이 아닌 경우에만)
        if send_telegram or not is_web_browser:
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
            
            # 텔레그램 메시지 전송 (큐에 추가하여 비동기 처리)
            queue_telegram_message(message, priority="high")
            logger.log_system("계좌 정보 조회 결과 메시지 전송 완료")
        else:
            logger.log_system("웹 요청으로 인한 계좌 정보 조회 - 텔레그램 알림 전송 생략")
        
        return jsonify(info)
    except Exception as e:
        # 예외 발생 시 오류 메시지 텔레그램 전송 (항상)
        error_message = f"""
        *계좌 정보 조회 중 오류 발생* ❌
        조회 시간: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        
        한국투자증권 API 계좌 정보 조회 중 예외가 발생했습니다.
        오류 내용: {str(e)}
        """
        
        # 오류 메시지를 큐에 추가 (우선순위 높음)
        queue_telegram_message(error_message, priority="high")
        logger.log_system("계좌 정보 조회 오류 메시지 큐에 추가 완료")
        
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
    """텔레그램으로 메시지 전송 (웹 인터페이스에서 호출)"""
    try:
        data = request.json
        message = data.get('message', '')
        
        if not message:
            return jsonify({'error': 'No message provided'}), 400
            
        # 메시지 큐에 추가
        queue_telegram_message(message)
        logger.log_system("웹 인터페이스에서 요청한 텔레그램 메시지를 큐에 추가했습니다.")
            
        return jsonify({'status': 'success', 'message': 'Message queued for delivery'})
    except Exception as e:
        logger.log_error(e, "텔레그램 메시지 전송 API 오류")
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

# KIS API 접속 테스트 및 결과 텔레그램 전송 함수
async def test_kis_api_connection():
    """KIS API 접속 테스트 및 결과 전송"""
    try:
        logger.log_system("KIS API 접속 테스트 시작...")
        # api_client.check_connectivity 메서드를 사용해 접속 테스트
        result = await api_client.check_connectivity()
        
        # 현재 시간 포맷팅
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        if result and result.get("rt_cd") == "0":
            # 성공 메시지
            logger.log_system("KIS API 접속 성공")
            
            success_message = f"""
            *KIS API 접속 성공* ✅
            테스트 시간: {current_time}
            
            한국투자증권 API 서버에 성공적으로 접속했습니다.
            응답 메시지: {result.get("msg1", "정상")}
            """
            
            logger.log_system("KIS API 접속 성공 메시지 큐에 추가 중...")
            queue_telegram_message(success_message)
            logger.log_system("KIS API 접속 성공 메시지 큐에 추가 완료")
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
            
            logger.log_system("KIS API 접속 실패 메시지 큐에 추가 중...")
            queue_telegram_message(fail_message, priority="high")
            logger.log_system("KIS API 접속 실패 메시지 큐에 추가 완료")
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
            # 오류 메시지 큐에 추가
            logger.log_system("KIS API 접속 오류 메시지 큐에 추가 중...")
            queue_telegram_message(error_message, priority="high")
            logger.log_system("KIS API 접속 오류 메시지 큐에 추가 완료")
        except Exception as msg_error:
            logger.log_error(msg_error, "KIS API 접속 오류 메시지 큐 추가 실패")
        
        return False

# 메시지 큐 처리 함수
async def process_message_queue():
    """텔레그램 메시지 큐에서 메시지를 꺼내 전송하는 비동기 함수"""
    logger.log_system("텔레그램 메시지 큐 처리 시작")
    
    # 로컬 실행 플래그 - 안정적인 종료를 위해 사용
    running = True
    last_status_log = time.time()
    empty_counter = 0
    
    while running:
        try:
            # 큐에서 메시지 가져오기 (비차단 방식)
            try:
                message_data = telegram_message_queue.get_nowait()
                empty_counter = 0  # 메시지를 찾았으므로 카운터 초기화
                
                text = message_data.get("text", "")
                reply_to = message_data.get("reply_to")
                priority = message_data.get("priority", "normal")
                
                # 로깅용 텍스트 준비 (이모지 처리)
                from utils.logger import sanitize_for_console
                log_text = text[:30] + "..." if len(text) > 30 else text
                log_text = sanitize_for_console(log_text)
                
                # 메시지 전송 - 직접 HTTP 요청 사용 (aiohttp 세션 문제 회피)
                try:
                    logger.log_system(f"큐에서 메시지 처리 중 (우선순위: {priority}): {log_text}")
                    
                    # 텔레그램 봇 핸들러가 실행 중인지 확인
                    if telegram_bot_handler.bot_running:
                        # 제대로 된 비동기 태스크로 래핑하여 실행
                        send_task = asyncio.create_task(safe_send_message(text, reply_to))
                        await send_task
                        logger.log_system("큐의 메시지 처리 완료")
                    else:
                        logger.log_system("텔레그램 봇이 실행 중이 아니어서 메시지를 보낼 수 없습니다.", level="WARNING")
                        
                except Exception as send_error:
                    logger.log_error(send_error, "큐의 메시지 전송 중 오류 발생")
                
                # 작업 완료 표시
                telegram_message_queue.task_done()
                
            except queue.Empty:
                # 큐가 비어있으면 잠시 대기
                empty_counter += 1
                
                # 주기적으로 상태 로그 남기기 (1분에 한 번)
                current_time = time.time()
                if current_time - last_status_log > 60:
                    logger.log_system(f"메시지 큐 처리기 실행 중: 큐 크기 = {telegram_message_queue.qsize()}, 빈 횟수 = {empty_counter}")
                    last_status_log = current_time
                
                # 텔레그램 봇 핸들러가 종료되었는지 확인
                if not telegram_bot_handler.bot_running and empty_counter > 10:
                    logger.log_system("텔레그램 봇이 종료되어 메시지 큐 처리기도 종료합니다.")
                    running = False
                    break
                
                pass
                
            # 처리 간격 - 부하 방지와 CPU 사용량 감소를 위해
            await asyncio.sleep(0.5)
            
        except Exception as e:
            logger.log_error(e, "메시지 큐 처리 중 오류 발생")
            await asyncio.sleep(5)  # 오류 발생 시 더 오래 대기
    
    logger.log_system("메시지 큐 처리기가 정상적으로 종료되었습니다.")

# 백그라운드 이벤트 루프에서 메시지 큐 처리 실행
def start_message_queue_processor():
    """메시지 큐 처리기를 백그라운드에서 실행"""
    try:
        logger.log_system("메시지 큐 처리기 시작 중...")
        loop = asyncio.new_event_loop()
        
        # 큐 처리 스레드 참조를 저장
        queue_processor_thread = None
        
        def run_processor():
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(process_message_queue())
            except Exception as e:
                logger.log_error(e, "메시지 큐 처리기 오류")
            finally:
                if not loop.is_closed():
                    loop.close()
                    logger.log_system("메시지 큐 처리기 이벤트 루프가 종료되었습니다")
        
        # 백그라운드 스레드 시작
        queue_processor_thread = threading.Thread(target=run_processor, daemon=True)
        queue_processor_thread.start()
        logger.log_system("메시지 큐 처리기가 백그라운드에서 실행 중입니다")
        
        # 스레드 참조 반환
        return queue_processor_thread
    except Exception as e:
        logger.log_error(e, "메시지 큐 처리기 시작 실패")
        return None

# 텔레그램 봇 핸들러 초기화 및 시작 메시지 전송
async def init_telegram_handler():
    """텔레그램 봇 초기화 및 시작 메시지 전송"""
    try:
        # 텔레그램 봇 핸들러 준비 대기
        logger.log_system("대시보드 백엔드: 텔레그램 봇 핸들러 준비 대기...")
        
        # 대시보드 시작 알림 텍스트 준비
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        dashboard_message = f"""
        *트레이딩 봇 대시보드 시작* 🚀
        시작 시간: {current_time}

        웹 기반 대시보드 모드로 트레이딩 봇이 시작되었습니다.
        백엔드 API 서버가 실행 중입니다.
        """
        
        # 시작 메시지를 큐에 추가
        logger.log_system("대시보드 시작 알림 메시지 큐에 추가 중...")
        queue_telegram_message(dashboard_message)
        logger.log_system("대시보드 시작 알림 메시지 큐에 추가 완료")
        
    except Exception as e:
        logger.log_error(e, "텔레그램 봇 초기화 오류")

# 직접 HTTP 요청을 사용한 메시지 전송 함수 (aiohttp 세션 문제 회피)
async def safe_send_message(text, reply_to=None):
    """안전하게 텔레그램 메시지를 전송하는 함수"""
    try:
        # 세션 문제를 회피하기 위해 requests 모듈 사용 (동기식)
        import requests
        
        # 메시지 텍스트 준비
        params = {
            "chat_id": telegram_bot_handler.chat_id,
            "text": text,
            "parse_mode": "HTML"  # HTML 형식 지원 활성화
        }
        
        # 회신 메시지인 경우
        if reply_to:
            params["reply_to_message_id"] = reply_to
        
        # API URL
        url = f"https://api.telegram.org/bot{telegram_bot_handler.token}/sendMessage"
        
        # 비동기 블로킹을 방지하기 위해 동기식 HTTP 요청을 별도 스레드에서 실행
        import concurrent.futures
        
        # 스레드풀에서 동기식 함수 실행
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(lambda: requests.post(url, params=params, timeout=10))
            
            # 메시지 ID 반환
            response = await asyncio.wrap_future(future)
            if response.status_code == 200:
                data = response.json()
                if data.get("ok"):
                    message_id = data.get("result", {}).get("message_id")
                    logger.log_system(f"텔레그램 메시지 전송 성공 (message_id: {message_id})")
                    return message_id
                else:
                    error_msg = f"텔레그램 API 오류: {data.get('description', '알 수 없는 오류')}"
                    logger.log_system(error_msg, level="ERROR")
                    raise Exception(error_msg)
            else:
                error_msg = f"텔레그램 API 응답 오류: HTTP {response.status_code}"
                logger.log_system(error_msg, level="ERROR")
                raise Exception(error_msg)
                
    except Exception as e:
        logger.log_error(e, "안전한 텔레그램 메시지 전송 중 오류 발생")
        raise

@app.route('/api/token/status')
def api_token_status():
    """토큰 상태 확인"""
    try:
        status = api_client.check_token_status()
        return jsonify(status)
    except Exception as e:
        logger.log_error(e, "토큰 상태 확인 중 오류 발생")
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500

@app.route('/api/token/refresh', methods=['POST'])
def api_token_refresh():
    """토큰 강제 갱신"""
    try:
        result = api_client.force_token_refresh()
        
        if result["status"] == "success":
            return jsonify(result)
        else:
            return jsonify(result), 500
    except Exception as e:
        logger.log_error(e, "토큰 갱신 중 오류 발생")
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500

# 서버 시작 시 필요한 초기화 작업
def initialize_server():
    """서버 시작 시 필요한 초기화 작업 수행"""
    try:
        logger.log_system("백엔드 서버 초기화 중...")
        
        # 텔레그램 설정 로그 출력
        logger.log_system(f"텔레그램 설정 - 토큰: {telegram_bot_handler.token[:10]}..., 채팅 ID: {telegram_bot_handler.chat_id}")
        
        # 텔레그램 봇 폴링 태스크 시작 (별도 스레드에서 실행)
        import threading
        
        def start_telegram_polling():
            try:
                telegram_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(telegram_loop)
                # 텔레그램 봇 폴링 시작
                logger.log_system("텔레그램 봇 폴링 시작 중...")
                telegram_loop.run_until_complete(telegram_bot_handler.start_polling())
                # 이벤트 루프 계속 실행
                telegram_loop.run_forever()
            except Exception as e:
                logger.log_error(e, "텔레그램 봇 폴링 스레드 오류")
            finally:
                if not telegram_loop.is_closed():
                    telegram_loop.close()
                logger.log_system("텔레그램 봇 폴링 스레드 종료")
                
        # 텔레그램 폴링 스레드 시작
        telegram_thread = threading.Thread(target=start_telegram_polling, daemon=True)
        telegram_thread.start()
        logger.log_system("텔레그램 봇 폴링이 백그라운드에서 시작되었습니다")
        
        # 텔레그램 봇이 준비될 때까지 잠시 대기 (최대 10초)
        time.sleep(2)
        
        # 텔레그램 봇 초기화
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(init_telegram_handler())
        loop.close()
        
        # 메시지 큐 처리기 시작
        queue_processor_thread = start_message_queue_processor()
        
        # 종료 시 정리 함수 등록
        def cleanup():
            logger.log_system("백엔드 서버 종료 중...")
            # 종료 메시지 큐에 추가
            try:
                shutdown_message = f"""
                *트레이딩 봇 대시보드 종료* 🛑
                종료 시간: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

                웹 기반 대시보드가 종료되었습니다.
                """
                queue_telegram_message(shutdown_message)
                # 큐의 메시지가 모두 처리될 때까지 약간의 시간 대기
                time.sleep(2)
            except Exception as e:
                logger.log_error(e, "종료 메시지 전송 실패")
        
        # 종료 시 정리 함수 등록
        atexit.register(cleanup)
        
        logger.log_system("백엔드 서버 초기화 완료")
    except Exception as e:
        logger.log_error(e, "백엔드 서버 초기화 실패")

# 서버 시작
if __name__ == "__main__":
    # 서버 초기화
    initialize_server()
    # Flask 앱 실행
    app.run(host='0.0.0.0', port=5050, debug=False)
else:
    # WSGI 서버에서 실행될 때도 초기화
    initialize_server()
        