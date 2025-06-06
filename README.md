# 주식 자동매매 봇 (한국투자증권 API)

한국투자증권 API를 활용한 실시간 주식 자동매매 프로그램입니다.

## 주요 기능

- 실시간 시세 수신 (WebSocket)
- 초단기 스캘핑 전략
- 리스크 관리 (손절/익절)
- 포지션 자동 관리
- 실시간 알림 (텔레그램/이메일)
- 거래 로깅 및 성과 분석
- 데이터베이스 백업

## 시작하기

### 1. 요구사항

- Python 3.9+
- 한국투자증권 API 키
- 한국투자증권 계좌

### 2. 설치

```bash
# 클론
cd D:\GIT\autoStockTrading\trading_bot

# 가상환경 생성 및 활성화
python -m venv venv
venv\Scripts\activate  # Windows
# source venv/bin/activate  # Linux/Mac

# 패키지 설치
pip install -r requirements.txt
```

### 3. 환경 설정

1. `.env.example` 파일을 `.env`로 복사
2. `.env` 파일에 필요한 정보 입력:
   - 한국투자증권 API 키
   - 계좌번호
   - 알림 설정 (선택사항)

### 4. 실행

```bash
python main.py
```

## 프로젝트 구조

```
trading_bot/
├── config/          # 설정 파일
├── core/            # 핵심 모듈 (API, WebSocket, 주문관리)
├── strategies/      # 트레이딩 전략
├── utils/           # 유틸리티 (로깅, DB)
├── monitoring/      # 모니터링 및 알림
├── main.py          # 메인 실행 파일
└── requirements.txt
```

## 주요 설정

`config/settings.py`에서 다음 항목들을 설정할 수 있습니다:

- 거래 시간
- 포지션 크기 제한
- 손절/익절 수준
- 스캘핑 전략 파라미터

## 전략

### 초단기 스캘핑 전략
- 실시간 가격/거래량 분석
- 모멘텀 기반 진입
- 시간/가격 기반 청산
- 리스크 관리 자동화

## 안전 장치

- 최대 포지션 크기 제한
- 손절선 자동 설정
- 일일 손실 한도
- 긴급 청산 기능
- 장외 시간 거래 차단

## 모니터링

- 실시간 텔레그램 알림
- 이메일 알림
- 일일 거래 리포트
- 에러 알림

## 데이터베이스

SQLite를 사용하여 다음 데이터를 저장:
- 주문 내역
- 포지션 정보
- 거래 기록
- 성과 분석
- 시스템 상태

## 주의사항

1. **실제 계좌에서 사용 시 주의**: 실제 자금으로 거래되므로 충분한 테스트 후 사용하세요.
2. **시장 상황 모니터링**: 자동매매 중에도 정기적으로 시장 상황을 확인하세요.
3. **리스크 관리**: 적절한 손절선과 포지션 크기를 설정하세요.
4. **API 한도**: 한국투자증권 API 호출 제한을 준수하세요.

## 라이센스

This project is licensed under the MIT License.

## 면책 조항

이 프로그램은 교육 및 연구 목적으로 제공됩니다. 실제 투자에 사용 시 발생하는 모든 손실에 대한 책임은 사용자에게 있습니다.
