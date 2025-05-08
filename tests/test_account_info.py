"""
계좌 정보 테스트 스크립트
"""
import sys
import os
import asyncio
import json
from datetime import datetime

# 프로젝트 루트 디렉토리를 Path에 추가
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.api_client import api_client

async def test_account_info():
    """계좌 정보 확인 테스트"""
    print("==== 계좌 정보 조회 테스트 ====")
    
    # 1. 토큰 발급 확인
    token_valid = await api_client.is_token_valid()
    if not token_valid:
        print("토큰이 유효하지 않아 새로 발급합니다...")
        await api_client.ensure_token()
    else:
        print("토큰이 유효합니다. 기존 토큰 사용")
    
    # 2. 계좌 정보 조회
    account_info = await api_client.get_account_info()
    
    # 3. 계좌 정보 출력
    print("\n== 계좌 정보 응답 코드 ==")
    print(f"결과코드: {account_info.get('rt_cd', 'N/A')}")
    print(f"메시지: {account_info.get('msg1', 'N/A')}")
    
    # 4. 상세 정보 출력
    output = account_info.get('output', {})
    if output:
        print("\n== 계좌 상세 정보 ==")
        output_formatted = json.dumps(output, indent=2, ensure_ascii=False)
        print(output_formatted)
        
        # 5. 주요 필드 확인
        print("\n== 주요 계좌 필드 ==")
        # 주요 필드 목록 (api_client.py 코드 참고)
        key_fields = [
            "dnca_tot_amt",        # 예수금총금액
            "nxdy_excc_amt",       # 익일정산금액
            "prvs_rcdl_excc_amt",  # 가수도정산금액
            "cma_evlu_amt",        # CMA평가금액
            "tot_loan_amt",        # 총대출금액
            "scts_evlu_amt",       # 유가증권평가금액
            "tot_evlu_amt",        # 총평가금액
            "pchs_avg_pric",       # 매입평균가격
            "evlu_pfls_rt",        # 평가손익율
            "evlu_amt",            # 평가금액
            "evlu_pfls_amt",       # 평가손익금액
        ]
        
        for field in key_fields:
            if field in output:
                print(f"{field}: {output.get(field, 'N/A')}")
    
    # 6. 응답 처리 예제
    print("\n== 응답 처리 예제 ==")
    if account_info.get("rt_cd") == "0":
        deposit_value = float(output.get("dnca_tot_amt", "0"))
        securities_value = float(output.get("scts_evlu_amt", "0"))
        total_value = float(output.get("tot_evlu_amt", "0"))
        
        print(f"예수금: {int(deposit_value):,}원")
        print(f"유가증권 평가금액: {int(securities_value):,}원")
        print(f"총 평가금액: {int(total_value):,}원")
    else:
        print(f"API 호출 실패: {account_info.get('msg1', '알 수 없는 오류')}")

if __name__ == "__main__":
    asyncio.run(test_account_info())
