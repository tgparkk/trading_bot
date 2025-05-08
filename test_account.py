#!/usr/bin/env python
# -*- coding: utf-8 -*-

from core.api_client import api_client
import json

def main():
    print("=== 계좌 정보 조회 테스트 ===")
    
    # 토큰 재발급
    try:
        api_client._get_access_token()
        print("토큰이 정상적으로 발급되었습니다.")
    except Exception as e:
        print(f"토큰 발급 오류: {e}")
    
    # 계좌 정보 조회
    result = api_client.get_account_balance()
    
    # 결과 출력
    print("\n=== 응답 결과 ===")
    print(f"응답 코드: {result.get('rt_cd', 'N/A')}")
    print(f"응답 메시지: {result.get('msg1', 'N/A')}")
    
    # 출력 데이터 처리
    print("\n=== 응답 데이터 ===")
    if result.get("rt_cd") == "0":
        output = result.get("output", {})
        if output:
            print(json.dumps(output, indent=2, ensure_ascii=False))
            
            # 주요 필드 값 출력
            print("\n=== 주요 필드 ===")
            print(f"예수금: {output.get('dnca_tot_amt', 'N/A')}")
            print(f"주식평가금액: {output.get('scts_evlu_amt', 'N/A')}")
            print(f"총평가금액: {output.get('tot_evlu_amt', 'N/A')}")
        else:
            print("응답 데이터가 없습니다.")
    else:
        print(f"API 오류: {result.get('msg1', 'N/A')}")

if __name__ == "__main__":
    main() 