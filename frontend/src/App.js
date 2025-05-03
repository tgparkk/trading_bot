import React, { useEffect, useState } from 'react';
import './App.css';

function App() {
  const [account, setAccount] = useState(null);
  const [positions, setPositions] = useState([]);
  const [candidates, setCandidates] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const fetchAccount = async () => {
    try {
      const res = await fetch('/api/account');
      const data = await res.json();
      setAccount(data);
    } catch (e) {
      setAccount(null);
    }
  };

  const fetchPositions = async () => {
    try {
      const res = await fetch('/api/positions');
      const data = await res.json();
      setPositions(data);
    } catch (e) {
      setPositions([]);
    }
  };

  const fetchCandidates = async () => {
    try {
      const res = await fetch('/api/candidates');
      const data = await res.json();
      setCandidates(data);
    } catch (e) {
      setCandidates([]);
    }
  };

  const refreshCandidates = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch('/api/refresh_candidates', { method: 'POST' });
      const data = await res.json();
      if (data.candidates) {
        setCandidates(data.candidates);
      } else {
        setError(data.error || '갱신 실패');
      }
    } catch (e) {
      setError('갱신 실패');
    }
    setLoading(false);
  };

  useEffect(() => {
    fetchAccount();
    fetchPositions();
    fetchCandidates();
  }, []);

  return (
    <div className="App" style={{ maxWidth: 800, margin: '0 auto', padding: 20 }}>
      <h1>트레이딩 봇 대시보드</h1>
      <section>
        <h2>계좌 정보</h2>
        <pre style={{ background: '#f4f4f4', padding: 10 }}>
          {account ? JSON.stringify(account, null, 2) : '로딩 중...'}
        </pre>
      </section>
      <section>
        <h2>보유 종목 리스트</h2>
        <ul>
          {positions && positions.length > 0 ? positions.map((pos, i) => (
            <li key={i}>{pos.symbol} | 수량: {pos.quantity} | 평균가: {pos.avg_price}</li>
          )) : <li>없음</li>}
        </ul>
      </section>
      <section>
        <h2>매수 후보 리스트
          <button onClick={refreshCandidates} disabled={loading} style={{ marginLeft: 10 }}>
            {loading ? '갱신 중...' : '갱신'}
          </button>
        </h2>
        {error && <div style={{ color: 'red' }}>{error}</div>}
        <ul>
          {candidates && candidates.length > 0 ? candidates.map((sym, i) => (
            <li key={i}>{sym}</li>
          )) : <li>없음</li>}
        </ul>
      </section>
    </div>
  );
}

export default App;
