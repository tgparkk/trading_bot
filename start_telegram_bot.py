"""
텔레그램 봇 백그라운드 실행 스크립트
"""
import sys
import asyncio
import signal
import aiohttp
import os
import json
from datetime import datetime
from pathlib import Path
from monitoring.telegram_bot_handler import telegram_bot_handler
from utils.logger import logger
from utils.database import db

# 종료 시그널 처리
shutdown_requested = False
signal_handler_called = False

def signal_handler(sig, frame):
    """종료 시그널 처리"""
    global shutdown_requested, signal_handler_called
    print("종료 요청 받음 (Ctrl+C)")
    shutdown_requested = True
    signal_handler_called = True

# 시그널 핸들러 등록
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# 프로세스 락 파일 경로
LOCK_FILE = Path("telegram_bot.lock")

def check_bot_running():
    """봇이 이미 실행 중인지 확인하고, 실행 중이면 자동으로 종료"""
    if LOCK_FILE.exists():
        try:
            with open(LOCK_FILE, "r") as f:
                data = json.load(f)
                pid = data.get("pid")
                start_time = data.get("start_time")
                
                # PID가 여전히 활성 상태인지 확인
                try:
                    # psutil이 설치되어 있는지 확인
                    import psutil
                    if psutil.pid_exists(pid):
                        # 해당 프로세스가 실제로 텔레그램 봇인지 확인 (명령줄 확인)
                        is_bot_process = False
                        try:
                            process = psutil.Process(pid)
                            cmdline = " ".join(process.cmdline())
                            # 명령줄에 telegram 또는 start_telegram_bot이 포함되어 있는지 확인
                            if any(keyword in cmdline.lower() for keyword in ["telegram", "start_telegram_bot.py"]):
                                is_bot_process = True
                                print(f"⚠️ 텔레그램 봇 프로세스가 이미 실행 중입니다 (PID: {pid}, 시작 시간: {start_time})")
                                print(f"명령줄: {cmdline}")
                            else:
                                print(f"경고: 락 파일에 등록된 PID {pid}는 다른 프로세스입니다: {cmdline}")
                                print("잘못된 락 파일을 삭제합니다.")
                                LOCK_FILE.unlink(missing_ok=True)
                                return False
                        except (psutil.AccessDenied, psutil.NoSuchProcess):
                            # 프로세스 접근 권한이 없으면 PID만으로 판단
                            print(f"⚠️ PID {pid}의 프로세스가 존재하지만 명령줄을 확인할 수 없습니다. 텔레그램 봇으로 가정합니다.")
                            is_bot_process = True
                        
                        # 텔레그램 봇 프로세스라면 종료 시도
                        if is_bot_process:
                            print("기존 프로세스를 종료하고 새로 시작합니다...")
                            
                            try:
                                # 프로세스 종료 시도
                                process = psutil.Process(pid)
                                process.terminate()  # SIGTERM 신호 전송
                                
                                # 최대 5초 동안 종료될 때까지 대기
                                process.wait(timeout=5)
                                print(f"✅ 이전 텔레그램 봇 프로세스(PID: {pid})가 성공적으로 종료되었습니다.")
                                LOCK_FILE.unlink(missing_ok=True)
                                return False
                            except psutil.NoSuchProcess:
                                print(f"프로세스(PID: {pid})가 이미 종료되었습니다.")
                                LOCK_FILE.unlink(missing_ok=True)
                                return False
                            except psutil.TimeoutExpired:
                                print(f"프로세스(PID: {pid}) 종료 시간 초과. 강제 종료를 시도합니다.")
                                try:
                                    process.kill()  # SIGKILL 신호 전송 (강제 종료)
                                    print(f"✅ 이전 텔레그램 봇 프로세스(PID: {pid})가 강제 종료되었습니다.")
                                    LOCK_FILE.unlink(missing_ok=True)
                                    return False
                                except Exception as kill_error:
                                    print(f"강제 종료 실패: {str(kill_error)}")
                                    print("기존 인스턴스를 수동으로 종료한 후 다시 시도하세요.")
                                    return True
                            except Exception as e:
                                print(f"프로세스 종료 중 오류 발생: {str(e)}")
                                print("기존 인스턴스를 수동으로 종료한 후 다시 시도하세요.")
                                return True
                    else:
                        print(f"락 파일에 등록된 PID {pid}의 프로세스가 존재하지 않습니다.")
                        print("오래된 락 파일을 삭제합니다.")
                        LOCK_FILE.unlink(missing_ok=True)
                        return False
                except ImportError:
                    # psutil이 설치되지 않은 경우
                    print("⚠️ psutil 모듈이 설치되지 않아 기존 프로세스를 자동으로 종료할 수 없습니다.")
                    print("pip install psutil 명령으로 psutil을 설치하거나,")
                    print("기존 인스턴스를 수동으로 종료한 후 다시 시도하세요.")
                    return True
        except (json.JSONDecodeError, KeyError) as e:
            print(f"락 파일이 손상되었습니다: {e}")
            
        # 락 파일은 존재하지만 프로세스가 실행 중이 아니거나 종료된 경우, 락 파일 삭제
        LOCK_FILE.unlink(missing_ok=True)
        print("이전 락 파일을 삭제했습니다.")
        
    return False

def create_lock_file():
    """프로세스 락 파일 생성"""
    data = {
        "pid": os.getpid(),
        "start_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    
    with open(LOCK_FILE, "w") as f:
        json.dump(data, f)
    
    print(f"프로세스 락 파일 생성: {LOCK_FILE}")

def remove_lock_file():
    """프로세스 락 파일 제거"""
    if LOCK_FILE.exists():
        LOCK_FILE.unlink()
        print(f"프로세스 락 파일 제거: {LOCK_FILE}")

async def reset_telegram_webhook():
    """텔레그램 웹훅 초기화 - 409 충돌 문제 해결"""
    try:
        print("텔레그램 웹훅 초기화 중...")
        token = telegram_bot_handler.token
        base_url = f"https://api.telegram.org/bot{token}"
        
        # 먼저 동일한 봇이 실행 중인지 완전히 확인하기 위해
        # 실행 중인 모든 python 프로세스를 검색
        try:
            import psutil
            current_pid = os.getpid()
            telegram_processes = []
            
            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    # 자기 자신은 제외
                    if proc.info['pid'] == current_pid:
                        continue
                    
                    proc_name = proc.info['name'].lower()
                    if "python" in proc_name or "pythonw" in proc_name:
                        cmd = " ".join(proc.cmdline())
                        # 텔레그램 봇 관련 프로세스 확인
                        if any(x in cmd for x in ['start_telegram_bot.py', 'telegram_bot']):
                            telegram_processes.append(proc)
                            print(f"⚠️ 다른 텔레그램 봇 인스턴스 발견: PID {proc.pid}, CMD: {cmd}")
                except (psutil.NoSuchProcess, psutil.AccessDenied, Exception):
                    continue
            
            # 발견된 텔레그램 봇 프로세스 강제 종료
            if telegram_processes:
                print(f"⚠️ {len(telegram_processes)}개의 텔레그램 봇 인스턴스가 이미 실행 중입니다. 모두 종료합니다...")
                for proc in telegram_processes:
                    try:
                        proc.terminate()
                        print(f"PID {proc.pid} 종료 요청")
                    except Exception as e:
                        print(f"PID {proc.pid} 종료 중 오류: {e}")
                
                # 최대 5초간 종료 대기
                await asyncio.sleep(2)
                
                # 여전히 살아있는 프로세스 확인
                still_alive = []
                for proc in telegram_processes:
                    try:
                        if proc.is_running():
                            still_alive.append(proc)
                    except:
                        pass
                        
                # 여전히 살아있는 프로세스 강제 종료
                if still_alive:
                    print(f"응답하지 않는 {len(still_alive)}개 프로세스 강제 종료...")
                    for proc in still_alive:
                        try:
                            proc.kill()
                        except:
                            pass
                
                # 텔레그램 충돌 문제 해결을 위해 잠시 대기
                print("다른 텔레그램 봇 인스턴스가 완전히 종료되기를 기다립니다...")
                await asyncio.sleep(3)
        except ImportError:
            print("psutil이 설치되어 있지 않아 다른 텔레그램 봇 인스턴스 확인을 건너뜁니다.")
        except Exception as e:
            print(f"다른 텔레그램 봇 인스턴스 확인 중 오류: {e}")
        
        # 더욱 강화된 웹훅 초기화 로직
        print("웹훅 초기화 및 업데이트 큐 정리 시작...")
        
        async with aiohttp.ClientSession() as session:
            # 1. 웹훅 정보 확인
            async with session.get(f"{base_url}/getWebhookInfo") as response:
                data = await response.json()
                webhook_url = data.get("result", {}).get("url", "")
                
                if webhook_url:
                    print(f"기존 웹훅 URL 발견: {webhook_url}, 삭제 시도...")
                
            # 2. 웹훅 강제 삭제 (drop_pending_updates=True 추가)
            async with session.get(f"{base_url}/deleteWebhook", params={"drop_pending_updates": True}) as response:
                data = await response.json()
                success = data.get("ok", False)
                
                if success:
                    print("✅ 웹훅 초기화 성공 (대기 중인 업데이트 모두 제거)")
                else:
                    print(f"⚠️ 웹훅 초기화 실패: {data}")
            
            # 3. 업데이트 큐 초기화 (큰 오프셋 값으로 모든 이전 업데이트 건너뛰기)
            print("업데이트 큐 초기화 중...")
            try:
                # 먼저 현재 업데이트 ID 확인
                async with session.get(f"{base_url}/getUpdates", params={"limit": 1}) as response:
                    data = await response.json()
                    updates = data.get("result", [])
                    
                    if updates:
                        # 가장 최근 업데이트의 ID + 1로 오프셋 설정 (이전 업데이트 모두 무시)
                        last_update_id = updates[0].get("update_id", 0)
                        offset = last_update_id + 1
                        
                        # 새 오프셋으로 업데이트 초기화
                        async with session.get(f"{base_url}/getUpdates", params={"offset": offset}) as reset_response:
                            reset_data = await reset_response.json()
                            print(f"업데이트 큐 초기화 완료: 오프셋 {offset}으로 설정됨")
                    else:
                        # 업데이트가 없는 경우 큰 음수 값으로 초기화
                        async with session.get(f"{base_url}/getUpdates", params={"offset": -1}) as reset_response:
                            reset_data = await reset_response.json()
                            print("업데이트 큐 초기화 완료 (업데이트 없음)")
            except Exception as e:
                print(f"업데이트 큐 초기화 중 오류: {e}")
            
            # 4. 웹훅 상태 최종 확인
            async with session.get(f"{base_url}/getWebhookInfo") as response:
                data = await response.json()
                if not data.get("result", {}).get("url", ""):
                    print("✅ 웹훅이 성공적으로 제거되었습니다. 폴링 모드로 전환됩니다.")
                    
                    # 5. 텔레그램 API 서버 응답 확인
                    async with session.get(f"{base_url}/getMe") as me_response:
                        me_data = await me_response.json()
                        if me_data.get("ok"):
                            bot_name = me_data.get("result", {}).get("username", "")
                            print(f"✅ 텔레그램 API 서버 응답 확인: {bot_name} 봇에 연결됨")
                        else:
                            print(f"⚠️ 텔레그램 API 서버 응답 오류: {me_data}")
                    
                    return True
                else:
                    print(f"⚠️ 웹훅 제거 실패: {data}")
                    return False
                    
    except Exception as e:
        print(f"❌ 웹훅 초기화 중 오류: {str(e)}")
        logger.log_error(e, "텔레그램 웹훅 초기화 중 오류")
        return False

async def shutdown():
    """봇 종료 처리"""
    global shutdown_requested
    print("텔레그램 봇 종료 중...")
    
    # 전역 종료 요청 플래그 설정
    shutdown_requested = True
    
    # 봇 종료 준비
    telegram_bot_handler.bot_running = False
    
    # 상태 업데이트
    try:
        db.update_system_status("STOPPED", "텔레그램 명령으로 시스템 종료됨")
    except Exception as e:
        print(f"상태 업데이트 실패: {e}")
    
    # 활성 세션이 모두 정리될 때까지 잠시 대기
    await asyncio.sleep(2)
    print("텔레그램 봇 종료 완료")
    
    # 락 파일 제거
    remove_lock_file()

    # 백엔드 프로세스 종료 시도 (향상된 방법)
    print("관련 프로세스 종료 시도...")
    try:
        # psutil을 사용한 프로세스 종료
        import psutil
        current_pid = os.getpid()
        current_process = psutil.Process(current_pid)
        print(f"현재 텔레그램 봇 프로세스: PID {current_pid}")
        
        # 부모 프로세스 종료 시도 (main.py일 가능성이 높음)
        try:
            parent = current_process.parent()
            print(f"부모 프로세스: {parent.name()} (PID: {parent.pid})")
            if "python" in parent.name().lower():
                print(f"부모 Python 프로세스 종료 시도...")
                parent.terminate()
        except Exception as e:
            print(f"부모 프로세스 접근 중 오류: {e}")
        
        # 모든 Python 프로세스 중 trading_bot 관련 프로세스 찾기
        python_processes = []
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                # 자기 자신은 제외
                if proc.info['pid'] == current_pid:
                    continue
                
                proc_name = proc.info['name'].lower()
                if "python" in proc_name or "pythonw" in proc_name:
                    try:
                        cmd = " ".join(proc.cmdline())
                        # trading_bot 관련 프로세스 확인
                        if any(x in cmd for x in ['main.py', 'trading_bot', 'start_fixed_system']):
                            python_processes.append(proc)
                            print(f"종료 대상 프로세스 발견: PID {proc.pid}, CMD: {cmd}")
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue
            except Exception:
                continue
        
        # 발견된 프로세스 종료
        if python_processes:
            print(f"{len(python_processes)}개의 관련 프로세스 종료 시작...")
            for proc in python_processes:
                try:
                    proc.terminate()
                    print(f"프로세스 PID {proc.pid} 종료 요청 완료")
                except Exception as e:
                    print(f"프로세스 종료 중 오류: {e}")
            
            # 5초간 프로세스가 종료되길 기다림
            gone, still_alive = psutil.wait_procs(python_processes, timeout=5)
            if still_alive:
                # 여전히 살아있는 프로세스 강제 종료
                print(f"{len(still_alive)}개 프로세스가 응답하지 않아 강제 종료합니다...")
                for proc in still_alive:
                    try:
                        proc.kill()  # SIGKILL로 강제 종료
                    except:
                        pass
        
        # Windows 환경에서 추가 종료 방법
        if os.name == 'nt':
            print("Windows taskkill 명령으로 Python 프로세스 종료 시도...")
            # 백엔드 프로세스 정확히 타겟팅
            os.system('taskkill /f /im python.exe /fi "COMMANDLINE eq *main.py*"')
            # 모든 trading_bot 관련 프로세스 타겟팅
            os.system('taskkill /f /im python.exe /fi "COMMANDLINE eq *trading_bot*"')
            # 같은 사용자의 Python 프로세스 타겟팅 (위험할 수 있음)
            os.system('taskkill /f /im python.exe /fi "USERNAME eq %USERNAME%"')
            print("taskkill 명령 실행 완료")
            
    except ImportError:
        print("psutil이 설치되지 않아 프로세스 관리 기능을 사용할 수 없습니다.")
    except Exception as e:
        print(f"프로세스 종료 중 일반 오류: {e}")
    
    # 강제 종료
    print("프로그램을 종료합니다...")
    # 1초 대기 후 종료
    await asyncio.sleep(1)
    
    try:
        # 강제 종료 (안전한 종료 방지)
        os._exit(0)
    except:
        sys.exit(0)

async def status_update():
    """주기적인 상태 업데이트 (60초마다)"""
    interval = 60
    counter = 0
    while not shutdown_requested:
        await asyncio.sleep(1)
        counter += 1
        if counter >= interval:
            counter = 0
            try:
                # 시스템 상태 업데이트
                db.update_system_status("RUNNING", "텔레그램 봇 정상 실행 중")
                logger.log_system(f"상태 업데이트: 텔레그램 봇 정상 실행 중 ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})")
                
                # 봇 실행 상태 확인 및 필요시 재설정
                if not telegram_bot_handler.bot_running:
                    logger.log_system("봇 실행 상태가 False로 설정되어 있어 True로 재설정합니다.", level="WARNING")
                    telegram_bot_handler.bot_running = True
                    
                # 세션 확인 및 필요시 재생성
                if telegram_bot_handler._session is None or telegram_bot_handler._session.closed:
                    logger.log_system("봇 세션이 없거나 닫혀 있어 재생성합니다.", level="WARNING")
                    try:
                        telegram_bot_handler._session = aiohttp.ClientSession()
                        logger.log_system("새 aiohttp 세션 생성 완료")
                    except Exception as e:
                        logger.log_error(e, "세션 재생성 중 오류 발생")
            except Exception as e:
                logger.log_error(e, "상태 업데이트 중 오류")

async def main():
    """메인 함수"""
    global shutdown_requested
    
    print("=== 텔레그램 봇 시작 ===")
    
    # 이미 실행 중인 봇 체크 및 처리
    if check_bot_running():
        print("이미 실행 중인 텔레그램 봇이 감지되었습니다.")
        sys.exit(1)
    
    # 락 파일 생성
    create_lock_file()
    
    try:
        # 웹훅 초기화 먼저 진행 (충돌 방지)
        await reset_telegram_webhook()
        
        # 시스템 상태 업데이트
        db.update_system_status("RUNNING", "텔레그램 봇 시작됨")
        
        # 봇 핸들러에 종료 콜백 설정
        telegram_bot_handler.set_shutdown_callback(shutdown)
        
        # 봇 실행 상태를 명시적으로 True로 설정
        telegram_bot_handler.bot_running = True
        
        # 초기화 재시도 로직
        init_retries = 3
        init_success = False
        
        for attempt in range(init_retries):
            try:
                print(f"텔레그램 봇 초기화 시도 #{attempt+1}...")
                
                # 세션 정리 - 안전하게 새로 시작
                if hasattr(telegram_bot_handler, '_session') and telegram_bot_handler._session:
                    if not telegram_bot_handler._session.closed:
                        try:
                            await telegram_bot_handler._session.close()
                            print("이전 세션 정리 완료")
                        except Exception as e:
                            print(f"이전 세션 정리 중 오류: {e}")
                
                # ready_event 초기화
                telegram_bot_handler.ready_event = asyncio.Event()
                
                # 봇 상태 초기화 - 실행 중임을 명시
                telegram_bot_handler.bot_running = True
                
                # 봇이 이미 재시도 로직에서 중지되었는지 확인
                if shutdown_requested:
                    print("종료 요청이 감지되었습니다. 초기화를 중단합니다.")
                    break
                
                # 폴링 시작 (별도 태스크)
                polling_task = asyncio.create_task(telegram_bot_handler.start_polling())
                
                # 최대 10초 동안 봇이 준비될 때까지 대기
                try:
                    await asyncio.wait_for(telegram_bot_handler.ready_event.wait(), timeout=10)
                    print("텔레그램 봇 초기화 완료!")
                    init_success = True
                    break
                except asyncio.TimeoutError:
                    print("텔레그램 봇 초기화 시간 초과")
                    # 봇 상태 재설정
                    telegram_bot_handler.bot_running = False
                    continue
                    
            except Exception as e:
                print(f"텔레그램 봇 초기화 오류: {e}")
                import traceback
                traceback.print_exc()
                await asyncio.sleep(2)  # 재시도 전 대기
        
        if not init_success:
            print("텔레그램 봇 초기화에 실패했습니다. 프로그램을 종료합니다.")
            remove_lock_file()
            sys.exit(1)
            
        # 다시 봇이 실행 중임을 명시적으로 설정
        telegram_bot_handler.bot_running = True
            
        # 주기적인 상태 업데이트 태스크 시작
        status_task = asyncio.create_task(status_update())
        
        # 초기 상태 메시지 전송
        try:
            await telegram_bot_handler._send_message("📡 <b>텔레그램 봇이 성공적으로 시작되었습니다.</b>\n\n/help 명령어로 사용 가능한 명령어를 확인하세요.")
            print("초기 상태 메시지 전송 성공")
        except Exception as e:
            print(f"초기 상태 메시지 전송 실패: {e}")
        
        try:
            # 상태를 주기적으로 모니터링하며 대기
            while not shutdown_requested:
                if not telegram_bot_handler.bot_running:
                    print("텔레그램 봇 종료 감지. 프로그램을 종료합니다.")
                    break
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            print("키보드 인터럽트 감지. 프로그램을 종료합니다.")
            shutdown_requested = True
        finally:
            # 태스크 취소
            if 'status_task' in locals() and not status_task.done():
                status_task.cancel()
            if 'polling_task' in locals() and not polling_task.done():
                polling_task.cancel()
            
            # 봇 종료 처리
            telegram_bot_handler.bot_running = False
            
            # 락 파일 제거
            remove_lock_file()
            
            # 수동 종료 프로세스 실행
            await shutdown()
            
    except Exception as e:
        print(f"텔레그램 봇 실행 중 오류: {e}")
        import traceback
        traceback.print_exc()
    finally:
        # 추가 정리 작업
        try:
            if hasattr(telegram_bot_handler, '_session') and telegram_bot_handler._session:
                if not telegram_bot_handler._session.closed:
                    await telegram_bot_handler._session.close()
            remove_lock_file()
        except Exception as e:
            print(f"정리 중 오류: {e}")
        
        print("=== 텔레그램 봇 종료 ===")
        
        # 프로그램 강제 종료
        sys.exit(0)

if __name__ == "__main__":
    try:
        exit_code = asyncio.run(main())
        # 정상 종료
        print("프로그램이 정상적으로 종료되었습니다.")
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("사용자에 의해 중단됨")
        remove_lock_file()  # 종료 시 락 파일 제거
        sys.exit(0)
    except Exception as e:
        print(f"오류 발생: {str(e)}")
        remove_lock_file()  # 종료 시 락 파일 제거
        sys.exit(1) 