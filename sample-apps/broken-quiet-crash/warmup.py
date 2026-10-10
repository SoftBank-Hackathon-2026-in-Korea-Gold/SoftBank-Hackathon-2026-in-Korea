import ctypes

STEPS = ["loading settings", "priming template cache", "precomputing lookup table"]

for i, step in enumerate(STEPS, 1):
    print(f"[warmup] {i}/{len(STEPS)} {step}")

# 의도된 결함: 네이티브 코드의 NULL 포인터 읽기(SIGSEGV) — 버퍼에 남은 print 출력은 flush되지 못하고 사라짐
ctypes.string_at(0)

print("[warmup] done")
