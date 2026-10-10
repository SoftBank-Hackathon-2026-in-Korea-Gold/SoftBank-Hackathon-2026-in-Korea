# 3D 시각화 가이드

대시보드에 붙는 React Three Fiber 컴포넌트 3종의 역할, props, 설치, 연동 방법을 정리합니다.
세 컴포넌트 모두 `frontend/src/components/3d/`에 있고, 상태 판정·레이아웃 계산은 `frontend/src/lib/`의 순수 함수(vitest로 테스트)로 분리되어 있습니다.

| 컴포넌트 | 보여주는 것 | 순수 로직 |
| --- | --- | --- |
| `PipelineJourney` | 소스코드 큐브가 SSE 단계(분석 → 배포 → 자가치유 → 공개 URL)를 따라 트랙 위를 이동 | `lib/pipelineJourney.js` |
| `InstancedFleetGrid` | 노드 풀의 서버 랙을 Drei `<Instances>`로 그린 단일 드로우콜 그리드 | `lib/fleetStatus.js` |
| `VictoryCelebration` | 자가치유 성공 시 뜨는 3D 축하 무대(스포트라이트 + Sparkles + 댄서) | `lib/victory.js` |
| `SceneBoundary` | WebGL·런타임 오류가 2D 대시보드로 번지지 않게 막는 에러 경계 | — |

## 컴포넌트와 props

### PipelineJourney

```jsx
<PipelineJourney run={run} height={220} className="" />
```

- `run`: `lib/run.js`의 run 객체(Stepper와 같은 것). SSE를 다시 파싱하지 않고 `stepStatus(run)`에서 파생합니다.
- 큐브 위치: `idle → analyze → deploy → (heal) → live`. `heal`은 deploy와 live 사이에서 갈라지는 우회 트랙이며, 패치가 하나라도 있으면 live로 갈 때 이 우회로를 거칩니다.
- 실행 중에는 live에 도달하지 않고, 실패하면 실패한 지점에서 붉게 흔들린 뒤 멈춥니다.
- 이동은 `THREE.MathUtils.damp`(프레임레이트와 무관한 지수 감쇠)로 처리하고, `useFrame` 안에서는 setState를 호출하지 않습니다.
- `prefers-reduced-motion`이면 큐브가 최종 위치로 바로 이동하고 흔들림·펄스·벨트 애니메이션이 꺼집니다.

### InstancedFleetGrid

```jsx
<InstancedFleetGrid fleet={fleet} demoCount={0} height={320} className="" />
```

- `fleet`: `GET /fleet` 응답(`fleet.nodes`). 노드가 없으면 Canvas 없이 안내 카드만 그립니다.
- `demoCount`: 0보다 크면 결정적(seed 7) 가상 노드를 뒤에 덧붙입니다. 시연용입니다.
- 상태 색상: 정상 `#10b981`(에메랄드), 과부하 `#f97316`(주황, `load1 / cpus >= 0.8`, 백엔드와 같은 기준), 장애 `#ef4444`(빨강, `ok=false`).
- 펄스: 정상은 느린 호흡, 과부하는 빠르고 큰 호흡, 장애는 어둡게 깜빡임. 노드마다 위상을 달리해 동시에 깜빡이지 않습니다.
- 모든 큐브는 하나의 `InstancedMesh`(형상 1개 + 흰색 머티리얼 1개, 색은 인스턴스별)로 그려집니다. 바닥 Grid는 별도 드로우콜입니다.
- `<Instances limit>`는 2의 거듭제곱 버킷(최소 16)입니다. drei가 버퍼를 첫 `limit` 기준으로 한 번만 잡기 때문에, 폴링 사이에 노드가 늘어도 잘리지 않도록 버킷을 넘을 때만 다시 마운트합니다.

### VictoryCelebration

```jsx
<VictoryCelebration open={open} run={run} onClose={onClose}
  modelUrl={import.meta.env.VITE_VICTORY_MODEL_URL} clip="hiphop01" autoCloseMs={0} height={380} />
```

- `fixed inset-0 z-50` 오버레이입니다. 바깥 래퍼는 `pointer-events-none`이라 뒤의 2D UI를 계속 쓸 수 있고, 무대 카드와 우측 상단 닫기 버튼만 `pointer-events-auto`입니다. Esc로도 닫힙니다.
- 무대: 원형 스테이지, 키·필 `spotLight` 2개, 림 `pointLight` 2개, `<Sparkles />` 4겹(보라·에메랄드·호박·별가루), `ContactShadows`.
- 댄서: `modelUrl`이 있으면 `useGLTF` + `useAnimations`로 `clip` 애니메이션을 재생하고, 그 이름의 클립이 없으면 첫 번째 클립을 재생합니다. `modelUrl`이 없거나, 로딩 중이거나, 로딩에 실패하면 기본형 도형으로 만든 마스코트(CloudBot)가 대신 춤춥니다.
- 하단에는 패치 개수, 패치 카테고리, 공개 URL(http/https만 링크)을 요약합니다.

## 설치

현재 `package.json`과 `package-lock.json`에 이미 들어 있으므로 평소에는 `npm ci`면 충분합니다. 새로 추가할 때의 명령은 다음과 같습니다.

```bash
cd frontend
# 기본 (현재 lockfile 기준 react 19.3.0에서 peer 충돌 없음 확인)
npm install three @react-three/fiber@^9 @react-three/drei@^10
```

peer 충돌이 날 때의 대안:

```bash
npm install three @react-three/fiber@^9 @react-three/drei@^10 --legacy-peer-deps
```

`--legacy-peer-deps`가 필요해지는 실제 이유: `@react-three/fiber` 9.8의 peerDependencies는 `react: ">=19 <19.4"`, `react-dom: ">=19 <19.4"`입니다. `package.json`의 `"react": "^19.2.8"`은 19.4 이상도 허용하므로, lockfile 없이 새로 설치할 때 react가 19.4+로 풀리면 fiber의 peer 범위를 벗어나 `ERESOLVE`가 납니다. (drei 10은 `react: ^19`, `@react-three/fiber: ^9.0.0`이라 문제 없음.)

`--legacy-peer-deps`는 경고를 덮을 뿐이니, 장기적으로는 둘 중 하나를 권장합니다.

- react / react-dom을 fiber가 지원하는 범위로 고정 (예: `"react": "~19.3.0"`)
- react 19.4를 지원하는 fiber 버전으로 업그레이드

참고: `useFrame`, `useThree`, `Canvas`는 `@react-three/fiber`에서 가져옵니다. drei에는 없습니다. drei에서는 `Instances`, `Instance`, `Sparkles`, `useGLTF`, `useAnimations`, `Html`, `RoundedBox`, `OrbitControls`, `Grid`, `ContactShadows`를 씁니다.

## 선택: GLTF 댄서 모델

1. `.glb` 파일을 `frontend/public/models/`에 둡니다. 이 폴더는 `.gitignore`에 들어 있어 커밋되지 않습니다(모델 라이선스·용량 문제 방지).
2. `frontend/.env.local`에 경로를 지정합니다.

   ```bash
   VITE_VICTORY_MODEL_URL=/models/dancer.glb
   ```

3. 개발 서버를 다시 시작합니다. 모델에 `hiphop01` 클립이 없으면 첫 번째 클립을 재생합니다. 다른 이름을 쓰려면 `clip` prop을 넘기세요.

> ⚠️ `public/models/`는 git에서는 제외되지만, `npm run build` 시 `public/`의 모든 파일이 `dist/`로 복사됩니다. 재배포 권한이 없는 모델을 둔 채로 만든 빌드는 배포하지 마세요.

외부 URL을 쓸 경우 CORS가 허용되어야 합니다. 설정하지 않으면 CloudBot 마스코트가 표시됩니다.

## 인스턴싱 데모

```
http://localhost:5173/?fleetDemo=200
```

`fleetDemo`는 페이지 로드 때 한 번 읽고 0~500으로 제한합니다. 3D 그리드는 `fleet`이 있을 때(백엔드가 `/fleet`에 응답할 때)만 렌더되므로, 노드 풀이 없는 백엔드에서는 데모 노드도 보이지 않습니다.

## 축하 세레머니 발동 규칙

`lib/victory.js`의 `shouldCelebrate(run, dismissedId)`가 참일 때만 열립니다.

- `run.id`가 있고 `dismissedId`와 다름
- `run.status === 'completed'` (전체 성공. `partial_failure`, `failed`, `cancelled`, `running`, `idle`은 제외)
- 자가치유 흔적이 있음: `run.patches.length > 0` 또는 어떤 타깃의 `targetState[t].healed`가 참

닫으면 그 `run.id`를 `dismissedId`로 기억해 같은 배포에서는 다시 뜨지 않습니다. 이미 끝난 자가치유 배포를 프로젝트 목록에서 다시 열람(재연결)하면 한 번 더 뜹니다.

## App.jsx 연동 예시

three / fiber / drei는 `React.lazy`로 분리된 청크에 들어가므로 2D UI가 먼저 그려집니다. 각 3D 컴포넌트는 `SceneBoundary`(에러 경계)와 `Suspense`(같은 높이의 스켈레톤)로 감쌉니다.

```jsx
import React, { Suspense, lazy, useState } from 'react';
import SceneBoundary from './components/3d/SceneBoundary';
import { shouldCelebrate } from './lib/victory'; // 순수 함수라 정적 import해도 three가 딸려오지 않음

const PipelineJourney = lazy(() => import('./components/3d/PipelineJourney'));
const InstancedFleetGrid = lazy(() => import('./components/3d/InstancedFleetGrid'));
const VictoryCelebration = lazy(() => import('./components/3d/VictoryCelebration'));

// JourneySkeleton, FleetSkeleton, parseFleetDemo(?fleetDemo=N → 0~500)는 frontend/src/App.jsx 참고
export default function App() {
  // ... run, fleet 등 기존 상태
  const [fleetDemo] = useState(() => parseFleetDemo(window.location.search));
  const [dismissedVictoryId, setDismissedVictoryId] = useState(null);
  const celebrate = shouldCelebrate(run, dismissedVictoryId);

  return (
    <>
      <Stepper run={run} /* ... */ />
      {run.status !== 'idle' && (
        <SceneBoundary>
          <Suspense fallback={<JourneySkeleton />}>
            <PipelineJourney run={run} height={220} />
          </Suspense>
        </SceneBoundary>
      )}

      <FleetPanel fleet={fleet} />
      {fleet && (
        <SceneBoundary>
          <Suspense fallback={<FleetSkeleton />}>
            <InstancedFleetGrid fleet={fleet} demoCount={fleetDemo} height={320} />
          </Suspense>
        </SceneBoundary>
      )}

      {/* 열릴 때만 렌더해야 축하 청크를 첫 화면에서 받지 않음 */}
      {celebrate && (
        <SceneBoundary key={run.id}>
          <Suspense fallback={null}>
            <VictoryCelebration open run={run} onClose={() => setDismissedVictoryId(run.id)} />
          </Suspense>
        </SceneBoundary>
      )}
    </>
  );
}
```

포인트:

- `React.lazy`는 요소가 렌더되는 순간 청크를 가져옵니다. `VictoryCelebration`은 닫혀 있을 때 `null`을 반환하지만, 그래도 렌더 트리에 두면 three 청크를 첫 화면에서 받게 되므로 `celebrate &&`로 감쌉니다.
- 오버레이는 레이아웃 흐름 밖(`fixed`)이라 `Suspense` fallback은 `null`입니다. 나머지 둘은 컴포넌트와 같은 카드 외형·높이의 스켈레톤을 써서 레이아웃이 밀리지 않게 합니다.
- `SceneBoundary`의 기본 fallback은 `null`입니다. GPU가 없거나 WebGL 컨텍스트를 잃으면 3D 패널만 사라지고 Stepper, FleetPanel 같은 2D 패널은 그대로 남습니다.
- 실제 연동 코드는 `frontend/src/App.jsx`에 있습니다.

## 검증

```bash
cd frontend
npm test        # vitest: lib/pipelineJourney, lib/fleetStatus, lib/victory
npm run lint    # oxlint
npm run build   # three 계열은 별도 청크로 분리, 엔트리 청크에는 포함되지 않음
```

빌드 시 three / fiber / drei 공유 청크가 500 kB를 넘는다는 Vite 경고가 나오지만, 지연 로드되는 청크라 첫 화면에는 영향이 없습니다.
