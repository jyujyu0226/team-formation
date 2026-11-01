"""팀 편성 규칙과 배정 계산 (화면, DB와 무관한 순수 파이썬).

이 파일은 Streamlit이나 DB를 전혀 쓰지 않기 때문에 컴퓨터에서 바로 테스트할 수 있다.
앱은 DB에서 읽은 현재 상태(State)를 이 파일의 함수에 넘기고,
함수가 돌려준 결과(Outcome)의 effects(DB에 할 일 목록)를 DB에 반영한다.
"""
from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------- 상수
ROLES = ["기획자", "웹 프로그래머", "프론트엔드 디자이너", "테스터", "보고서 작성자", "발표자"]
PLANNER, WEB = "기획자", "웹 프로그래머"
MIN_TEAM, MAX_TEAM = 2, 4
YES, NO = "찬성", "거부"
PENDING, APPROVED, REJECTED = "대기", "승인", "거절"
DEFAULT_PASSWORD = "1234"  # 공고 2.2절: 가상 학생의 비밀번호는 모두 1234


# ---------------------------------------------------------------- 데이터 모양
@dataclass(frozen=True)
class Person:
    id: int
    no: str          # 학번
    name: str
    main: str        # 주 역할
    subs: tuple = ()  # 보조 역할 0~2개

    @property
    def roles(self):
        return {self.main, *self.subs}


@dataclass
class App:
    id: int
    team_id: int
    student_id: int
    status: str = PENDING
    reason: str = ""


@dataclass
class State:
    persons: dict = field(default_factory=dict)   # 학생 id -> Person (관리자는 없음)
    teams: dict = field(default_factory=dict)     # 팀 id -> 팀 이름 (id 순서)
    members: dict = field(default_factory=dict)   # 팀 id -> [학생 id, ...] (칸 번호 순서)
    apps: dict = field(default_factory=dict)      # 대기 중인 지원: 지원 id -> App
    latest: dict = field(default_factory=dict)    # 학생 id -> 그 학생의 가장 최근 지원 App
    votes: dict = field(default_factory=dict)     # 지원 id -> {투표한 학생 id: 찬성/거부}
    closed: bool = False
    proposal: dict | None = None                  # {"assign": {팀 id: [학생 id]}, "source": 글}

    def team_of(self, student_id):
        for tid, ids in self.members.items():
            if student_id in ids:
                return tid
        return None

    def pending_of(self, student_id):
        for app in self.apps.values():
            if app.student_id == student_id:
                return app
        return None

    def people(self, team_id):
        return [self.persons[i] for i in self.members.get(team_id, []) if i in self.persons]

    def unassigned(self):
        placed = {i for ids in self.members.values() for i in ids}
        return [p for p in sorted(self.persons.values(), key=lambda p: p.id) if p.id not in placed]


@dataclass
class Outcome:
    """한 번의 동작 결과. kind는 승인/대기/거절/막힘 가운데 하나(화면 result가 이 글자로 시작함)."""
    kind: str
    message: str
    effects: list = field(default_factory=list)

    @property
    def text(self):
        return f"{self.kind}: {self.message}"


def role_text(p: Person) -> str:
    return f"주 역할 {p.main}" + (f" / 보조 역할 {', '.join(p.subs)}" if p.subs else "")


# ---------------------------------------------------------------- 팀 규칙
def required_problem(people):
    """필수 포함 역할(기획자, 웹 프로그래머를 서로 다른 학생이)을 못 채우면 이유 글, 채우면 None."""
    pl = [p for p in people if PLANNER in p.roles]
    wp = [p for p in people if WEB in p.roles]
    if not pl and not wp:
        return "기획자와 웹 프로그래머를 맡을 학생이 없음"
    if not pl:
        return "기획자를 맡을 학생이 없음"
    if not wp:
        return "웹 프로그래머를 맡을 학생이 없음"
    if not any(a.id != b.id for a in pl for b in wp):
        return "기획자와 웹 프로그래머를 서로 다른 학생이 맡을 수 없음"
    return None


def unfilled_required(people) -> int:
    """아직 채우지 못한 필수 포함 역할 수(0~2). 한 학생이 둘 다 할 수 있어도 하나로만 셈."""
    pl = [p for p in people if PLANNER in p.roles]
    wp = [p for p in people if WEB in p.roles]
    if any(a.id != b.id for a in pl for b in wp):
        return 0
    return 1 if (pl or wp) else 2


def join_problem(members, newcomer):
    """newcomer가 members(현재 팀원)의 팀에 들어갈 때 어기는 규칙. 지켜지면 None.

    지원할 때와 승인 직전 재검사 때 똑같이 쓴다.
    """
    if len(members) >= MAX_TEAM:
        return "팀이 꽉 참"
    if any(m.main == newcomer.main for m in members):
        return "같은 주 역할의 팀원이 있음"
    after = list(members) + [newcomer]
    seats_left = MAX_TEAM - len(after)
    if seats_left < unfilled_required(after):
        return "필수 포함 역할을 맡을 학생의 자리가 모자람"
    return None


def apply_problem(state: State, student_id: int, team_id: int):
    if state.closed:
        return "모집이 마감됨"
    if state.team_of(student_id) is not None:
        return "이미 팀이 있음"
    if state.pending_of(student_id) is not None:
        return "결과를 기다리는 지원이 있음"
    if team_id not in state.teams:
        return "없는 팀임"
    return join_problem(state.people(team_id), state.persons[student_id])


# ---------------------------------------------------------------- 지원, 투표, 마감
def apply_to_team(state: State, student_id: int, team_id: int) -> Outcome:
    if student_id not in state.persons:
        return Outcome("막힘", "학생만 지원할 수 있음")
    reason = apply_problem(state, student_id, team_id)
    if reason:
        return Outcome("막힘", reason)
    tname = state.teams[team_id]
    if not state.members.get(team_id):
        # 빈 팀: 투표할 팀원이 없으므로 바로 승인
        return Outcome(
            "승인", f"{tname}이 빈 팀이라 바로 팀원이 됨",
            [("add_app", team_id, student_id, APPROVED, "빈 팀"), ("add_member", team_id, student_id)],
        )
    return Outcome("대기", f"{tname}의 팀원이 투표할 때까지 기다림",
                   [("add_app", team_id, student_id, PENDING, "")])


def cast_vote(state: State, voter_id: int, applicant_id: int, vote: str) -> Outcome:
    if voter_id not in state.persons or vote not in (YES, NO):
        return Outcome("막힘", "학생만 투표할 수 있음")
    app = state.pending_of(applicant_id)
    if app is None:
        last = state.latest.get(applicant_id)
        if last is not None and last.status in (APPROVED, REJECTED):
            return Outcome(last.status, "이미 결과가 나온 지원임")
        return Outcome("막힘", "투표할 지원이 없음")
    members = state.members.get(app.team_id, [])
    if voter_id not in members:
        return Outcome("막힘", "그 팀의 팀원만 투표할 수 있음")
    votes = dict(state.votes.get(app.id, {}))
    if voter_id in votes:
        return Outcome("대기", "이미 투표했음(한 번 투표하면 바꿀 수 없음)")

    applicant = state.persons[applicant_id]
    tname = state.teams[app.team_id]
    effects = [("add_vote", app.id, voter_id, vote)]

    if vote == NO:  # 한 명이라도 거부하면 다른 팀원을 기다리지 않고 바로 거절
        effects.append(("set_app", app.id, REJECTED, "팀원이 거부함"))
        return Outcome("거절", f"{applicant.name}의 지원이 거부됨", effects)

    votes[voter_id] = YES
    # 투표할 사람은 '투표하는 때의 팀원'이므로 지금 팀원 모두가 찬성했는지 본다
    if all(votes.get(m) == YES for m in members):
        problem = join_problem(state.people(app.team_id), applicant)  # 승인 직전 재검사
        if problem:
            effects.append(("set_app", app.id, REJECTED, f"승인 직전 검사: {problem}"))
            return Outcome("거절", f"승인 직전 검사에서 규칙을 어김({problem})", effects)
        effects += [("set_app", app.id, APPROVED, ""), ("add_member", app.team_id, applicant_id)]
        return Outcome("승인", f"{applicant.name}이(가) {tname}의 팀원이 됨", effects)

    yes = sum(1 for m in members if votes.get(m) == YES)
    return Outcome("대기", f"찬성 {yes}/{len(members)}명, 나머지 팀원의 투표를 기다림", effects)


def close_recruitment(state: State) -> Outcome:
    if state.closed:
        return Outcome("막힘", "이미 모집이 마감됨")
    effects = []
    for app in state.apps.values():
        effects.append(("set_app", app.id, REJECTED, "모집 마감"))
    removed = 0
    for tid, ids in state.members.items():
        if len(ids) == 1:  # 1명뿐인 팀의 학생은 팀에서 빠짐
            effects.append(("remove_member", tid, ids[0]))
            removed += 1
    effects += [("set_setting", "closed", "true"), ("del_setting", "proposal")]
    return Outcome("승인", f"모집을 마감함(대기 지원 {len(state.apps)}건 거절, 팀에서 빠진 학생 {removed}명)", effects)


# ---------------------------------------------------------------- 배정 검사 (공고 2.5절 4항의 다섯 가지)
def check_assignment(state: State, assign: dict) -> list:
    """assign: {팀 id: [새로 넣을 학생 id, ...]}. 어긴 규칙 글의 목록을 돌려줌(빈 목록이면 통과)."""
    errors = []
    U = {p.id for p in state.unassigned()}

    def nm(i):
        return state.persons[i].name if i in state.persons else f"알 수 없는 학생({i})"

    flat = [sid for ids in assign.values() for sid in ids]
    seen, dup = set(), set()
    for sid in flat:
        (dup if sid in seen else seen).add(sid)
    if dup:
        errors.append("한 학생이 여러 팀에 배정됨: " + ", ".join(nm(i) for i in sorted(dup)))
    missing = U - seen
    if missing:
        errors.append("팀이 없는 학생이 배정되지 않음: " + ", ".join(nm(i) for i in sorted(missing)))
    extra = seen - U
    if extra:
        errors.append("팀이 없는 학생이 아닌 사람이 배정됨: " + ", ".join(nm(i) for i in sorted(extra)))
    unknown = [t for t in assign if t not in state.teams]
    if unknown:
        errors.append("없는 팀에 배정됨")

    for tid, tname in state.teams.items():
        added = [state.persons[i] for i in assign.get(tid, []) if i in U]
        final = state.people(tid) + added
        if not (MIN_TEAM <= len(final) <= MAX_TEAM):
            errors.append(f"{tname}의 인원이 {len(final)}명임({MIN_TEAM}~{MAX_TEAM}명이어야 함)")
        mains = [p.main for p in final]
        for role in sorted({r for r in mains if mains.count(r) > 1}):
            errors.append(f"{tname}에 주 역할이 같은 학생이 둘 이상임({role})")
        problem = required_problem(final)
        if problem:
            errors.append(f"{tname}에서 필수 포함 역할을 채우지 못함({problem})")
    return errors


# ---------------------------------------------------------------- 앱이 스스로 배정 (모든 경우를 따져 찾기)
def solve_assignment(state: State, node_limit: int = 2_000_000):
    """다섯 가지 검사를 모두 통과하는 배정을 끝까지 찾는다.

    돌려주는 값: (배정, 상태). 상태는 "found"(찾음), "none"(없음이 확실함), "limit"(너무 오래 걸려 중단).
    공고 6.2절 9항에 따라 팀 없는 학생은 8명 이하이므로 보통 순식간에 끝난다.
    """
    team_ids = list(state.teams)
    cur = {t: list(state.people(t)) for t in team_ids}
    base_len = {t: len(cur[t]) for t in team_ids}

    def options(p):
        return sum(1 for t in team_ids if len(cur[t]) < MAX_TEAM and all(m.main != p.main for m in cur[t]))

    order = sorted(state.unassigned(), key=options)  # 갈 곳이 적은 학생부터
    nodes = [0]

    class _Stop(Exception):
        pass

    def final_ok():
        return all(MIN_TEAM <= len(cur[t]) <= MAX_TEAM and required_problem(cur[t]) is None for t in team_ids)

    def dfs(i):
        nodes[0] += 1
        if nodes[0] > node_limit:
            raise _Stop
        remaining = len(order) - i
        need = sum(max(0, MIN_TEAM - len(cur[t])) for t in team_ids)
        if need > remaining:  # 남은 학생으로 최소 인원을 못 채움
            return False
        if i == len(order):
            return final_ok()
        p = order[i]
        for t in sorted(team_ids, key=lambda t: len(cur[t])):
            m = cur[t]
            if len(m) >= MAX_TEAM or any(x.main == p.main for x in m):
                continue
            m.append(p)
            if dfs(i + 1):
                return True
            m.pop()
        return False

    try:
        ok = dfs(0)
    except _Stop:
        return None, "limit"
    if not ok:
        return None, "none"
    return {t: [p.id for p in cur[t][base_len[t]:]] for t in team_ids if len(cur[t]) > base_len[t]}, "found"


def explain_no_solution(state: State) -> list:
    """규칙을 지키는 배정이 없을 때 관리자에게 보여 줄 이유."""
    reasons = []
    U = state.unassigned()
    for tid, tname in state.teams.items():
        members = state.people(tid)
        pool = [] if len(members) >= MAX_TEAM else [p for p in U if all(p.main != m.main for m in members)]
        problem = required_problem(members + pool)
        if problem:
            reasons.append(f"{tname}에 {problem}")
    free = sum(MAX_TEAM - len(state.members.get(t, [])) for t in state.teams)
    need_min = sum(max(0, MIN_TEAM - len(state.members.get(t, []))) for t in state.teams)
    if len(U) > free:
        reasons.append(f"팀의 남은 자리({free}개)보다 팀이 없는 학생({len(U)}명)이 많음")
    if len(U) < need_min:
        reasons.append(f"팀이 없는 학생({len(U)}명)이 모자라 모든 팀을 {MIN_TEAM}명 이상으로 만들 수 없음")
    if not reasons:
        reasons.append("팀 인원, 주 역할 중복, 필수 포함 역할을 한꺼번에 지키는 배정이 없음")
    return reasons


def assign_precheck(state: State):
    """AI 배정을 누르자마자 하는 검사. (막힘 Outcome 또는 None, 화면에 남길 글 목록)을 돌려줌."""
    if not state.closed:
        return Outcome("막힘", "모집을 먼저 마감해야 함"), ["모집을 먼저 마감해야 함"]
    _, status = solve_assignment(state)
    if status == "none":
        msg = "규칙을 지키는 배정이 없음: " + "; ".join(explain_no_solution(state))
        return Outcome("막힘", msg), [msg]
    if not state.unassigned():
        return Outcome("막힘", "팀이 없는 학생이 없음"), ["팀이 없는 학생이 없음"]
    return None, []


def confirm_proposal(state: State) -> Outcome:
    if not state.closed:
        return Outcome("막힘", "모집을 먼저 마감해야 함")
    if not state.proposal:
        return Outcome("막힘", "확정할 배정안이 없음. 먼저 AI 배정을 하세요")
    assign = state.proposal["assign"]
    errors = check_assignment(state, assign)
    if errors:
        return Outcome("막힘", "배정안이 지금 상태와 맞지 않아 확정하지 않음: " + "; ".join(errors[:3]),
                       [("del_setting", "proposal")])
    effects = [("add_member", t, sid) for t, ids in assign.items() for sid in ids]
    effects.append(("del_setting", "proposal"))
    return Outcome("승인", f"배정을 확정함(학생 {sum(len(v) for v in assign.values())}명)", effects)


# ---------------------------------------------------------------- AI 요청 글과 답 읽기
def build_prompt(state: State) -> str:
    def line(p):
        subs = ", ".join(p.subs) if p.subs else "없음"
        return f"- {p.no} {p.name} (주 역할: {p.main}, 보조 역할: {subs})"

    out = [
        "당신은 대학 팀 프로젝트의 팀 편성을 돕는 도우미입니다. 팀이 없는 학생을 아래 팀에 배정하세요.",
        "",
        "[규칙: 모두 지켜야 함]",
        "1. 이미 팀에 있는 학생은 옮기지 않습니다. 팀이 없는 학생만 팀에 넣습니다.",
        "2. 팀이 없는 학생은 한 명도 빠짐없이, 한 팀에만 배정합니다.",
        f"3. 배정이 끝난 뒤 모든 팀은 {MIN_TEAM}명 이상 {MAX_TEAM}명 이하여야 합니다(빈 팀 없음).",
        "4. 한 팀 안에 주 역할이 같은 학생이 2명 이상이면 안 됩니다.",
        "5. 모든 팀에는 기획자를 맡을 수 있는 학생과 웹 프로그래머를 맡을 수 있는 학생이 서로 다른 사람으로 1명씩 있어야 합니다. "
        "(주 역할이든 보조 역할이든 상관없음)",
        "",
        "[현재 팀]",
    ]
    for tid, tname in state.teams.items():
        ppl = state.people(tid)
        out.append(f"{tname}: 현재 {len(ppl)}명" + ("" if ppl else " (빈 팀)"))
        out += [line(p) for p in ppl]
    out += ["", "[팀이 없는 학생]"] + [line(p) for p in state.unassigned()]
    names = ", ".join(f'"{n}"' for n in state.teams.values())
    out += [
        "",
        "[출력 형식]",
        "설명 없이 JSON 객체 하나만 출력합니다. 키는 팀 이름(" + names + " 전부), "
        "값은 그 팀에 새로 넣을 학생의 학번(문자열) 목록입니다. 새로 넣을 학생이 없는 팀은 빈 목록으로 씁니다.",
        '예: {"팀1": ["2026001", "2026002"], "팀2": []}',
    ]
    return "\n".join(out)


def extract_json(text: str):
    """AI 답에서 첫 번째 JSON 객체를 꺼낸다(코드 블록이나 앞뒤 설명이 있어도 됨)."""
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1)
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except ValueError:
                        break
        start = text.find("{", start + 1)
    return None


def parse_ai_assignment(text: str, state: State):
    """AI 답 -> ({팀 id: [학생 id]}, 오류 목록). 오류가 있으면 배정안으로 쓰지 않는다."""
    obj = extract_json(text)
    if not isinstance(obj, dict) or not obj:
        return {}, ["답에서 JSON 배정안을 찾지 못함"]
    if len(obj) == 1:
        only = next(iter(obj.values()))
        if isinstance(only, dict):
            obj = only
    by_no = {p.no: p.id for p in state.persons.values()}
    by_team = {name: tid for tid, name in state.teams.items()}

    def team_id(key):
        k = str(key).strip()
        if k in by_team:
            return by_team[k]
        m = re.search(r"\d+", k)
        return by_team.get(f"팀{int(m.group())}") if m else None

    errors, assign = [], {}
    if all(not isinstance(v, list) for v in obj.values()):  # {학번: 팀} 모양으로 답한 경우
        pairs = [(v, [k]) for k, v in obj.items()]
    else:
        pairs = list(obj.items())
    for key, vals in pairs:
        tid = team_id(key)
        if tid is None:
            errors.append(f"모르는 팀 이름: {key}")
            continue
        if not isinstance(vals, list):
            errors.append(f"{key}의 값이 목록이 아님")
            continue
        for x in vals:
            sid = by_no.get(str(x).strip())
            if sid is None:
                errors.append(f"모르는 학번: {x}")
            else:
                assign.setdefault(tid, []).append(sid)
    return assign, errors


# ---------------------------------------------------------------- 명단 CSV 읽기 (공고 2.6절)
def parse_roster_csv(data: bytes):
    """CSV 바이트 -> (학생 목록, 오류 목록). BOM이 붙은 UTF-8(utf-8-sig)도 읽는다."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return [], ["파일이 UTF-8이 아님"]
    reader = csv.DictReader(io.StringIO(text))
    headers = [(h or "").strip() for h in (reader.fieldnames or [])]
    need = ["student_no", "name", "main_role", "sub_roles"]
    if any(h not in headers for h in need):
        return [], ["첫 줄은 student_no,name,main_role,sub_roles 이어야 함"]
    rows, errors, seen = [], [], set()
    for n, raw in enumerate(reader, start=2):
        row = {(k or "").strip(): (v or "").strip() for k, v in raw.items() if k is not None}
        if not any(row.values()):
            continue
        no, name, main = row.get("student_no", ""), row.get("name", ""), row.get("main_role", "")
        subs = [s.strip() for s in row.get("sub_roles", "").split(";") if s.strip()]
        if not no or not name:
            errors.append(f"{n}번째 줄: 학번이나 이름이 비어 있음")
            continue
        if no.lower() == "admin":
            errors.append(f"{n}번째 줄: 학번 admin 은 쓸 수 없음")
        if no in seen:
            errors.append(f"{n}번째 줄: 학번 {no} 이 겹침")
        seen.add(no)
        if main not in ROLES:
            errors.append(f"{n}번째 줄: 주 역할 '{main}' 이 6개 역할이 아님")
        bad = [s for s in subs if s not in ROLES]
        if bad:
            errors.append(f"{n}번째 줄: 보조 역할 '{bad[0]}' 이 6개 역할이 아님")
        subs = [s for i, s in enumerate(subs) if s != main and s not in subs[:i]]
        if len(subs) > 2:
            errors.append(f"{n}번째 줄: 보조 역할은 2개까지임")
        rows.append({"no": no, "name": name, "main": main, "subs": subs})
    if not rows and not errors:
        errors.append("학생이 한 명도 없음")
    return rows, errors


# ---------------------------------------------------------------- DB가 준 JSON -> State
def state_from_json(d: dict) -> State:
    s = State()
    for r in d["students"]:
        s.persons[r["id"]] = Person(r["id"], r["no"], r["name"], r["main"], tuple(r["subs"] or ()))
    for t in d["teams"]:
        s.teams[t["id"]] = t["name"]
        s.members[t["id"]] = []
    for m in d["members"]:
        s.members.setdefault(m["team_id"], []).append(m["student_id"])
    for a in d["apps"]:
        s.apps[a["id"]] = App(a["id"], a["team_id"], a["student_id"], PENDING)
    for a in d["latest"]:
        s.latest[a["student_id"]] = App(a["id"], a["team_id"], a["student_id"], a["status"], a.get("reason") or "")
    for v in d["votes"]:
        s.votes.setdefault(v["app_id"], {})[v["voter_id"]] = v["vote"]
    s.closed = d.get("closed") == "true"
    prop = d.get("proposal")
    if prop:
        p = json.loads(prop)
        p["assign"] = {int(k): [int(x) for x in v] for k, v in p["assign"].items()}
        s.proposal = p
    return s
