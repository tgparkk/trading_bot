"""
시장 거래 시간 유틸리티 모듈
한국 주식시장의 거래 시간을 관리하고 확인하는 기능을 제공합니다.
"""

import datetime
from typing import Optional, Tuple
from config.settings import config

# 한국 시장 거래 시간 기본값
DEFAULT_MARKET_OPEN = datetime.time(9, 0)  # 오전 9시
DEFAULT_MARKET_CLOSE = datetime.time(15, 30)  # 오후 3시 30분

def is_market_open(current_time: Optional[datetime.datetime] = None) -> bool:
    """
    현재 시장이 거래 시간 중인지 확인합니다.
    
    Args:
        current_time: 확인할 시간 (기본값: 현재 시간)
        
    Returns:
        bool: 시장이 열려있으면 True, 아니면 False
    """
    # 현재 시간이 제공되지 않으면 현재 시간 사용
    if current_time is None:
        current_time = datetime.datetime.now()
    
    # 날짜만 확인
    current_date = current_time.date()
    current_time_only = current_time.time()
    
    # 주말 체크 (5: 토요일, 6: 일요일)
    if current_date.weekday() >= 5:
        return False
    
    # 휴장일 체크
    # 여기에 휴장일 목록이나 휴장일 확인 로직을 추가할 수 있음
    holidays = config.get("market_holidays", [])
    current_date_str = current_date.strftime("%Y-%m-%d")
    if current_date_str in holidays:
        return False
    
    # 거래 시간 가져오기
    try:
        market_open = config["trading"]["market_open"]
        market_close = config["trading"]["market_close"]
    except (KeyError, TypeError):
        # 설정에서 가져올 수 없는 경우 기본값 사용
        market_open = DEFAULT_MARKET_OPEN
        market_close = DEFAULT_MARKET_CLOSE
    
    # 거래 시간 내인지 확인
    return market_open <= current_time_only <= market_close

def get_next_market_open(current_time: Optional[datetime.datetime] = None) -> datetime.datetime:
    """
    다음 시장 개장 시간을 계산합니다.
    
    Args:
        current_time: 기준 시간 (기본값: 현재 시간)
        
    Returns:
        datetime.datetime: 다음 시장 개장 시간
    """
    if current_time is None:
        current_time = datetime.datetime.now()
    
    # 거래 시간 가져오기
    try:
        market_open = config["trading"]["market_open"]
        market_close = config["trading"]["market_close"]
    except (KeyError, TypeError):
        market_open = DEFAULT_MARKET_OPEN
        market_close = DEFAULT_MARKET_CLOSE
    
    current_date = current_time.date()
    current_time_only = current_time.time()
    
    # 오늘 장이 열리기 전인 경우 (현재 시간이 오늘 개장 시간보다 이른 경우)
    if current_time_only < market_open:
        return datetime.datetime.combine(current_date, market_open)
    
    # 다음 거래일 찾기
    next_date = current_date + datetime.timedelta(days=1)
    while True:
        # 주말 체크
        if next_date.weekday() >= 5:
            next_date += datetime.timedelta(days=1)
            continue
        
        # 휴장일 체크
        holidays = config.get("market_holidays", [])
        next_date_str = next_date.strftime("%Y-%m-%d")
        if next_date_str in holidays:
            next_date += datetime.timedelta(days=1)
            continue
        
        # 다음 거래일 찾음
        return datetime.datetime.combine(next_date, market_open)

def format_market_time(dt: datetime.datetime) -> str:
    """
    거래 시간을 보기 좋은 형식으로 포맷팅합니다.
    
    Args:
        dt: 포맷팅할 datetime 객체
        
    Returns:
        str: 포맷팅된 시간 문자열 (예: "2023-06-01 09:00:00 (목)")
    """
    weekday_names = ["월", "화", "수", "목", "금", "토", "일"]
    weekday = weekday_names[dt.weekday()]
    
    return f"{dt.strftime('%Y-%m-%d %H:%M:%S')} ({weekday})"

def get_market_status() -> dict:
    """
    현재 시장 상태에 대한 종합 정보를 반환합니다.
    
    Returns:
        dict: 시장 상태 정보를 담은 딕셔너리
    """
    current_time = datetime.datetime.now()
    is_open = is_market_open(current_time)
    next_open = get_next_market_open(current_time)
    
    # 현재 장이 열려있다면, 마감 시간 계산
    if is_open:
        try:
            market_close = config["trading"]["market_close"]
        except (KeyError, TypeError):
            market_close = DEFAULT_MARKET_CLOSE
        
        market_close_dt = datetime.datetime.combine(current_time.date(), market_close)
        time_to_close = (market_close_dt - current_time).total_seconds() / 60  # 분 단위
    else:
        time_to_close = None
    
    # 장이 열려있지 않다면, 다음 개장까지 남은 시간 계산
    if not is_open:
        time_to_open = (next_open - current_time).total_seconds() / 60  # 분 단위
    else:
        time_to_open = None
    
    return {
        "is_market_open": is_open,
        "current_time": current_time.strftime("%Y-%m-%d %H:%M:%S"),
        "next_market_open": format_market_time(next_open),
        "minutes_to_open": time_to_open,
        "minutes_to_close": time_to_close,
        "status": "OPEN" if is_open else "CLOSED"
    } 