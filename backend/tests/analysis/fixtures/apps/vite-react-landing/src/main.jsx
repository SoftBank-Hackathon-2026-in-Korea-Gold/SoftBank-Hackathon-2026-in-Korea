import { StrictMode, useState } from 'react'
import { createRoot } from 'react-dom/client'

function App() {
  const [count, setCount] = useState(0)
  return (
    <main style={{ fontFamily: 'sans-serif', textAlign: 'center', padding: '4rem 1rem' }}>
      <h1>우리 서비스를 소개합니다</h1>
      <p>간단하고 빠른 랜딩 페이지 예제입니다.</p>
      <button onClick={() => setCount((c) => c + 1)}>좋아요 {count}</button>
    </main>
  )
}

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
