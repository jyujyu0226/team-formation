"""DB(Supabase Postgres) 연결과 DB 작업.

- 연결은 한 번 만든 뒤 계속 다시 쓴다(연결 풀). 질의마다 연결을 새로 만들지 않는다. (공고 4.2절)
- 상태를 바꾸는 작업은 모두 "락 -> 현재 상태 읽기 -> logic.py 규칙 실행 -> 결과 반영"을 한 트랜잭션으로 한다.
  락(pg_advisory_xact_lock)이 있어서 두 사람이 동시에 눌러도 차례대로 처리된다.
- 키, DB 주소, 비밀번호는 코드에 없고 .env 또는 Streamlit Secrets 에서만 읽는다. 화면이나 로그에 출력하지 않는다.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time

import bcrypt
import psycopg2
import psycopg2.errors
import psycopg2.extras
import psycopg2.pool

import logic

try:  # 내 컴퓨터에서는 .env 를 읽는다 (배포한 앱에는 .env 가 없고 Streamlit Secrets 를 쓴다)
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

LOCK_KEY = 20261102      # 상태를 바꾸는 작업이 서로 기다리게 하는 락 번호
POOL_MAX = 6             # 동시에 열어 둘 연결 수

_pool = None
_pool_lock = threading.Lock()
_init_lock = threading.Lock()
_ready = False


class SlotError(Exception):
    """팀에 빈 자리가 없을 때"""


def get_secret(name, default=None):
    """환경변수(.env 포함) -> Streamlit Secrets 순서로 비밀 값을 읽는다."""
    value = os.environ.get(name)
    if value:
        return value
    try:
        import streamlit as st

        value = st.secrets.get(name)
        if value:
            return str(value)
    except Exception:
        pass
    return default


# ---------------------------------------------------------------- 연결 풀
def _get_conn():
    global _pool
    with _pool_lock:
        if _pool is None:
            url = get_secret("DATABASE_URL")
            if not url:
                raise RuntimeError("DATABASE_URL 이 설정되지 않음")
            _pool = psycopg2.pool.ThreadedConnectionPool(
                1, POOL_MAX, dsn=url, connect_timeout=10,
                keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3,
            )
        pool = _pool
    deadline = time.time() + 20
    while True:
        try:
            return pool.getconn()
        except psycopg2.pool.PoolError:  # 연결을 다 쓰는 중이면 조금 기다림
            if time.time() > deadline:
                raise
            time.sleep(0.05)


def run_tx(fn, lock=False):
    """연결 하나를 빌려 fn(cur)를 한 트랜잭션으로 실행한다. 끊어진 연결이면 새 연결로 한 번 다시 시도한다."""
    last = None
    for _ in range(2):
        conn = _get_conn()
        broken = False
        try:
            with conn.cursor() as cur:
                if lock:
                    cur.execute("select pg_advisory_xact_lock(%s)", (LOCK_KEY,))
                result = fn(cur)
            conn.commit()
            return result
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
            broken, last = True, e
            try:
                conn.rollback()
            except Exception:
                pass
        except Exception:
            try:
                conn.rollback()
            except Exception:
                broken = True
            raise
        finally:
            _pool.putconn(conn, close=broken or bool(conn.closed))
    raise last


# ---------------------------------------------------------------- 비밀번호 (bcrypt 해시)
def _b(pw: str) -> bytes:
    return pw.encode("utf-8")[:72]  # bcrypt 는 72바이트까지만 읽는다


def hash_pw(pw: str, rounds: int = 10) -> str:
    return bcrypt.hashpw(_b(pw), bcrypt.gensalt(rounds)).decode()


def check_pw(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(_b(pw), hashed.encode())
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------- 시작: 테이블 만들기, 관리자 계정
def ensure_ready():
    """앱이 시작할 때 한 번: schema.sql 을 실행하고 관리자 계정을 만들거나 비밀번호를 갱신한다."""
    global _ready
    if _ready:
        return
    with _init_lock:
        if _ready:
            return
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")
        with open(path, encoding="utf-8") as f:
            schema = f.read()
        admin_pw = get_secret("ADMIN_PASSWORD")
        if not admin_pw:
            raise RuntimeError("ADMIN_PASSWORD 가 설정되지 않음")

        def op(cur):
            cur.execute(schema)
            cur.execute("select id, password_hash from tf_students where student_no = %s", ("admin",))
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    "insert into tf_students (student_no, name, main_role, password_hash, is_admin) "
                    "values (%s, %s, null, %s, true)", ("admin", "관리자", hash_pw(admin_pw)))
            elif not check_pw(admin_pw, row[1]):  # 비밀 값이 바뀌었을 때만 갱신
                cur.execute("update tf_students set password_hash = %s, is_admin = true where id = %s",
                            (hash_pw(admin_pw), row[0]))

        run_tx(op, lock=True)
        _ready = True


def login(student_no: str, password: str):
    """학번과 비밀번호가 맞으면 {id, no, name, is_admin}, 아니면 None."""
    def op(cur):
        cur.execute("select id, student_no, name, password_hash, is_admin from tf_students where student_no = %s",
                    (student_no,))
        return cur.fetchone()

    row = run_tx(op)
    if row is None or not check_pw(password, row[3]):
        return None
    return {"id": row[0], "no": row[1], "name": row[2], "is_admin": bool(row[4])}


# ---------------------------------------------------------------- 현재 상태 읽기 (질의 한 번에 전부)
_STATE_SQL = """
select json_build_object(
  'closed',   (select value from tf_settings where key = 'closed'),
  'proposal', (select value from tf_settings where key = 'proposal'),
  'teams',    (select coalesce(json_agg(json_build_object('id', t.id, 'name', t.name) order by t.id), '[]'::json)
                 from tf_teams t),
  'students', (select coalesce(json_agg(json_build_object(
                    'id', s.id, 'no', s.student_no, 'name', s.name, 'main', s.main_role,
                    'subs', (select coalesce(json_agg(r.role order by r.role), '[]'::json)
                               from tf_student_sub_roles r where r.student_id = s.id)
                 ) order by s.id), '[]'::json)
                 from tf_students s where not s.is_admin),
  'members',  (select coalesce(json_agg(json_build_object('team_id', m.team_id, 'student_id', m.student_id)
                 order by m.team_id, m.slot_no), '[]'::json) from tf_members m),
  'apps',     (select coalesce(json_agg(json_build_object('id', a.id, 'team_id', a.team_id, 'student_id', a.student_id)
                 order by a.id), '[]'::json) from tf_applications a where a.status = '대기'),
  'latest',   (select coalesce(json_agg(json_build_object('id', x.id, 'team_id', x.team_id, 'student_id', x.student_id,
                 'status', x.status, 'reason', x.reason)), '[]'::json)
                 from (select distinct on (student_id) id, team_id, student_id, status, reason
                         from tf_applications order by student_id, id desc) x),
  'votes',    (select coalesce(json_agg(json_build_object('app_id', v.application_id, 'voter_id', v.voter_id,
                 'vote', v.vote)), '[]'::json)
                 from tf_votes v join tf_applications a on a.id = v.application_id where a.status = '대기')
)
"""


def load_state_cur(cur) -> logic.State:
    cur.execute(_STATE_SQL)
    return logic.state_from_json(cur.fetchone()[0])


def load_state() -> logic.State:
    return run_tx(load_state_cur)


# ---------------------------------------------------------------- logic.py 의 결과(effects)를 DB에 반영
def _apply_effects(cur, effects):
    for e in effects:
        kind = e[0]
        if kind == "add_app":
            _, team_id, sid, status, reason = e
            cur.execute("insert into tf_applications (team_id, student_id, status, reason) values (%s, %s, %s, %s)",
                        (team_id, sid, status, reason))
        elif kind == "set_app":
            _, app_id, status, reason = e
            cur.execute("update tf_applications set status = %s, reason = %s where id = %s", (status, reason, app_id))
        elif kind == "add_member":
            _, team_id, sid = e
            # 가장 낮은 빈 자리(1~4번)에 넣는다. 자리가 없으면 아무것도 넣지 못함
            cur.execute(
                "insert into tf_members (team_id, slot_no, student_id) "
                "select %s, s, %s from generate_series(1, 4) s "
                "where not exists (select 1 from tf_members m where m.team_id = %s and m.slot_no = s) "
                "order by s limit 1", (team_id, sid, team_id))
            if cur.rowcount != 1:
                raise SlotError()
        elif kind == "remove_member":
            cur.execute("delete from tf_members where team_id = %s and student_id = %s", (e[1], e[2]))
        elif kind == "add_vote":
            cur.execute("insert into tf_votes (application_id, voter_id, vote) values (%s, %s, %s)", (e[1], e[2], e[3]))
        elif kind == "set_setting":
            cur.execute("insert into tf_settings (key, value) values (%s, %s) "
                        "on conflict (key) do update set value = excluded.value", (e[1], e[2]))
        elif kind == "del_setting":
            cur.execute("delete from tf_settings where key = %s", (e[1],))
        else:
            raise ValueError(f"모르는 effect: {kind}")


def _is_admin(cur, user_id) -> bool:
    cur.execute("select is_admin from tf_students where id = %s", (user_id,))
    row = cur.fetchone()
    return bool(row and row[0])


def run_engine(user_id, fn, admin=False) -> logic.Outcome:
    """락을 잡고 현재 상태를 읽어 규칙 함수 fn(state)를 실행하고, 그 결과를 DB에 반영한다."""
    def op(cur):
        if admin and not _is_admin(cur, user_id):  # 학생 계정으로는 관리자 기능을 쓸 수 없다
            return logic.Outcome("막힘", "관리자만 쓸 수 있는 기능임")
        out = fn(load_state_cur(cur))
        _apply_effects(cur, out.effects)
        return out

    try:
        return run_tx(op, lock=True)
    except (psycopg2.errors.UniqueViolation, SlotError):
        # 락 덕분에 거의 일어나지 않지만, 일어나도 DB 유일성 제약이 먼저 도착한 한 명만 기록한다
        return logic.Outcome("막힘", "다른 사람이 먼저 같은 자리를 차지했음")


# 학생 동작
def apply_to_team(user_id, team_id):
    return run_engine(user_id, lambda s: logic.apply_to_team(s, user_id, team_id))


def cast_vote(user_id, applicant_id, vote):
    return run_engine(user_id, lambda s: logic.cast_vote(s, user_id, applicant_id, vote))


# 관리자 동작
def close_recruitment(user_id):
    return run_engine(user_id, logic.close_recruitment, admin=True)


def confirm_proposal(user_id):
    return run_engine(user_id, logic.confirm_proposal, admin=True)


def upload_roster(user_id, rows, team_count) -> logic.Outcome:
    """명단과 팀 수로 앱 상태를 처음으로 되돌린다(지원, 투표, 팀원, 마감 표시를 지우고 학생과 빈 팀을 만듦). 관리자 계정은 남긴다."""
    from psycopg2.extras import execute_values

    # 비밀번호 해시는 시간이 걸리므로 DB 락을 잡기 전에 미리 만든다
    hashed = [hash_pw(logic.DEFAULT_PASSWORD) for _ in rows]

    def op(cur):
        if not _is_admin(cur, user_id):
            return logic.Outcome("막힘", "관리자만 쓸 수 있는 기능임")
        cur.execute("delete from tf_students where not is_admin")  # 팀원, 지원, 투표, 보조 역할도 함께 지워짐
        cur.execute("delete from tf_teams")
        cur.execute("delete from tf_settings")
        execute_values(cur, "insert into tf_teams (name) values %s", [(f"팀{i}",) for i in range(1, team_count + 1)])
        ids = execute_values(
            cur, "insert into tf_students (student_no, name, main_role, password_hash, is_admin) values %s "
                 "returning id, student_no",
            [(r["no"], r["name"], r["main"], h, False) for r, h in zip(rows, hashed)], fetch=True)
        id_of = {no: i for i, no in ids}
        subs = [(id_of[r["no"]], role) for r in rows for role in r["subs"]]
        if subs:
            execute_values(cur, "insert into tf_student_sub_roles (student_id, role) values %s", subs)
        cur.execute("insert into tf_settings (key, value) values ('closed', 'false')")
        return logic.Outcome("승인", f"명단을 올렸음(학생 {len(rows)}명, 팀 {team_count}개)")

    try:
        return run_tx(op, lock=True)
    except psycopg2.errors.UniqueViolation:
        return logic.Outcome("막힘", "명단에 학번이 겹치거나 이미 있는 학번이 있음")


def save_proposal(user_id, assign, source):
    """검사를 통과한 배정안을 임시로 저장한다(확정 전). 관리자가 확정하면 팀원으로 기록된다."""
    value = json.dumps({"assign": {str(t): ids for t, ids in assign.items()}, "source": source}, ensure_ascii=False)

    def op(cur):
        if not _is_admin(cur, user_id):
            return False
        cur.execute("insert into tf_settings (key, value) values ('proposal', %s) "
                    "on conflict (key) do update set value = excluded.value", (value,))
        return True

    return run_tx(op, lock=True)


# ---------------------------------------------------------------- AI 호출 기록
def log_ai(model, outcome, detail=""):
    try:
        run_tx(lambda cur: cur.execute(
            "insert into tf_ai_logs (model, outcome, detail) values (%s, %s, %s)", (model, outcome, (detail or "")[:500])))
    except Exception:
        print("[경고] AI 기록을 저장하지 못함", file=sys.stderr)


def recent_ai_logs(limit=15):
    def op(cur):
        cur.execute(  # 배포 서버 시각은 UTC 이므로 한국 시각으로 바꿔서 보여 준다
            "select to_char(created_at at time zone 'Asia/Seoul', 'MM-DD HH24:MI:SS'), model, outcome, detail "
            "from tf_ai_logs order by id desc limit %s", (limit,))
        return cur.fetchall()

    return run_tx(op)
