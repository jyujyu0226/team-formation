"""팀 프로젝트 팀 편성 웹 앱 (Streamlit).

실행: streamlit run streamlit_app.py
화면 요소의 key 는 공고 2.7절의 표를 그대로 따른다(자동 채점 프로그램이 key 로 요소를 찾음).
"""
import re
import sys

import streamlit as st
from streamlit.errors import StreamlitAPIException

import ai
import db
import logic

REFRESH_SECONDS = 3  # 학생 화면이 다른 사람의 변경을 자동으로 다시 그리는 간격(공고: 5초 이하)

st.set_page_config(page_title="팀 편성", page_icon="👥", layout="wide")


# ---------------------------------------------------------------- 도우미
def log_error(where, e):
    # 비밀 값이 섞일 수 있으므로 오류 내용은 출력하지 않고 종류만 남긴다
    print(f"[오류] {where}: {type(e).__name__}", file=sys.stderr)


def team_number(name, fallback):
    m = re.search(r"\d+", name)
    return int(m.group()) if m else fallback


def set_result(text):
    st.session_state["result_msg"] = text


def run_action(fn, *args):
    """DB 작업을 실행하고 결과 안내 글(승인/대기/거절/막힘으로 시작)을 만든다."""
    try:
        return fn(*args).text
    except Exception as e:
        log_error(getattr(fn, "__name__", "action"), e)
        return "막힘: 처리 중 오류가 생겼음. 잠시 뒤 다시 시도해 주세요"


def rerun_fragment():
    try:
        st.rerun(scope="fragment")
    except StreamlitAPIException:  # 조각 다시 실행이 아닌 때에는 전체를 다시 실행
        st.rerun()


# ---------------------------------------------------------------- 단추를 누르면 먼저 실행되는 함수(callback)
def cb_login():
    no = (st.session_state.get("login_student_no") or "").strip()
    pw = st.session_state.get("login_password") or ""
    if not no or not pw:
        set_result("막힘: 학번과 비밀번호를 입력하세요")
        return
    try:
        user = db.login(no, pw)
    except Exception as e:
        log_error("login", e)
        set_result("막힘: DB에 연결하지 못했음. 잠시 뒤 다시 시도해 주세요")
        return
    if user is None:
        set_result("막힘: 학번 또는 비밀번호가 틀림")
    else:
        st.session_state["user"] = user
        set_result("")


def cb_logout():
    for k in ("user", "result_msg", "assign_log", "assign_job"):
        st.session_state.pop(k, None)


def cb_apply(team_id):
    user = st.session_state.get("user")
    if user:
        set_result(run_action(db.apply_to_team, user["id"], team_id))


def cb_vote(applicant_id, vote):
    user = st.session_state.get("user")
    if user:
        set_result(run_action(db.cast_vote, user["id"], applicant_id, vote))


def cb_start():
    user = st.session_state.get("user")
    file = st.session_state.get("admin_roster_file")
    count = int(st.session_state.get("admin_team_count") or 0)
    st.session_state["assign_log"] = []
    st.session_state["assign_job"] = False
    if not user or not user["is_admin"]:
        set_result("막힘: 관리자만 쓸 수 있는 기능임")
    elif file is None:
        set_result("막힘: 명단 CSV 파일을 먼저 올려 주세요")
    elif count < 1:
        set_result("막힘: 팀 수는 1 이상이어야 함")
    else:
        rows, errors = logic.parse_roster_csv(file.getvalue())
        if errors:
            set_result("막힘: " + "; ".join(errors[:3]))
        else:
            set_result(run_action(db.upload_roster, user["id"], rows, count))


def cb_close():
    user = st.session_state.get("user")
    if user and user["is_admin"]:
        set_result(run_action(db.close_recruitment, user["id"]))
    else:
        set_result("막힘: 관리자만 쓸 수 있는 기능임")


def cb_assign():
    """AI 배정 단추: 바로 알 수 있는 막힘(마감 전, 배정 불가)은 여기서 안내하고, 아니면 AI 호출을 예약한다."""
    user = st.session_state.get("user")
    st.session_state["assign_job"] = False
    if not user or not user["is_admin"]:
        set_result("막힘: 관리자만 쓸 수 있는 기능임")
        return
    try:
        blocked, lines = logic.assign_precheck(db.load_state())
    except Exception as e:
        log_error("assign_precheck", e)
        set_result("막힘: 처리 중 오류가 생겼음. 잠시 뒤 다시 시도해 주세요")
        return
    st.session_state["assign_log"] = lines
    if blocked:
        set_result(blocked.text)
    else:
        set_result("")
        st.session_state["assign_job"] = True


def cb_confirm():
    user = st.session_state.get("user")
    if user and user["is_admin"]:
        set_result(run_action(db.confirm_proposal, user["id"]))
        st.session_state["assign_log"] = []
    else:
        set_result("막힘: 관리자만 쓸 수 있는 기능임")


# ---------------------------------------------------------------- 로그인 화면
def login_view():
    st.title("팀 프로젝트 팀 편성")
    st.text_input("학번", key="login_student_no")
    st.text_input("비밀번호", type="password", key="login_password")
    st.button("로그인", key="login_submit", on_click=cb_login)
    with st.container(key="result"):
        msg = st.session_state.get("result_msg", "")
        if msg:
            st.write(msg)


# ---------------------------------------------------------------- 공통 조각
def render_team(state, tid, tname, idx, my_tid, show_apply, can_apply):
    n = team_number(tname, idx)
    with st.container(key=f"team_{n}", border=True):
        people = state.people(tid)
        mine = " (내 팀)" if tid == my_tid else ""
        st.markdown(f"**{tname}**{mine} · {len(people)}/{logic.MAX_TEAM}명")
        if people:
            st.markdown("\n".join(f"- {p.name} — {logic.role_text(p)}" for p in people))
        else:
            st.write("아직 팀원이 없음")
        if show_apply:
            st.button("이 팀에 지원", key=f"apply_{n}", on_click=cb_apply, args=(tid,), disabled=not can_apply)


def render_unassigned(state):
    with st.container(key="unassigned"):
        people = state.unassigned()
        st.markdown(f"**팀이 없는 학생 {len(people)}명**")
        if people:
            st.markdown("\n".join(f"- {p.name}" for p in people))


def db_error_box():
    st.error("DB에 연결하지 못했음. 잠시 뒤 '새로 고침'을 눌러 주세요.")
    st.button("새로 고침", key="refresh")


# ---------------------------------------------------------------- 학생 화면
@st.fragment(run_every=REFRESH_SECONDS)  # 이 조각만 3초마다 자동으로 다시 그림(공고 3장의 어려운 해결)
def student_view():
    user = st.session_state.get("user")
    if not user:
        return
    try:
        state = db.load_state()
    except Exception as e:
        log_error("load_state", e)
        db_error_box()
        return
    me = state.persons.get(user["id"])
    if me is None:  # 관리자가 명단을 다시 올려 이 계정이 사라진 경우
        st.session_state["user"] = None
        st.rerun()
        return

    with st.container(key="whoami"):
        st.write(f"로그인: {me.name} (학번 {me.no})")
    with st.container(key="result"):
        if st.session_state.get("result_msg"):
            st.write(st.session_state["result_msg"])

    st.caption("모집이 마감되었음" if state.closed else "모집 중")
    my_tid = state.team_of(me.id)
    pend = state.pending_of(me.id)
    if my_tid is not None:
        st.write(f"내 팀: {state.teams[my_tid]}")
    else:
        st.write("아직 팀이 없음")
    if pend is not None:
        st.write(f"내 지원: {state.teams[pend.team_id]}에 지원했고 결과를 기다리는 중")
    else:
        last = state.latest.get(me.id)
        if my_tid is None and last is not None and last.status == logic.REJECTED:
            st.write(f"최근 지원은 거절되었음({last.reason or '이유 없음'}). 다른 팀에 다시 지원할 수 있음")

    can_apply = (not state.closed) and my_tid is None and pend is None
    st.subheader("팀 목록")
    for idx, (tid, tname) in enumerate(state.teams.items(), start=1):
        render_team(state, tid, tname, idx, my_tid, True, can_apply)

    if my_tid is not None:
        st.subheader("우리 팀에 들어온 지원")
        incoming = [a for a in state.apps.values() if a.team_id == my_tid]
        if not incoming:
            st.write("투표할 지원이 없음")
        members = state.members.get(my_tid, [])
        for a in incoming:
            ap = state.persons[a.student_id]
            votes = state.votes.get(a.id, {})
            yes = sum(1 for m in members if votes.get(m) == logic.YES)
            st.write(f"{ap.name} — {logic.role_text(ap)} (찬성 {yes}/{len(members)}명)")
            if me.id in votes:  # 한 번 투표하면 바꿀 수 없으므로 단추 대신 내 투표를 보여 줌
                st.write(f"내 투표: {votes[me.id]}")
            else:
                c1, c2 = st.columns(2)
                c1.button("찬성", key=f"vote_yes_{ap.no}", on_click=cb_vote, args=(ap.id, logic.YES))
                c2.button("거부", key=f"vote_no_{ap.no}", on_click=cb_vote, args=(ap.id, logic.NO))

    render_unassigned(state)
    st.button("새로 고침", key="refresh")


# ---------------------------------------------------------------- AI 배정 (관리자)
def run_assign_job(box, user):
    """모델 1차, 2차, 3차 순서로 배정안을 받는다. 모두 실패하면 앱이 모든 경우를 따져 직접 배정한다."""
    lines = []

    def show(extra=None):
        box.markdown("\n\n".join(lines + ([extra] if extra else [])))

    try:
        state = db.load_state()
        prompt = logic.build_prompt(state)
        api_key = db.get_secret("OPENROUTER_API_KEY")
        found, source = None, ""
        for i, model in enumerate(ai.get_models(), start=1):
            label = f"{i}차 모델({model})"
            show(f"AI가 생각하는 중… {label}의 응답을 기다리는 중(최대 {ai.LIMIT_SECONDS}초)")
            text, err = ai.ask_model(
                model, prompt, api_key, ai.LIMIT_SECONDS,
                on_tick=lambda s, label=label: show(
                    f"AI가 생각하는 중… {label}의 응답을 기다리는 중({s}초 / 최대 {ai.LIMIT_SECONDS}초)"),
            )
            if err:
                lines.append(f"{label}: 실패({err}) → 다음 모델로 넘어감")
                db.log_ai(model, "실패", err)
                continue
            assign, errors = logic.parse_ai_assignment(text, state)
            if not errors:
                errors = logic.check_assignment(state, assign)
            if errors:
                lines.append(f"{label}: 배정안이 규칙 검사를 통과하지 못함({'; '.join(errors[:3])}) → 다음 모델로 넘어감")
                db.log_ai(model, "검사 탈락", "; ".join(errors)[:400])
                continue
            lines.append(f"{label}: 배정안이 규칙 검사를 통과함")
            db.log_ai(model, "성공", "")
            found, source = assign, label
            break

        if found is None:
            show("모든 AI 모델이 실패해 앱이 직접 배정하는 중…")
            found, status = logic.solve_assignment(state)
            if found is not None:
                source = "앱이 직접 배정(모든 경우를 따져 규칙을 지키는 배정을 찾음)"
                lines.append("AI 모델이 모두 실패해 앱이 직접 배정함")
            else:
                lines.append("앱이 직접 배정하려 했지만 규칙을 지키는 배정을 찾지 못함")

        if found is None:
            set_result("막힘: 규칙을 지키는 배정안을 만들지 못했음")
        elif db.save_proposal(user["id"], found, source):
            set_result(f"승인: 배정안을 만들었음({source}). 내용을 확인한 뒤 확정하세요")
        else:
            set_result("막힘: 관리자만 쓸 수 있는 기능임")
    except Exception as e:
        log_error("assign_job", e)
        lines.append("처리 중 오류가 생겼음")
        set_result("막힘: AI 배정 중 오류가 생겼음. 잠시 뒤 다시 시도해 주세요")
    st.session_state["assign_log"] = lines


# ---------------------------------------------------------------- 관리자 화면
@st.fragment  # 관리자 화면은 자동으로 다시 그리지 않고 '새로 고침' 단추를 쓴다(AI를 기다리는 동안 끊기지 않게)
def admin_view():
    user = st.session_state.get("user")
    if not user or not user["is_admin"]:
        return
    try:
        state = db.load_state()
    except Exception as e:
        log_error("load_state", e)
        db_error_box()
        return

    with st.container(key="whoami"):
        st.write(f"로그인: {user['name']} ({user['no']})")
    result_box = st.container(key="result")  # 아래 작업이 끝난 뒤 내용을 채움
    st.button("새로 고침", key="refresh")
    st.caption(("모집이 마감되었음" if state.closed else "모집 중") + f" · 대기 중인 지원 {len(state.apps)}건")

    st.subheader("1. 명단 올리기")
    st.file_uploader("명단 CSV 파일(UTF-8, BOM 가능)", type=["csv"], key="admin_roster_file")
    st.number_input("팀 수", min_value=1, max_value=10, value=3, step=1, key="admin_team_count")
    st.button("명단으로 처음 상태 만들기", key="admin_start", on_click=cb_start)

    st.subheader("2. 모집 마감")
    st.button("모집 마감", key="admin_close", on_click=cb_close)
    render_unassigned(state)

    st.subheader("3. AI 배정")
    st.button("AI 배정", key="admin_assign", on_click=cb_assign)
    job_ran = False
    with st.container(key="assign_status"):
        box = st.empty()
        if st.session_state.get("assign_job"):
            st.session_state["assign_job"] = False
            run_assign_job(box, user)
            job_ran = True
        else:
            lines = st.session_state.get("assign_log") or []
            box.markdown("\n\n".join(lines) if lines else "아직 AI 배정을 하지 않았음")
    if job_ran:
        rerun_fragment()  # 새 배정안과 결과 안내를 다시 그림

    st.subheader("4. 배정안 확정")
    if state.proposal:
        st.write(f"배정안({state.proposal.get('source', '')})")
        rows = []
        for tid, ids in state.proposal["assign"].items():
            for sid in ids:
                p = state.persons.get(sid)
                if p:
                    rows.append({"학생": p.name, "역할": logic.role_text(p), "배정된 팀": state.teams.get(tid, "?")})
        st.table(rows)
    else:
        st.write("확정할 배정안이 없음")
    st.button("배정안 확정", key="admin_confirm", on_click=cb_confirm)

    st.subheader("팀 목록")
    for idx, (tid, tname) in enumerate(state.teams.items(), start=1):
        render_team(state, tid, tname, idx, None, False, False)

    with st.expander("AI 호출 기록"):
        try:
            logs = db.recent_ai_logs()
            if logs:
                st.table([{"시각(한국)": a, "모델": b, "결과": c, "내용": d} for a, b, c, d in logs])
            else:
                st.write("아직 기록이 없음")
        except Exception as e:
            log_error("ai_logs", e)
            st.write("기록을 불러오지 못했음")

    with result_box:
        if st.session_state.get("result_msg"):
            st.write(st.session_state["result_msg"])


# ---------------------------------------------------------------- 시작
def main():
    try:
        db.ensure_ready()
    except Exception as e:
        log_error("ensure_ready", e)
        st.error("DB를 준비하지 못했음. DATABASE_URL, ADMIN_PASSWORD 설정을 확인하세요.")
        st.stop()
    user = st.session_state.get("user")
    if not user:
        login_view()
        return
    st.sidebar.button("로그아웃", key="logout", on_click=cb_logout)
    if user["is_admin"]:
        admin_view()
    else:
        student_view()


main()
