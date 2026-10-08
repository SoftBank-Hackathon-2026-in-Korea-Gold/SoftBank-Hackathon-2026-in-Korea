import React, { useState } from 'react';
import { 
  Cloud, Server, Play, CheckCircle2, AlertTriangle, 
  Wrench, Terminal, ExternalLink, Cpu, FolderGit2, RefreshCw 
} from 'lucide-react';

export default function App() {
  const [projectPath, setProjectPath] = useState('./sample_broken_app');
  const [targetMode, setTargetMode] = useState('auto'); // auto, cloud, local
  const [step, setStep] = useState(0); // 0: 대기, 1: 분석, 2: 1차배포(에러), 3: 자가치유, 4: 완료
  const [logs, setLogs] = useState([]);
  const [isDeploying, setIsDeploying] = useState(false);

  // 로그 한 줄씩 추가하는 함수
  const addLog = (msg) => {
    setLogs((prev) => [...prev, `[${new Date().toLocaleTimeString()}] ${msg}`]);
  };

  // One Action 배포 버튼 클릭 시 실행되는 시뮬레이션 (나중에 4번 백엔드 API와 연결할 부분)
  const handleDeploy = () => {
    setIsDeploying(true);
    setLogs([]);
    
    // 1단계: AI 코드 분석
    setStep(1);
    addLog("🔍 AI 아키텍트가 로컬 프로젝트 구조를 스캔하고 있습니다...");
    setTimeout(() => {
      addLog("✅ 프레임워크 감지: FastAPI (Python 3.10)");
      addLog("💡 AI 배포 환경 결정: 하이브리드 배포 (Google Cloud Run + 로컬 Docker)");
      
      // 2단계: 1차 빌드 및 에러 발생
      setStep(2);
      addLog("🐳 생성된 Dockerfile로 1차 컨테이너 빌드를 시작합니다...");
    }, 2000);

    setTimeout(() => {
      addLog("❌ [ERROR] 배포 실패! (Exit Code 1)");
      addLog("🚨 stderr: ModuleNotFoundError: No module named 'uvicorn'");
      addLog("🚨 stderr: Host '127.0.0.1' cannot be reached from external container.");
      
      // 3단계: AI 자가치유 (Self-Healing) 가동
      setStep(3);
      addLog("🛠️ [Self-Healing] AI 자가치유 에이전트가 에러 로그를 분석 중입니다...");
    }, 4500);

    setTimeout(() => {
      addLog("✨ [패치 완료] requirements.txt에 'uvicorn' 패키지 자동 추가");
      addLog("✨ [패치 완료] app.py 호스트를 '0.0.0.0'으로 수정 및 Dockerfile 재생성");
      addLog("🔄 수정된 코드로 2차 재배포를 시도합니다...");
    }, 7500);

    // 4단계: 최종 배포 완료
    setTimeout(() => {
      setStep(4);
      setIsDeploying(false);
      addLog("🎉 [SUCCESS] 로컬 온프레미스(Docker) 및 Google Cloud Run 배포 완료!");
    }, 10000);
  };

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 p-8 font-sans">
      {/* 상단 헤더 */}
      <header className="max-w-6xl mx-auto mb-8 flex items-center justify-between border-b border-slate-800 pb-5">
        <div className="flex items-center gap-3">
          <div className="bg-indigo-600 p-2.5 rounded-xl shadow-lg shadow-indigo-500/30">
            <Cloud className="w-8 h-8 text-white" />
          </div>
          <div>
            <h1 className="text-2xl font-bold bg-gradient-to-r from-white to-slate-400 bg-clip-text text-transparent">
              AutoCloudPilot
            </h1>
            <p className="text-sm text-slate-400">Theme: One Action, Infinite Clouds — AI 자가치유 멀티 배포 시스템</p>
          </div>
        </div>
        <span className="px-3 py-1 bg-indigo-500/10 border border-indigo-500/30 text-indigo-400 rounded-full text-xs font-semibold">
          LangGraph Agent Ready
        </span>
      </header>

      <main className="max-w-6xl mx-auto space-y-6">
        {/* 1. 컨트롤 패널 (입력창 & One Action 버튼) */}
        <section className="bg-slate-900 border border-slate-800 rounded-2xl p-6 shadow-xl">
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4 items-end">
            <div>
              <label className="block text-xs font-medium text-slate-400 mb-2 flex items-center gap-1.5">
                <FolderGit2 className="w-4 h-4 text-indigo-400" /> 배포할 로컬 프로젝트 경로
              </label>
              <input 
                type="text" 
                value={projectPath}
                onChange={(e) => setProjectPath(e.target.value)}
                className="w-full bg-slate-950 border border-slate-700 rounded-xl px-4 py-3 text-sm focus:outline-none focus:border-indigo-500"
              />
            </div>

            <div>
              <label className="block text-xs font-medium text-slate-400 mb-2 flex items-center gap-1.5">
                <Cpu className="w-4 h-4 text-indigo-400" /> 배포 타겟 환경 설정
              </label>
              <select 
                value={targetMode}
                onChange={(e) => setTargetMode(e.target.value)}
                className="w-full bg-slate-950 border border-slate-700 rounded-xl px-4 py-3 text-sm focus:outline-none focus:border-indigo-500"
              >
                <option value="auto">🤖 AI 자동 분석 및 결정 (권장)</option>
                <option value="cloud">☁️ 퍼블릭 클라우드 (Google Cloud Run)</option>
                <option value="local">🖥️ 온프레미스 (로컬 PC Docker)</option>
              </select>
            </div>

            <button 
              onClick={handleDeploy}
              disabled={isDeploying}
              className="w-full bg-indigo-600 hover:bg-indigo-500 disabled:bg-slate-800 text-white font-semibold py-3 px-6 rounded-xl shadow-lg shadow-indigo-600/30 transition flex items-center justify-center gap-2 cursor-pointer"
            >
              {isDeploying ? (
                <><RefreshCw className="w-5 h-5 animate-spin" /> AI 에이전트 작업 중...</>
              ) : (
                <><Play className="w-5 h-5 fill-current" /> One Action Deploy</>
              )}
            </button>
          </div>
        </section>

        {/* 2. 시각적 파이프라인 (4단계 상태 카드) */}
        <section className="grid grid-cols-1 md:grid-cols-4 gap-4">
          {/* Step 1 */}
          <div className={`p-5 rounded-2xl border transition-all ${step >= 1 ? 'bg-slate-900 border-indigo-500/50 shadow-lg shadow-indigo-500/10' : 'bg-slate-900/40 border-slate-800 opacity-50'}`}>
            <div className="flex items-center justify-between mb-3">
              <span className="text-xs font-bold text-indigo-400">STEP 1</span>
              <Cpu className={`w-5 h-5 ${step === 1 ? 'text-indigo-400 animate-pulse' : 'text-slate-500'}`} />
            </div>
            <h3 className="font-bold text-base mb-1">코드 분석 & 환경 결정</h3>
            <p className="text-xs text-slate-400">프레임워크 파악 및 최적의 클라우드/온프레미스 타겟 선정</p>
          </div>

          {/* Step 2 */}
          <div className={`p-5 rounded-2xl border transition-all ${step === 2 ? 'bg-red-950/30 border-red-500' : step > 2 ? 'bg-slate-900 border-red-500/40' : 'bg-slate-900/40 border-slate-800 opacity-50'}`}>
            <div className="flex items-center justify-between mb-3">
              <span className="text-xs font-bold text-red-400">STEP 2</span>
              <AlertTriangle className={`w-5 h-5 ${step >= 2 ? 'text-red-400 animate-bounce' : 'text-slate-500'}`} />
            </div>
            <h3 className="font-bold text-base mb-1">1차 배포 및 에러 감지</h3>
            <p className="text-xs text-slate-400">컨테이너 빌드 실행 및 런타임 에러 로그(stderr) 실시간 포착</p>
          </div>

          {/* Step 3 */}
          <div className={`p-5 rounded-2xl border transition-all ${step === 3 ? 'bg-amber-950/30 border-amber-500 shadow-lg shadow-amber-500/10' : step > 3 ? 'bg-slate-900 border-amber-500/40' : 'bg-slate-900/40 border-slate-800 opacity-50'}`}>
            <div className="flex items-center justify-between mb-3">
              <span className="text-xs font-bold text-amber-400">STEP 3 (KILLER)</span>
              <Wrench className={`w-5 h-5 ${step === 3 ? 'text-amber-400 animate-spin' : 'text-slate-500'}`} />
            </div>
            <h3 className="font-bold text-base mb-1">AI 자가치유 (Self-Healing)</h3>
            <p className="text-xs text-slate-400">LLM이 에러 원인을 분석해 소스코드 및 Dockerfile 자동 패치</p>
          </div>

          {/* Step 4 */}
          <div className={`p-5 rounded-2xl border transition-all ${step === 4 ? 'bg-emerald-950/30 border-emerald-500 shadow-lg shadow-emerald-500/10' : 'bg-slate-900/40 border-slate-800 opacity-50'}`}>
            <div className="flex items-center justify-between mb-3">
              <span className="text-xs font-bold text-emerald-400">STEP 4</span>
              <CheckCircle2 className={`w-5 h-5 ${step === 4 ? 'text-emerald-400' : 'text-slate-500'}`} />
            </div>
            <h3 className="font-bold text-base mb-1">멀티 환경 배포 완료</h3>
            <p className="text-xs text-slate-400">수리된 컨테이너를 클라우드 및 온프레미스에 무중단 배포</p>
          </div>
        </section>

        {/* 3. 하단 상세 영역: 왼쪽(실시간 터미널 로그) / 오른쪽(AI 수리 리포트 & 배포 결과) */}
        <section className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          {/* 실시간 터미널 창 */}
          <div className="bg-slate-900 border border-slate-800 rounded-2xl p-5 flex flex-col h-96">
            <div className="flex items-center gap-2 text-xs font-semibold text-slate-400 mb-3 border-b border-slate-800 pb-3">
              <Terminal className="w-4 h-4 text-indigo-400" /> 실시간 에이전트 & 배포 터미널 로그
            </div>
            <div className="flex-1 bg-slate-950 rounded-xl p-4 font-mono text-xs overflow-y-auto space-y-2">
              {logs.length === 0 ? (
                <p className="text-slate-600">상단의 'One Action Deploy' 버튼을 누르면 AI 파이프라인이 시작됩니다...</p>
              ) : (
                logs.map((log, index) => (
                  <div key={index} className={
                    log.includes('ERROR') || log.includes('stderr') ? 'text-red-400' :
                    log.includes('패치 완료') || log.includes('Self-Healing') ? 'text-amber-300' :
                    log.includes('SUCCESS') ? 'text-emerald-400 font-bold' : 'text-slate-300'
                  }>
                    {log}
                  </div>
                ))
              )}
            </div>
          </div>

          {/* AI 자가치유 코드 비교 & 배포 URL 카드 */}
          <div className="bg-slate-900 border border-slate-800 rounded-2xl p-5 flex flex-col justify-between h-96">
            <div>
              <h3 className="text-sm font-bold text-slate-200 mb-3 flex items-center gap-2">
                <Wrench className="w-4 h-4 text-amber-400" /> AI 자가치유(Self-Healing) 패치 내역
              </h3>
              {step >= 3 ? (
                <div className="bg-slate-950 border border-slate-800 rounded-xl p-4 font-mono text-xs space-y-1">
                  <div className="text-slate-500"># requirements.txt & app.py 자동 수정 비교 (Diff)</div>
                  <div className="text-red-400">- uvicorn.run(app, host="127.0.0.1", port=8000)</div>
                  <div className="text-emerald-400">+ uvicorn.run(app, host="0.0.0.0", port=8080)</div>
                  <div className="text-emerald-400">+ requirements.txt: 'uvicorn' 패키지 누락분 자동 추가</div>
                </div>
              ) : (
                <div className="bg-slate-950 border border-slate-800 rounded-xl p-8 text-center text-xs text-slate-600">
                  배포 중 에러가 감지되면 AI가 수정한 코드 비교(Diff) 내역이 이곳에 표시됩니다.
                </div>
              )}
            </div>

            {/* 최종 배포 완료 시 나타나는 접속 링크 박스 */}
            <div className="mt-4">
              <h3 className="text-sm font-bold text-slate-200 mb-3">🌐 배포 완료된 엔드포인트 (Infinite Clouds)</h3>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <a 
                  href={step === 4 ? "https://cloud.google.com/run" : "#"} 
                  target="_blank" 
                  rel="noreferrer"
                  className={`p-3.5 rounded-xl border flex items-center justify-between transition ${step === 4 ? 'bg-indigo-950/40 border-indigo-500/50 hover:bg-indigo-900/40 cursor-pointer' : 'bg-slate-950 border-slate-800 opacity-40 pointer-events-none'}`}
                >
                  <div className="flex items-center gap-2.5">
                    <Cloud className="w-5 h-5 text-indigo-400" />
                    <div>
                      <div className="text-xs font-bold">Google Cloud Run</div>
                      <div className="text-[11px] text-slate-400">https://autocloud-xyz.a.run.app</div>
                    </div>
                  </div>
                  <ExternalLink className="w-4 h-4 text-slate-400" />
                </a>

                <a 
                  href={step === 4 ? "http://localhost:8000" : "#"} 
                  target="_blank" 
                  rel="noreferrer"
                  className={`p-3.5 rounded-xl border flex items-center justify-between transition ${step === 4 ? 'bg-emerald-950/40 border-emerald-500/50 hover:bg-emerald-900/40 cursor-pointer' : 'bg-slate-950 border-slate-800 opacity-40 pointer-events-none'}`}
                >
                  <div className="flex items-center gap-2.5">
                    <Server className="w-5 h-5 text-emerald-400" />
                    <div>
                      <div className="text-xs font-bold">On-Premise (로컬 PC)</div>
                      <div className="text-[11px] text-slate-400">http://localhost:8000</div>
                    </div>
                  </div>
                  <ExternalLink className="w-4 h-4 text-slate-400" />
                </a>
              </div>
            </div>
          </div>
        </section>
      </main>
    </div>
  );
}