#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Trading Bot - 백엔드 API 테스트 유틸리티
API 엔드포인트가 올바르게 작동하는지 테스트합니다.
"""

import requests
import sys
import time
import json
from colorama import init, Fore, Style

# 색상 초기화
init()

# 백엔드 API URL
API_BASE_URL = "http://localhost:5050"

def print_header(text):
    """헤더 형식으로 텍스트 출력"""
    print(f"\n{Fore.CYAN}{Style.BRIGHT}{'=' * 60}")
    print(f" {text}")
    print(f"{'=' * 60}{Style.RESET_ALL}")

def print_success(text):
    """성공 메시지 출력"""
    print(f"{Fore.GREEN}✓ {text}{Style.RESET_ALL}")

def print_error(text):
    """오류 메시지 출력"""
    print(f"{Fore.RED}✗ {text}{Style.RESET_ALL}")

def print_info(text):
    """정보 메시지 출력"""
    print(f"{Fore.YELLOW}ℹ {text}{Style.RESET_ALL}")

def test_endpoint(endpoint_path, expected_status=200, method="GET", data=None, description=None):
    """
    API 엔드포인트를 테스트하고 결과를 반환합니다.
    
    Args:
        endpoint_path: API 엔드포인트 경로 (예: "/api/status")
        expected_status: 예상되는 HTTP 상태 코드
        method: HTTP 메서드 (GET, POST 등)
        data: POST 요청의 경우 전송할 데이터
        description: 테스트 설명
    
    Returns:
        (성공 여부, 응답 객체 또는 None)
    """
    url = f"{API_BASE_URL}{endpoint_path}"
    description = description or f"{method} {endpoint_path}"
    
    try:
        if method.upper() == "GET":
            response = requests.get(url, timeout=5)
        elif method.upper() == "POST":
            headers = {"Content-Type": "application/json"}
            response = requests.post(url, data=json.dumps(data) if data else None, 
                                    headers=headers, timeout=5)
        else:
            print_error(f"지원되지 않는 HTTP 메서드: {method}")
            return False, None
        
        if response.status_code == expected_status:
            print_success(f"{description} - 상태: {response.status_code}")
            return True, response
        else:
            print_error(f"{description} - 상태: {response.status_code} (예상: {expected_status})")
            print_info(f"응답: {response.text[:200]}...")
            return False, response
            
    except requests.exceptions.ConnectionError:
        print_error(f"{description} - 연결 실패. 백엔드 서버가 실행 중인지 확인하세요.")
        return False, None
    except requests.exceptions.Timeout:
        print_error(f"{description} - 요청 시간 초과")
        return False, None
    except Exception as e:
        print_error(f"{description} - 예외 발생: {str(e)}")
        return False, None

def main():
    """메인 테스트 함수"""
    print_header("트레이딩 봇 백엔드 API 테스트")
    print(f"백엔드 URL: {API_BASE_URL}")
    
    # 백엔드 서버 연결 테스트
    print_info("백엔드 서버 연결 테스트 중...")
    success, _ = test_endpoint("/", description="루트 엔드포인트 (기본 연결)")
    
    if not success:
        retry = input("백엔드 서버에 연결할 수 없습니다. 다시 시도하시겠습니까? (y/n): ")
        if retry.lower() != 'y':
            sys.exit(1)
    
    # API 상태 확인
    test_endpoint("/api/status", description="API 상태 확인")
    
    # 계좌 정보 확인
    test_endpoint("/api/account", description="계좌 정보 API")
    
    # 포지션 정보 확인
    test_endpoint("/api/positions", description="포지션 정보 API")
    
    # 후보 종목 확인
    test_endpoint("/api/candidates", description="후보 종목 API")
    
    # 후보 종목 갱신 (POST 요청)
    test_endpoint("/api/refresh_candidates", method="POST", description="후보 종목 갱신 API")
    
    # 텔레그램 메시지 전송 테스트 (선택적)
    test_data = {"message": "API 테스트 메시지"}
    test_endpoint("/api/send_telegram", method="POST", data=test_data, 
                 description="텔레그램 메시지 전송 API")
    
    # KIS API 연결 테스트
    test_endpoint("/api/test_kis_connection", description="KIS API 연결 테스트")
    
    print_header("테스트 완료")

if __name__ == "__main__":
    main() 