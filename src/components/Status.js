import React, { useEffect, useState } from 'react';

function Status() {
  const [status, setStatus] = useState(null);

  useEffect(() => {
    fetch('/api/status')
      .then(res => res.json())
      .then(data => setStatus(data));
  }, []);

  return (
    <div>
      <h1>트레이딩 봇 상태</h1>
      <pre>{JSON.stringify(status, null, 2)}</pre>
    </div>
  );
}

export default Status;
