import React, { useEffect, useState } from 'react';
import './App.css';

function App() {
  const [account, setAccount] = useState(null);
  const [positions, setPositions] = useState([]);
  const [candidates, setCandidates] = useState([]);
  const [loading, setLoading] = useState({
    account: false,
    positions: false,
    candidates: false
  });
  const [error, setError] = useState(null);
  const [backendStatus, setBackendStatus] = useState('unknown');
  
  // 백엔드 API URL
  // Use the absolute URL always to avoid proxy issues
  const API_BASE_URL = window.location.hostname === 'localhost' ? 'http://localhost:5050' : 'http://' + window.location.hostname + ':5050';
  
  const fetchAccount = async () => {
    try {
      setLoading(prev => ({ ...prev, account: true }));
      console.log('계좌 정보 요청 중...', `${API_BASE_URL}/api/account`);
      const res = await fetch(`${API_BASE_URL}/api/account`);
      console.log('계좌 정보 응답:', res.status);
      
      if (!res.ok) {
        throw new Error(`API 오류: ${res.status}`);
      }
      
      const data = await res.json();
      console.log('계좌 정보:', data);
      setAccount(data);
    } catch (e) {
      console.error('계좌 정보 로드 실패:', e);
      setAccount({ status: 'error', message: e.message });
    } finally {
      setLoading(prev => ({ ...prev, account: false }));
    }
  };

  const fetchPositions = async () => {
    try {
      setLoading(prev => ({ ...prev, positions: true }));
      console.log('포지션 정보 요청 중...', `${API_BASE_URL}/api/positions`);
      const res = await fetch(`${API_BASE_URL}/api/positions`);
      console.log('포지션 정보 응답:', res.status);
      
      if (!res.ok) {
        throw new Error(`API 오류: ${res.status}`);
      }
      
      const data = await res.json();
      console.log('포지션 정보:', data);
      setPositions(data);
    } catch (e) {
      console.error('포지션 정보 로드 실패:', e);
      setPositions([]);
    } finally {
      setLoading(prev => ({ ...prev, positions: false }));
    }
  };

  const fetchCandidates = async () => {
    try {
      setLoading(prev => ({ ...prev, candidates: true }));
      console.log('후보 종목 요청 중...', `${API_BASE_URL}/api/candidates`);
      const res = await fetch(`${API_BASE_URL}/api/candidates`);
      console.log('후보 종목 응답:', res.status);
      
      if (!res.ok) {
        throw new Error(`API 오류: ${res.status}`);
      }
      
      const data = await res.json();
      console.log('후보 종목:', data);
      setCandidates(data);
    } catch (e) {
      console.error('후보 종목 로드 실패:', e);
      setCandidates([]);
    } finally {
      setLoading(prev => ({ ...prev, candidates: false }));
    }
  };

  const refreshCandidates = async () => {
    setError(null);
    try {
      setLoading(prev => ({ ...prev, candidates: true }));
      const res = await fetch(`${API_BASE_URL}/api/refresh_candidates`, { 
        method: 'POST',
        headers: {
          'Content-Type': 'application/json'
        }
      });
      
      if (!res.ok) {
        throw new Error(`API 오류: ${res.status}`);
      }
      
      const data = await res.json();
      if (data.candidates) {
        setCandidates(data.candidates);
      } else {
        setError(data.error || '갱신 실패');
      }
    } catch (e) {
      console.error('후보 갱신 실패:', e);
      setError(e.message || '갱신 실패');
    } finally {
      setLoading(prev => ({ ...prev, candidates: false }));
    }
  };

  // 백엔드 API 서버 상태 확인
  const checkAPIServer = async () => {
    try {
      console.log('API 서버 상태 확인 중...', `${API_BASE_URL}`);
      const res = await fetch(`${API_BASE_URL}`);
      console.log('API 서버 응답:', res.status);
      
      if (res.ok) {
        console.log('API 서버가 정상적으로 실행 중입니다.');
        setBackendStatus('connected');
      } else {
        console.error('API 서버 응답 오류:', res.status);
        setBackendStatus('error');
      }
    } catch (e) {
      console.error('API 서버 연결 실패:', e);
      setBackendStatus('disconnected');
    }
  };

  // 초기 데이터 로드
  useEffect(() => {
    // 먼저 API 서버 상태 확인
    checkAPIServer().then(() => {
      // API 서버 확인 후 데이터 로드
      fetchAccount();
      fetchPositions();
      fetchCandidates();
    });
    
    // 30초마다 계좌 정보 자동 갱신
    const intervalId = setInterval(() => {
      fetchAccount();
    }, 30000);
    
    return () => clearInterval(intervalId);
  }, []);

  return (
    <div className="App" style={{ maxWidth: 800, margin: '0 auto', padding: 20 }}>
      <h1>트레이딩 봇 대시보드</h1>
      
      {/* Backend Status Indicator */}
      <div style={{ 
        padding: '8px 16px', 
        borderRadius: '4px', 
        backgroundColor: backendStatus === 'connected' ? '#e6ffe6' : 
                         backendStatus === 'disconnected' ? '#ffe6e6' : 
                         backendStatus === 'error' ? '#fff3cd' : '#f2f2f2',
        marginBottom: '15px'
      }}>
        <p>
          <strong>백엔드 서버 상태:</strong> {
            backendStatus === 'connected' ? '연결됨 ✅' : 
            backendStatus === 'disconnected' ? '연결 안됨 ❌' : 
            backendStatus === 'error' ? '오류 ⚠️' : '확인 중...'
          }
          <button 
            onClick={checkAPIServer} 
            style={{ marginLeft: 10, fontSize: '0.8em', padding: '2px 8px' }}
          >
            다시 확인
          </button>
        </p>
        <p style={{ fontSize: '0.8em', margin: 0 }}>
          서버 URL: {API_BASE_URL}
        </p>
      </div>
      
      <section>
        <h2>계좌 정보
          <button 
            onClick={fetchAccount} 
            disabled={loading.account}
            style={{ marginLeft: 10, fontSize: '0.8em', padding: '2px 8px' }}
          >
            {loading.account ? '로딩 중...' : '새로고침'}
          </button>
        </h2>
        {account ? (
          account.status === 'success' ? (
            <div style={{ background: '#f4f4f4', padding: 15, borderRadius: 5 }}>
              <p><strong>총 자산:</strong> {account.balance.totalAssets.toLocaleString()}원</p>
              <p><strong>예수금:</strong> {account.balance.cashBalance.toLocaleString()}원</p>
              <p><strong>주식 평가금액:</strong> {account.balance.stockValue.toLocaleString()}원</p>
              <p><strong>매수 가능금액:</strong> {account.balance.availableAmount.toLocaleString()}원</p>
              <p style={{ fontSize: '0.8em', color: '#666' }}>
                마지막 업데이트: {new Date(account.timestamp).toLocaleString()}
              </p>
            </div>
          ) : (
            <div style={{ color: 'red', padding: 10 }}>
              <p>계좌 정보 조회 실패: {account.message}</p>
            </div>
          )
        ) : (
          <div style={{ padding: 10 }}>
            {loading.account ? '로딩 중...' : '데이터 없음'}
          </div>
        )}
      </section>
      <section>
        <h2>보유 종목 리스트
          <button 
            onClick={fetchPositions} 
            disabled={loading.positions}
            style={{ marginLeft: 10, fontSize: '0.8em', padding: '2px 8px' }}
          >
            {loading.positions ? '로딩 중...' : '새로고침'}
          </button>
        </h2>
        {loading.positions ? (
          <div>로딩 중...</div>
        ) : (
          <ul>
            {positions && positions.length > 0 ? positions.map((pos, i) => (
              <li key={i}>{pos.symbol} | 수량: {pos.quantity} | 평균가: {pos.avg_price}</li>
            )) : <li>없음</li>}
          </ul>
        )}
      </section>
      <section>
        <h2>매수 후보 리스트
          <button 
            onClick={refreshCandidates} 
            disabled={loading.candidates}
            style={{ marginLeft: 10, fontSize: '0.8em', padding: '2px 8px' }}
          >
            {loading.candidates ? '갱신 중...' : '갱신'}
          </button>
        </h2>
        {error && <div style={{ color: 'red' }}>{error}</div>}
        {loading.candidates ? (
          <div>로딩 중...</div>
        ) : (
          <ul>
            {candidates && candidates.length > 0 ? candidates.map((sym, i) => (
              <li key={i}>{sym}</li>
            )) : <li>없음</li>}
          </ul>
        )}
      </section>
      <footer style={{ marginTop: 30, fontSize: '0.8em', color: '#666', textAlign: 'center' }}>
        <p>© 2024 Trading Bot Dashboard | 서버: {API_BASE_URL}</p>
      </footer>
    </div>
  );
}

export default App;
