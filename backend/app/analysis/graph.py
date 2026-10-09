"""AI 아키텍트: 로컬 코드를 분석해 배포 유형을 사용자와 함께 확정하고, 그 유형으로 가는 데 필요한 최소한의 코드 수정을 한다.

흐름 (LangGraph):
  scan(패턴 검색: 검사관에게 줄 참고 줄) → inspect(AI 검사관: 찾는 것마다 각자의 기준으로 판정, 동시에 돈다)
    ─ 치명적 위험을 '찾았지만 괜찮다'고 하면 → consent(사용자 동의) → inspect
    → choose(유형별 필요한 수정을 보여주고 사용자가 고름)
    → plan(고른 유형에 필요한 수정) ─┬─ 큰 수정이 있음 → ask(사용자 승인) ─ 거절하면 choose로
                                     └─ 작은 수정만 → convert ─┬─ diff가 큼 → confirm(사용자 확인)
                                                               └─ 파일이 바뀜 → scan (다시 plan) / 아니면 finish
  plan에 남은 수정이 없으면 finish: 고른 유형이 후보에 있고 큰 수정이 남지 않았으면 배포 준비 완료.
  finish는 백엔드에 넘길 배포 명세(spec.py)도 만든다.

찾고 판정하는 일은 AI 검사관(inspectors.py)이 하고, 코드는 울타리만 친다: 근거가 실제 파일에 있는지 확인하고,
신호 → 유형은 rules.py가 정하고, 치명적 위험을 푸는 것은 사용자 동의를 받는다.
코드를 고친 뒤에는 그 수정과 관련된 검사관만 다시 돌린다. LLM이 없으면 패턴 규칙(signals.py)으로 판정한다.

사용법:
  uv run python analyzer.py <프로젝트 폴더> [--type 4] [--yes | --no | --accept sqlite,health] [--json] [--no-llm]
"""

import argparse
import difflib
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from . import inspectors as ai
from . import llm
from . import offers as offer_table
from .rules import PREFERENCE, TYPE_INFO, TYPES, apply_rules
from .signals import SKIP_DIRS, read_text, scan
from .spec import build_spec

SOURCE_EXTS = {
    ".py",
    ".js",
    ".cjs",
    ".mjs",
    ".ts",
    ".tsx",
    ".jsx",
    ".go",
    ".java",
    ".kt",
    ".rb",
    ".php",
    ".rs",
    ".cs",
    ".json",
    ".txt",
    ".toml",
    ".mod",
    ".xml",
    ".gradle",
    ".csproj",
    ".html",
    ".properties",
    ".yml",
    ".yaml",
}
SOURCE_NAMES = {"Dockerfile", "Gemfile", "Pipfile"}
EDITABLE_EXTRA = {
    "requirements.txt",
    "package.json",
    "pyproject.toml",
    "go.mod",
    "Gemfile",
    "composer.json",
    "Cargo.toml",
}

# 유형별로 deployer가 할 수 있는 일
DEPLOY = {
    1: "안내 (클라우드 연결 시 배포)",
    2: "ECS Fargate 드라이런",
    3: "안내 (클라우드 연결 시 배포)",
    4: "로컬·사내 서버 Docker",
    5: "정적 nginx",
}
# 작은 수정이라도 이보다 크면 쓰기 전에 사용자에게 보여준다
MAX_SMALL_LINES = 30
MAX_SMALL_FILES = 2
# 같은 기능을 못 낼 수도 있는 수정. 이 수정이 필요한 유형은 추천하지 않는다
RISKY = {"crossplatform"}
# 검사관을 동시에 몇 개까지 돌릴지
PARALLEL = 6
# 잘못 풀면 조용히 데이터가 사라지거나 작업이 안 도는 신호. AI가 괜찮다고 해도 사용자 동의 없이는 풀지 않는다
FATAL = {
    "sqlite": "SQLite 파일은 컨테이너를 다시 만들 때 사라집니다",
    "file_write": "로컬 디스크에 쓴 파일은 재배포·재시작 때 사라지고, 여러 대로 늘면 서로 보이지 않습니다",
    "memory_state": "메모리 값은 재시작 때 사라지고, 여러 대로 늘면 대마다 달라집니다",
    "scheduler": "요청이 없으면 멈추는 환경에서는 주기 작업이 조용히 실행되지 않습니다",
}


class State(TypedDict, total=False):
    path: str
    languages: list
    frameworks: dict  # {언어: 프레임워크}
    signals: dict
    reports: dict  # {검사관: 확인을 거친 보고} (inspectors.validate)
    rejected: list  # 검사관이 냈지만 파일에서 확인되지 않아 버린 근거·이름
    llm_errors: list  # 검사관 호출 실패 (그 항목은 패턴 판정을 쓴다)
    recheck: list  # 코드를 고친 뒤 다시 돌릴 검사관
    pending: list  # 동의를 기다리는 치명적 신호 이름
    consent: dict  # {근거 키: 풀어도 되는가} 사용자 답
    target: int  # 사용자가 고른 유형 (없으면 None)
    asked: list  # 이미 시도했거나 물어본 제안 id (다시 묻지 않는다)
    declined: list  # 사용자가 거절한 제안 id
    to_ask: list  # 이번에 물어볼 큰 수정 id
    accepted: list  # 이번에 적용할 제안 id
    held: list  # 크기가 커서 확인을 기다리는 작은 수정 [{'offer', 'summary', 'diff', 'edits'}]
    conversions: list  # [{'offer', 'applied', 'summary', 'diff'}]
    rescan: bool  # 이번 scan 이후 convert·confirm에서 파일이 바뀌었는가
    result: dict


def read_sources(root: Path) -> dict:
    files = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if (
            p.is_file()
            and (p.suffix in SOURCE_EXTS or p.name in SOURCE_NAMES)
            and p.name != "package-lock.json"
            and not any(part in SKIP_DIRS or part.startswith(".") for part in rel.parts)
            and p.stat().st_size <= 100_000
        ):
            files[str(rel)] = read_text(p)
    return files


def evidence_key(name: str, signal: dict) -> str:
    """사용자 동의를 기억하는 키. 줄 번호 대신 파일과 그 줄의 코드로 기억한다.
    다른 수정 때문에 줄이 밀려도 다시 묻지 않고, 근거 코드가 바뀌면 다시 묻는다."""
    return (
        name
        + "|"
        + ",".join(sorted(f"{h['evidence'].rpartition(':')[0]}:{h['text']}" for h in signal["hits"]))
    )


def hints(name: str, state: State) -> str:
    """검사관에게 주는 참고: 패턴 검색이 찾은 줄 (틀릴 수 있다)"""
    if name in ai.SIGNAL_CRITERIA:
        return "\n".join(f"- {h['evidence']}: {h['text']}" for h in state["signals"][name]["hits"][:10])
    if name == "stack":
        fw = state["frameworks"]
        return "매니페스트로 본 언어: " + (
            ", ".join(f"{lang} ({fw[lang]})" if lang in fw else lang for lang in state["languages"]) or "없음"
        )
    return ""


def apply_reports(rule_signals: dict, reports: dict, consent: dict) -> tuple[dict, list]:
    """검사관 보고로 신호를 만든다. 보고가 없는 항목(검사관 실패)은 패턴 판정이 남는다. (신호, 동의를 기다리는 치명적 신호)"""
    signals = {k: dict(v) for k, v in rule_signals.items()}
    pending = []
    for n in ai.SIGNAL_CRITERIA:
        r = reports.get(n)
        if not r:
            continue
        s = {"value": r["value"], "hits": r["hits"], "by": "ai", "reason": r["reason"], "sure": True}
        if (
            r["value"] == "yes" and not r["hits"]
        ):  # 근거를 확인 못 한 '위험': 치명적이면 위험으로 두고, 아니면 판정을 미룬다
            s.update(
                value="yes" if n in FATAL else "unknown",
                reason=r["reason"] + " (근거 줄을 파일에서 확인하지 못했습니다)",
            )
        elif r["value"] == "no" and n in FATAL and (r["hits"] or rule_signals[n]["value"] == "yes"):
            # 찾았지만 괜찮다고 본 치명적 위험은 사용자가 정한다. AI가 근거를 안 냈어도 패턴 검색이 찾았으면 그 줄로 묻는다
            s["hits"] = r["hits"] or rule_signals[n]["hits"]
            key = evidence_key(n, s)
            if key not in consent:
                pending.append(n)
                s["value"] = "yes"  # 답을 받기 전까지는 위험으로 둔다
            elif consent[key]:
                s["by"] = "ai+user"
            else:
                s.update(
                    value="yes",
                    by="user",
                    reason=f"AI는 괜찮다고 봤지만 사용자가 위험으로 유지했습니다. AI 판단: {r['reason']}",
                )
        signals[n] = s
    stack = reports.get("stack")
    if stack:  # 배포 준비 신호는 실행 방법 보고에서 나온다
        server = signals["has_server"]["value"] == "yes"
        derived = {
            "port_hardcoded": server and not stack["port_env"] and not stack["static_output"],
            "no_health": server and not stack["health"],
            "faas_adapter": bool(stack["handler"]),
        }
        for n, on in derived.items():
            signals[n] = {
                "value": "yes" if on else "no",
                "hits": [],
                "by": "ai",
                "reason": stack["reason"],
                "sure": True,
            }
    return signals, pending


def prepare_edits(root: Path, edits, creates=frozenset()) -> tuple[list, str]:
    """기존 파일, 의존성 파일, 제안이 허락한 새 파일만 허용한다. 프로젝트 밖 경로는 거부한다. (쓸 목록, unified diff)를 돌려준다."""
    plan = []
    for e in edits:
        target = (root / e.path).resolve()
        if not target.is_relative_to(root) or ".git" in target.relative_to(root).parts:
            raise ValueError(f"고칠 수 없는 경로입니다: {e.path}")
        if not target.is_file() and not (
            target.parent == root and target.name in EDITABLE_EXTRA | set(creates)
        ):
            raise ValueError(f"이 제안으로는 만들 수 없는 파일입니다: {e.path}")
        plan.append((target, e.content))
    diffs = []
    for target, content in plan:
        before = read_text(target) if target.is_file() else ""
        rel = target.relative_to(root)
        diffs += difflib.unified_diff(
            before.splitlines(True), content.splitlines(True), f"a/{rel}", f"b/{rel}"
        )
    return plan, "".join(diffs)


def write_edits(plan):
    for target, content in plan:  # 모두 검사한 뒤에 쓴다 (일부만 적용되는 일이 없게)
        target.write_text(content)


def changed_lines(diff: str) -> int:
    return sum(1 for line in diff.splitlines() if line[:1] in "+-" and line[:3] not in ("+++", "---"))


def options(state: State) -> list[dict]:
    """유형마다: 지금 후보인지, 가려면 고칠 것, 갈 수 없는 이유, deployer가 할 수 있는 일, 장단점, 클라우드별 서비스 예시."""
    candidates = apply_rules(state["signals"])["candidates"]
    declined = set(state.get("declined", []))
    out = []
    for t in TYPES:
        fixes, reasons = offer_table.plan(t, state["signals"], state["languages"])
        refused = [o["id"] for o in fixes if o["id"] in declined and o["id"] not in offer_table.SMALL]
        if refused:
            reasons = [f"거절한 수정이 필요합니다: {', '.join(refused)}"]
        out.append(
            {
                "type": t,
                "name": TYPES[t],
                "deploy": DEPLOY[t],
                "now": t in candidates and not reasons,
                "fixes": []
                if reasons
                else [
                    {"id": o["id"], "small": o["id"] in offer_table.SMALL, "question": o["question"]}
                    for o in fixes
                ],
                "blocked": reasons,
                **TYPE_INFO[t],
            }
        )
    return out


def recommend(state: State, opts: list[dict]) -> dict | None:
    """운영 부담이 적고 옮기기 쉬운 유형부터 본다. 큰 수정이 필요하면 무엇을 고쳐야 하는지, 크게 고치지 않고 갈 수 있는 유형은 무엇인지 함께 말한다."""
    by_type = {o["type"]: o for o in opts}
    open_types = [t for t in PREFERENCE if not by_type[t]["blocked"]]
    tags = apply_rules(state["signals"])["tags"]
    for t in open_types:
        fixes = by_type[t]["fixes"]
        if any(f["id"] in RISKY for f in fixes):
            continue
        if (
            t == 5 and state["signals"]["is_spa"]["value"] != "yes"
        ):  # 서버 근거를 못 찾았다고 정적 사이트인 것은 아니다
            continue
        why = [TYPE_INFO[t]["pros"]] + tags.get(t, [])
        big = [f["id"] for f in fixes if not f["small"]]
        if big:
            why.append(f"{', '.join(big)} 수정이 필요합니다")
            plain = next((p for p in open_types if all(f["small"] for f in by_type[p]["fixes"])), None)
            if plain:
                why.append(f"코드를 크게 고치지 않으려면 {plain}. {TYPES[plain]}")
        if state["signals"]["long_running"]["value"] == "yes" and t != 4:
            why.append(
                "요청과 별개로 계속 도는 작업(WebSocket 같은 긴 연결, 백그라운드 작업)이 있어 연결 시간 제한과 최소 인스턴스 수를 확인해야 합니다"
            )
        if state["signals"]["scheduler"]["value"] == "yes" and t in (
            2,
            3,
        ):  # 0대로 줄면 주기 작업이 조용히 안 돈다
            why.append("프로세스 안의 주기 작업이 있어 인스턴스를 1대로 고정하고 0대로 줄지 않게 해야 합니다")
        return {"type": t, "name": TYPES[t], "why": why}
    return None


def recheck(state: State, done: list) -> list:
    """고친 수정과 관련된 검사관 (다시 스캔한 뒤 이것만 다시 돌린다)"""
    names = {n for c in done if c["applied"] for n in ai.RECHECK.get(c["offer"], [])}
    return sorted(names | set(state.get("recheck", [])))


def build_graph(inspect=None, converter=None):
    def scan_node(state: State):
        r = scan(state["path"])
        return {
            "languages": r["languages"],
            "frameworks": r["frameworks"],
            "signals": r["signals"],
            "rescan": False,
        }

    def inspect_node(state: State):
        if not inspect:  # LLM이 없으면 패턴 판정 그대로
            return {"pending": [], "recheck": []}
        root = Path(state["path"]).resolve()
        reports = dict(state.get("reports", {}))
        rejected, errors = list(state.get("rejected", [])), list(state.get("llm_errors", []))
        names = [n for n in ai.INSPECTORS if n not in reports or n in state.get("recheck", [])]
        if names:
            files, text = read_sources(root), ai.repo_text(root)
            with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
                futures = {n: pool.submit(inspect, n, hints(n, state), files) for n in names}
            for n, f in futures.items():
                try:
                    reports[n], dropped = ai.validate(n, f.result(), root, text)
                    rejected += dropped
                except Exception as e:  # noqa: BLE001 — 실패한 검사관 항목은 패턴 판정을 쓴다. 같은 스캔 안에서는 다시 부르지 않는다
                    reports[n] = None
                    errors.append(f"{n}: {e}")
        signals, pending = apply_reports(state["signals"], reports, state.get("consent", {}))
        frameworks = dict(state["frameworks"])
        stack = reports.get("stack")
        if stack and stack["framework"]:
            frameworks[stack["language"]] = stack["framework"]
        return {
            "signals": signals,
            "frameworks": frameworks,
            "reports": reports,
            "rejected": rejected,
            "llm_errors": errors,
            "pending": pending,
            "recheck": [],
        }

    def consent_node(state: State):
        items = []
        for n in state["pending"]:
            items.append(
                {
                    "signal": n,
                    "title": ai.SIGNAL_CRITERIA[n][0],
                    "risk": FATAL[n],
                    "hits": state["signals"][n]["hits"][:5],
                    "ai_reason": state["reports"][n]["reason"],
                }
            )
        answer = interrupt({"kind": "consent", "items": items})
        agreed = {n for n in state["pending"] if answer == "all" or n in (answer or [])}
        decided = {evidence_key(n, state["signals"][n]): n in agreed for n in state["pending"]}
        return {"consent": {**state.get("consent", {}), **decided}, "pending": []}

    def choose_node(state: State):
        opts = options(state)
        answer = interrupt(
            {
                "kind": "choose",
                "languages": state["languages"],
                "frameworks": state["frameworks"],
                "recommended": recommend(state, opts),
                "options": opts,
            }
        )
        ok = {o["type"] for o in opts if not o["blocked"]}
        return {"target": answer if answer in ok else None}

    def plan_node(state: State):
        fixes, _ = offer_table.plan(state["target"], state["signals"], state["languages"])
        asked = set(state.get("asked", []))
        todo = [o["id"] for o in fixes if o["id"] not in asked]
        small = [i for i in todo if i in offer_table.SMALL]
        return {
            "to_ask": [i for i in todo if i not in offer_table.SMALL],
            "accepted": small,
            "asked": state.get("asked", []) + small,
        }

    def ask_node(state: State):
        ids = state["to_ask"]
        todo = [offer_table.BY_ID[i] for i in ids]
        payload = [
            {
                "id": o["id"],
                "question": o["question"],
                "effect": offer_table.effect(o, state["signals"], todo),
                "hits": offer_table.hits(o, state["signals"]),
            }
            for o in todo
        ]
        answer = interrupt({"kind": "fixes", "target": state["target"], "offers": payload})
        chosen = ids if answer == "all" else [i for i in ids if i in (answer or [])]
        refused = [i for i in ids if i not in chosen]
        update = {
            "asked": state.get("asked", []) + ids,
            "declined": state.get("declined", []) + refused,
            "to_ask": [],
        }
        if refused:  # 고른 유형에 꼭 필요한 수정이라 이 유형으로는 갈 수 없다. 다른 유형을 고르게 한다.
            # 함께 적용하려던 작은 수정은 아직 안 했으므로, 다음 유형에서 다시 계획할 수 있게 asked에서 뺀다
            asked = [i for i in state.get("asked", []) if i not in state["accepted"]] + ids
            return {**update, "asked": asked, "target": None, "accepted": []}
        return {**update, "accepted": state["accepted"] + chosen}

    def convert_node(state: State):
        root = Path(state["path"]).resolve()
        done, held = [], []
        todo = sorted(state["accepted"], key=list(offer_table.BY_ID).index)
        for i, oid in enumerate(todo):
            offer = offer_table.BY_ID[oid]
            if not converter:
                done.append(
                    {
                        "offer": oid,
                        "applied": False,
                        "diff": "",
                        "summary": "코드 수정기가 없어 적용하지 않았습니다 (LLM이 없거나, 파이프라인에서는 코드를 고치지 않음)",
                    }
                )
                continue
            try:
                # 앞 제안이 고친 결과를 다음 제안이 보도록 매번 파일을 다시 읽는다
                conv = converter(offer, offer_table.hits(offer, state["signals"]), read_sources(root))
                plan, diff = prepare_edits(root, conv.edits, offer.get("creates", set()))
                if oid in offer_table.SMALL and (
                    changed_lines(diff) > MAX_SMALL_LINES or len(plan) > MAX_SMALL_FILES
                ):
                    held.append(
                        {
                            "offer": oid,
                            "summary": conv.summary,
                            "diff": diff,
                            "edits": [{"path": e.path, "content": e.content} for e in conv.edits],
                        }
                    )
                    # 확인을 기다리는 동안 다른 수정이 같은 파일을 바꾸면, 확인 뒤에 쓸 때 그 수정을 덮어쓴다. 나머지는 확인 뒤에 한다
                    return {
                        "conversions": state.get("conversions", []) + done,
                        "accepted": todo[i + 1 :],
                        "held": held,
                        "rescan": state.get("rescan", False) or any(c["applied"] for c in done),
                        "recheck": recheck(state, done),
                    }
                write_edits(plan)
                done.append(
                    {
                        "offer": oid,
                        "applied": bool(diff),
                        "summary": conv.summary,
                        "diff": diff,
                        "schedules": [s.model_dump() for s in conv.schedules],
                    }
                )
            except Exception as e:  # noqa: BLE001 — LLM 호출 실패나 허락되지 않은 경로: 이 제안만 건너뛴다
                done.append(
                    {"offer": oid, "applied": False, "diff": "", "summary": f"적용하지 않았습니다: {e}"}
                )
        return {
            "conversions": state.get("conversions", []) + done,
            "accepted": [],
            "held": held,
            "rescan": state.get("rescan", False) or any(c["applied"] for c in done),
            "recheck": recheck(state, done),
        }

    def confirm_node(state: State):
        held = state["held"]
        answer = interrupt(
            {"kind": "confirm", "items": [{k: h[k] for k in ("offer", "summary", "diff")} for h in held]}
        )
        root = Path(state["path"]).resolve()
        done = []
        for h in held:
            if answer == "all" or h["offer"] in (answer or []):
                offer = offer_table.BY_ID[h["offer"]]
                plan, diff = prepare_edits(
                    root, [llm.FileEdit(**e) for e in h["edits"]], offer.get("creates", set())
                )
                write_edits(plan)
                done.append(
                    {"offer": h["offer"], "applied": bool(diff), "summary": h["summary"], "diff": diff}
                )
            else:
                done.append(
                    {
                        "offer": h["offer"],
                        "applied": False,
                        "diff": h["diff"],
                        "summary": "수정이 커서 확인을 받았고, 거절했습니다",
                    }
                )
        return {
            "conversions": state.get("conversions", []) + done,
            "held": [],
            "rescan": state["rescan"] or any(c["applied"] for c in done),
            "recheck": recheck(state, done),
        }

    def finish_node(state: State):
        result = apply_rules(state["signals"])
        result["recommended"] = recommend(state, options(state))
        t = state.get("target")
        if t:
            fixes, reasons = offer_table.plan(t, state["signals"], state["languages"])
            remaining = [o["id"] for o in fixes]
            result.update(
                target=t,
                deploy=DEPLOY[t],
                remaining=remaining,
                ready=t in result["candidates"]
                and not reasons
                and not [i for i in remaining if i not in offer_table.SMALL],
                spec=build_spec(state, reasons, remaining).model_dump(),
            )
        return {"result": result}

    g = StateGraph(State)
    for name, fn in [
        ("scan", scan_node),
        ("inspect", inspect_node),
        ("consent", consent_node),
        ("choose", choose_node),
        ("plan", plan_node),
        ("ask", ask_node),
        ("convert", convert_node),
        ("confirm", confirm_node),
        ("finish", finish_node),
    ]:
        g.add_node(name, fn)
    g.add_edge(START, "scan")
    g.add_edge("scan", "inspect")
    g.add_conditional_edges(
        "inspect",
        lambda s: "consent" if s["pending"] else "plan" if s.get("target") else "choose",
        ["consent", "plan", "choose"],
    )
    g.add_edge("consent", "inspect")  # 답을 반영한다 (검사관은 다시 부르지 않는다)
    g.add_conditional_edges("choose", lambda s: "plan" if s["target"] else "finish", ["plan", "finish"])
    g.add_conditional_edges(
        "plan",
        lambda s: "ask" if s["to_ask"] else "convert" if s["accepted"] else "finish",
        ["ask", "convert", "finish"],
    )
    g.add_conditional_edges(
        "ask",
        lambda s: "choose" if not s["target"] else "convert" if s["accepted"] else "finish",
        ["choose", "convert", "finish"],
    )
    g.add_conditional_edges(
        "convert",
        lambda s: "confirm" if s["held"] else "scan" if s["rescan"] else "finish",
        ["confirm", "scan", "finish"],
    )
    g.add_conditional_edges(
        "confirm",
        lambda s: "convert" if s["accepted"] else "scan" if s["rescan"] else "finish",
        ["convert", "scan", "finish"],
    )
    g.add_edge("finish", END)
    return g.compile(checkpointer=InMemorySaver())


def run(path, target=None, accept=None, inspect=None, converter=None, ask=None) -> dict:
    """그래프를 끝까지 돌린다. 유형 선택에는 target으로, 수정 승인·확인에는 accept('all' 또는 id 목록)로 답한다.
    주지 않은 답은 ask(payload) 콜백으로 묻는다. 치명적 위험을 푸는 동의(consent)는 미리 줄 수 없고 언제나 ask로 묻는다."""
    graph = build_graph(inspect, converter)
    config = {"configurable": {"thread_id": "cli"}}
    out = graph.invoke({"path": str(Path(path).resolve())}, config)
    while "__interrupt__" in out:
        payload = out["__interrupt__"][0].value
        given = target if payload["kind"] == "choose" else None if payload["kind"] == "consent" else accept
        answer = given if given is not None else ask(payload)
        if answer is None:  # LangGraph는 None으로 재개할 수 없다. "고르지 않음"을 0 / []로 바꾼다
            answer = 0 if payload["kind"] == "choose" else []
        out = graph.invoke(Command(resume=answer), config)
    return out


def ask_terminal(payload):
    if payload["kind"] == "choose":
        print(f"\n{stack(payload['languages'], payload['frameworks'])}")
        rec = payload["recommended"]
        if rec:
            print(f"추천: {rec['type']}. {rec['name']} — {'; '.join(rec['why'])}")
        print("배포 유형:")
        for o in payload["options"]:
            if o["blocked"]:
                print(f"  ✗ {o['type']}. {o['name']} — {'; '.join(o['blocked'])}")
                continue
            fixes = (
                ", ".join(f"{f['id']}({'자동' if f['small'] else '승인 필요'})" for f in o["fixes"])
                or "수정 없음"
            )
            star = " ★" if rec and rec["type"] == o["type"] else ""
            print(
                f"  {'✓' if o['now'] else '△'} {o['type']}. {o['name']}{star} — {fixes} · 배포: {o['deploy']}"
            )
            print(f"      + {o['pros']} / - {o['cons']} · 예: {' · '.join(o['services'].values())}")
        raw = input("유형 번호 (Enter = 판정만 하고 끝내기): ").strip()
        return int(raw) if raw.isdigit() else None
    if payload["kind"] == "consent":
        print(
            "\nAI가 아래 위험은 풀어도 된다고 판단했습니다. 잘못 풀면 데이터가 조용히 사라질 수 있어 직접 확인을 받습니다."
        )
        for n, it in enumerate(payload["items"], 1):
            print(f"  [{n}] {it['signal']} — {it['risk']}\n      AI 판단: {it['ai_reason']}")
            for h in it["hits"][:3]:
                print(f"        {h['evidence']}: {h['text']}")
        raw = input("풀어도 되는 번호 (예: 1,2 / a = 전부 / Enter = 모두 위험으로 유지): ").strip().lower()
        picks = {int(x) for x in raw.replace(" ", "").split(",") if x.isdigit()}
        return (
            "all"
            if raw in ("a", "all")
            else [it["signal"] for n, it in enumerate(payload["items"], 1) if n in picks]
        )
    if payload["kind"] == "fixes":
        items = payload["offers"]
        print(
            f"\n유형 {payload['target']}(으)로 가려면 아래 수정이 필요합니다. 하나라도 거절하면 유형을 다시 고릅니다."
        )
    else:
        items = payload["items"]
        print(
            f"\n작은 수정인데 변경이 큽니다 ({MAX_SMALL_LINES}줄 또는 파일 {MAX_SMALL_FILES}개 초과). 적용할까요?"
        )
    for n, o in enumerate(items, 1):
        if payload["kind"] == "fixes":
            print(f"  [{n}] {o['question']}  → {o['effect']}")
            for h in o["hits"][:3]:
                print(f"        {h['evidence']}: {h['text']}")
        else:
            print(f"  [{n}] {o['offer']}: {o['summary']}\n{o['diff']}")
    raw = input("적용할 번호 (예: 1,3 / a = 전부 / Enter = 거절): ").strip().lower()
    if raw in ("a", "all"):
        return "all"
    picks = {int(x) for x in raw.replace(" ", "").split(",") if x.isdigit()}
    key = "id" if payload["kind"] == "fixes" else "offer"
    return [o[key] for n, o in enumerate(items, 1) if n in picks]


def stack(languages: list, frameworks: dict) -> str:
    return "언어: " + (
        ", ".join(f"{lang} ({frameworks[lang]})" if lang in frameworks else lang for lang in languages)
        or "감지 안 됨"
    )


def report(out: dict) -> str:
    r = out["result"]
    lines = [stack(out["languages"], out["frameworks"])]
    if r["recommended"]:
        rec = r["recommended"]
        lines.append(f"추천: {rec['type']}. {rec['name']} — {'; '.join(rec['why'])}")
    lines.append("")
    for c in out.get("conversions", []):
        mark = "적용" if c["applied"] else "미적용"
        lines += [f"[수정 · {c['offer']} · {mark}] {c['summary']}", c["diff"] or ""]
    if r.get("target"):
        t, spec = r["target"], r["spec"]
        lines.append(
            f"[확정] {t}. {TYPES[t]} · 배포: {r['deploy']} · 예: {' · '.join(spec['services'].values())}"
        )
        if spec["ready"]:
            lines.append(
                "  배포 준비 완료"
                + (f" (자동 수정 미적용: {', '.join(r['remaining'])})" if r["remaining"] else "")
            )
        else:
            lines += ["  아직 배포할 수 없습니다"] + [f"    ✗ {b}" for b in spec["blockers"]]
        lines += ["", "[배포 절차]"] + [f"  {n}. {s}" for n, s in enumerate(spec["steps"], 1)]
        if spec["env"]:
            lines += ["", "[환경변수]"]
            for e in spec["env"]:
                need = (
                    "자동 생성 가능"
                    if e["generate"]
                    else "필수"
                    if e["required"]
                    else f"기본값 {e['default']!r}"
                    if e["default"] is not None
                    else "선택"
                )
                lines.append(
                    f"  {e['name']} ({need}) — {e['description'] or '설명 없음 (LLM을 쓰면 채워집니다)'}  [{e['evidence']}]"
                )
                if e["how"] or e["example"]:
                    lines.append(
                        "      " + " ".join(filter(None, [e["how"], e["example"] and f"예: {e['example']}"]))
                    )
        lines.append("")
    lines.append("[후보]")
    for t in r["candidates"]:
        lines.append(f"  ✓ {t}. {TYPES[t]}")
        lines += [f"      · {tag}" for tag in r["tags"].get(t, [])]
    if r["blocked"]:
        lines.append(f"  {r['blocked']}")
    lines += ["", "[탈락]"]
    for t, reasons in sorted(r["removed"].items()):
        lines.append(f"  ✗ {t}. {TYPES[t]}")
        for why in reasons:
            ev = ", ".join(h["evidence"] for h in why["hits"][:3])
            lines.append(f"      {why['rule']} {why['reason']} ({ev})")
    by = {"ai": "AI", "ai+user": "AI 판단 + 사용자 동의", "user": "사용자가 위험으로 유지"}
    llm_signals = {k: v for k, v in out["signals"].items() if v["by"] in by}
    if llm_signals:
        lines += ["", "[AI 판정]"]
        lines += [f"  {k} = {v['value']} ({by[v['by']]}): {v['reason']}" for k, v in llm_signals.items()]
    risks = ((out.get("reports") or {}).get("risks") or {}).get("items", [])
    if risks:
        lines += ["", "[AI가 찾은 기타 위험] 판정에는 쓰지 않았습니다"]
        lines += [f"  {r['title']} ({r['hits'][0]['evidence']}): {r['reason']}" for r in risks]
    if out.get("rejected"):
        lines.append(f"\n[버린 AI 근거] {len(out['rejected'])}개 (파일에서 확인되지 않음)")
    if out.get("llm_errors"):
        lines += ["", "[AI 호출 실패] 그 항목은 패턴 판정을 썼습니다"] + [f"  {e}" for e in out["llm_errors"]]
    if r["review"]:
        lines += [
            "",
            "[확인 필요] 규칙으로 판정하지 못했고 LLM도 쓸 수 없었습니다. 후보에서 지우지 않았습니다.",
        ]
        for k, hits in r["review"].items():
            ev = ", ".join(h["evidence"] for h in hits[:3]) or "근거 없음"
            lines.append(f"  ? {k} ({ev})")
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(
        description="코드를 분석해 배포 유형을 확정하고, 그 유형으로 가는 데 필요한 코드 수정을 합니다"
    )
    p.add_argument("path")
    p.add_argument(
        "--type", type=int, choices=sorted(TYPES), help="확정할 배포 유형 번호 (없으면 물어봅니다)"
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument("--yes", action="store_true", help="수정을 전부 승인")
    g.add_argument("--no", action="store_true", help="수정을 전부 거절")
    g.add_argument("--accept", help=f"승인할 수정 id (쉼표 구분): {', '.join(offer_table.BY_ID)}")
    p.add_argument("--json", action="store_true", help="결과를 JSON으로 출력")
    p.add_argument("--no-llm", action="store_true", help="LLM 없이 규칙만으로 판정")
    a = p.parse_args()

    accept = "all" if a.yes else [] if a.no else a.accept.split(",") if a.accept else None
    model = None if a.no_llm else llm.load_model(a.path)
    out = run(
        a.path,
        target=a.type,
        accept=accept,
        inspect=ai.make_inspector(model) if model else None,
        converter=llm.make_converter(model) if model else None,
        ask=ask_terminal,
    )
    if a.json:
        keys = (
            "languages",
            "frameworks",
            "signals",
            "conversions",
            "reports",
            "rejected",
            "llm_errors",
            "result",
        )
        json.dump({k: out.get(k) for k in keys}, sys.stdout, ensure_ascii=False, indent=2)
    else:
        print(report(out))


if __name__ == "__main__":
    main()
