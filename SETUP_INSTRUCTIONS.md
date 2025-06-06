Instructions for running the application

# 트레이딩 봇 대시보드 설정 가이드

## 문제 해결: 404 에러 (API 엔드포인트 연결 실패)

프론트엔드(`localhost:3000`)에서 백엔드 API(`localhost:5050`)에 연결하지 못하는 문제를 해결하기 위한 가이드입니다.

### 변경된 파일

다음 파일들이 수정되었습니다:

1. `frontend/src/App.js` - API 호출 방식 개선 및 백엔드 상태 표시 추가
2. `backend/app.py` - CORS 설정 개선 (모든 경로에 대해 허용)
3. `start_dashboard_fixed.bat` - 백엔드와 프론트엔드를 올바르게 시작하는 새 배치 파일
4. `test_backend_api.py` - 백엔드 API 테스트 도구

### 해결 방법

1. **첫 번째 단계**: 백엔드 서버 작동 확인

   ```
   cd backend
   python app.py
   ```

   백엔드 서버가 `http://localhost:5050`에서 실행되는지 확인하세요.

2. **두 번째 단계**: 백엔드 API 테스트

   새 터미널을 열고 다음 명령을 실행하세요:

   ```
   python test_backend_api.py
   ```

   이 스크립트는 모든 필요한 API 엔드포인트가 올바르게 응답하는지 테스트합니다.

3. **세 번째 단계**: 새로운 배치 파일로 애플리케이션 시작

   ```
   start_dashboard_fixed.bat
   ```

   이 배치 파일은 백엔드와 프론트엔드를 순차적으로 올바르게 시작합니다.

### 수정된 내용 설명

1. **프론트엔드 수정 (App.js)**:
   - 하드코딩된 URL 대신 동적 API_BASE_URL 사용
   - 백엔드 서버 상태 표시 UI 추가
   - API 요청 디버그 로그 개선

2. **백엔드 수정 (app.py)**:
   - CORS 설정을 모든 경로(`/*`)에 적용
   - `supports_credentials=True` 추가

### 문제가 지속되는 경우

만약 위 해결책으로도 문제가 해결되지 않는다면:

1. **방화벽 설정 확인**: 포트 5050이 로컬에서 차단되지 않았는지 확인
2. **프록시 설정 확인**: `frontend/package.json`의 프록시 설정 확인
3. **브라우저 개발자 도구**: 네트워크 탭에서 API 요청 실패 원인 확인
4. **다른 포트 시도**: 백엔드 서버 포트를 변경(예: 8000)하고 프론트엔드 설정도 업데이트

### 추가 문제 해결 방법

1. **React 개발 서버를 함께 실행**:

   프론트엔드 디렉토리에서:
   ```
   npm run dev
   ```
   
   이 명령은 `package.json`의 `dev` 스크립트를 사용하여 백엔드와 프론트엔드를 동시에 실행합니다.

2. **브라우저 캐시 정리**: 개발자 도구에서 "애플리케이션" > "저장소" > "캐시 저장소" 삭제 후 새로고침
