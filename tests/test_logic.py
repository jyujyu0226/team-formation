"""logic.py 테스트. 실행: python -m unittest discover -s tests -v   (또는 pytest)

DB 없이 메모리 안에서 공고 부록의 연습용 시나리오 3개(쉬움, 중간, 어려움)를 그대로 따라 한다.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import logic  # noqa: E402
from logic import App, Person, State, YES, NO  # noqa: E402

DATA = os.path.join(os.path.dirname(__file__), "..", "data", "practice")


class Mem:
    """DB 대신 State를 직접 고치는 가짜 저장소. effects를 DB와 같은 뜻으로 처리한다."""

    def __init__(self, csv_name, team_count):
        with open(os.path.join(DATA, csv_name), "rb") as f:
            rows, errors = logic.parse_roster_csv(f.read())
        assert not errors, errors
        persons = {i: Person(i, r["no"], r["name"], r["main"], tuple(r["subs"])) for i, r in enumerate(rows, 1)}
        teams = {t: f"팀{t}" for t in range(1, team_count + 1)}
        self.s = State(persons=persons, teams=teams, members={t: [] for t in teams})
        self.next_app = 1

    def who(self, n):  # "학생n(" 으로 시작하는 학생의 id
        return next(p.id for p in self.s.persons.values() if p.name.startswith(f"학생{n}("))

    def run(self, fn, *args):
        out = fn(self.s, *args)
        for e in out.effects:
            self._apply(e)
        return out

    def _apply(self, e):
        s, kind = self.s, e[0]
        if kind == "add_app":
            _, tid, sid, status, reason = e
            app = App(self.next_app, tid, sid, status, reason)
            self.next_app += 1
            s.latest[sid] = app
            if status == logic.PENDING:
                s.apps[app.id] = app
        elif kind == "set_app":
            _, aid, status, reason = e
            app = next(a for a in s.latest.values() if a.id == aid)
            app.status, app.reason = status, reason
            s.apps.pop(aid, None)
        elif kind == "add_member":
            s.members[e[1]].append(e[2])
        elif kind == "remove_member":
            s.members[e[1]].remove(e[2])
        elif kind == "add_vote":
            s.votes.setdefault(e[1], {})[e[2]] = e[3]
        elif kind == "set_setting" and e[1] == "closed":
            s.closed = e[2] == "true"
        elif kind == "del_setting":
            s.proposal = None

    # 시나리오에서 쓰는 동작
    def apply(self, who, team):
        return self.run(logic.apply_to_team, self.who(who), team)

    def vote(self, voter, applicant, vote):
        return self.run(logic.cast_vote, self.who(voter), self.who(applicant), vote)

    def close(self):
        return self.run(logic.close_recruitment)

    def assign_and_confirm(self):
        assign, status = logic.solve_assignment(self.s)
        assert status == "found", status
        assert logic.check_assignment(self.s, assign) == [], logic.check_assignment(self.s, assign)
        self.s.proposal = {"assign": assign, "source": "test"}
        return self.run(logic.confirm_proposal)

    def assert_all_rules_kept(self):
        assert self.s.unassigned() == [], [p.name for p in self.s.unassigned()]
        for tid, tname in self.s.teams.items():
            ppl = self.s.people(tid)
            assert 2 <= len(ppl) <= 4, (tname, len(ppl))
            assert len({p.main for p in ppl}) == len(ppl), (tname, "주 역할 중복")
            assert logic.required_problem(ppl) is None, (tname, logic.required_problem(ppl))


class Scenarios(unittest.TestCase):
    def check(self, out, kind):
        self.assertEqual(out.kind, kind, out.text)

    def test_easy(self):
        m = Mem("쉬움.csv", 3)
        for who, team in [(1, 1), (2, 2), (3, 3)]:
            self.check(m.apply(who, team), "승인")
        self.check(m.apply(4, 1), "대기"); self.check(m.vote(1, 4, YES), "승인")
        self.check(m.apply(5, 2), "대기"); self.check(m.vote(2, 5, YES), "승인")
        self.check(m.apply(7, 1), "대기")
        self.check(m.vote(1, 7, YES), "대기")
        self.check(m.vote(4, 7, NO), "거절")
        self.check(m.apply(6, 3), "대기"); self.check(m.vote(3, 6, YES), "승인")
        self.check(m.close(), "승인")
        self.check(m.assign_and_confirm(), "승인")
        m.assert_all_rules_kept()

    def test_medium(self):
        m = Mem("중간.csv", 4)
        self.check(m.apply(1, 1), "승인")
        self.check(m.apply(5, 1), "대기"); self.check(m.vote(1, 5, YES), "승인")
        self.check(m.apply(2, 1), "막힘")  # 주 역할 중복(기획자)
        self.check(m.apply(6, 2), "승인")
        self.check(m.apply(9, 2), "대기"); self.check(m.vote(6, 9, YES), "승인")
        self.check(m.apply(10, 2), "대기")
        self.check(m.vote(6, 10, YES), "대기")
        self.check(m.vote(9, 10, NO), "거절")
        self.check(m.apply(3, 3), "승인"); self.check(m.apply(11, 4), "승인")
        self.check(m.apply(12, 4), "대기")   # 아무도 투표하지 않음
        self.check(m.apply(12, 3), "막힘")   # 결과를 기다리는 지원이 있음
        self.check(m.close(), "승인")
        self.assertEqual(m.s.latest[m.who(12)].status, logic.REJECTED)
        self.assertEqual({p.name for p in m.s.unassigned()} >= {m.s.persons[m.who(3)].name, m.s.persons[m.who(11)].name}, True)
        self.check(m.apply(2, 3), "막힘")    # 마감
        self.check(m.assign_and_confirm(), "승인")
        m.assert_all_rules_kept()

    def test_hard(self):
        m = Mem("어려움.csv", 4)
        self.check(m.apply(1, 1), "승인")
        self.check(m.apply(4, 1), "대기"); self.check(m.vote(1, 4, YES), "승인")
        self.check(m.apply(7, 1), "대기"); self.check(m.vote(1, 7, YES), "대기"); self.check(m.vote(4, 7, YES), "승인")
        self.check(m.apply(2, 2), "승인")
        self.check(m.apply(8, 2), "대기"); self.check(m.vote(2, 8, YES), "승인")
        self.check(m.apply(5, 2), "대기"); self.check(m.apply(6, 2), "대기")  # 한 팀에 지원 둘
        self.check(m.vote(2, 5, YES), "대기"); self.check(m.vote(8, 5, YES), "승인")
        self.check(m.vote(2, 6, YES), "대기"); self.check(m.vote(8, 6, YES), "대기")
        self.check(m.vote(5, 6, YES), "거절")  # 승인 직전 재검사: 주 역할 웹 프로그래머 중복
        self.check(m.apply(11, 3), "승인")
        self.check(m.apply(15, 3), "대기"); self.check(m.vote(11, 15, YES), "승인")
        self.check(m.apply(14, 3), "막힘")   # 필수 포함 역할 자리가 모자람
        self.check(m.apply(10, 4), "승인")
        self.check(m.apply(13, 1), "대기"); self.check(m.vote(1, 13, YES), "대기")
        self.check(m.close(), "승인")
        self.assertEqual(m.s.latest[m.who(13)].status, logic.REJECTED)
        self.assertIn(m.who(10), [p.id for p in m.s.unassigned()])  # 팀4에서 빠짐
        self.check(m.assign_and_confirm(), "승인")
        m.assert_all_rules_kept()


class Rules(unittest.TestCase):
    def P(self, i, main, *subs):
        return Person(i, str(i), f"학생{i}", main, tuple(subs))

    def test_seat_rule_examples_from_notice(self):
        # 테스터와 발표자만 있는 팀(2명)에 보고서 작성자가 지원하면 막힘
        team = [self.P(1, "테스터"), self.P(2, "발표자")]
        self.assertIsNotNone(logic.join_problem(team, self.P(3, "보고서 작성자")))
        # 주 역할 기획자, 보조 역할 웹 프로그래머인 학생 혼자 있는 팀은 필수 역할 2개 중 1개만 채움
        self.assertEqual(logic.unfilled_required([self.P(1, "기획자", "웹 프로그래머")]), 1)
        self.assertEqual(logic.unfilled_required([self.P(1, "기획자"), self.P(2, "웹 프로그래머")]), 0)
        self.assertEqual(logic.unfilled_required([self.P(1, "테스터")]), 2)

    def test_full_team_and_same_main(self):
        team = [self.P(i, r) for i, r in enumerate(["기획자", "웹 프로그래머", "테스터", "발표자"], 1)]
        self.assertEqual(logic.join_problem(team, self.P(9, "보고서 작성자")), "팀이 꽉 참")
        self.assertEqual(logic.join_problem(team[:2], self.P(9, "기획자")), "같은 주 역할의 팀원이 있음")

    def test_vote_cannot_change_and_only_members_vote(self):
        m = Mem("쉬움.csv", 3)
        m.apply(1, 1)
        m.apply(4, 1)
        self.check = lambda out, kind: self.assertEqual(out.kind, kind, out.text)
        self.check(m.vote(2, 4, YES), "막힘")  # 팀원이 아님
        self.check(m.vote(1, 4, YES), "승인")
        self.check(m.vote(1, 4, NO), "승인")  # 이미 결과가 난 지원

    def test_new_member_must_vote_on_pending(self):
        # 내가 지원한 뒤 다른 지원자가 먼저 승인되어 팀원이 되면, 그 사람도 내 지원에 투표해야 한다
        m = Mem("어려움.csv", 4)
        m.apply(2, 2); m.apply(8, 2); m.vote(2, 8, YES)
        m.apply(5, 2); m.apply(7, 2)  # 학생7(FD;PT;TT) 주 역할이 학생8과 같은 FD라 이미 막힘이 정상
        # 학생5, 학생6 두 지원: 학생5가 먼저 승인되면 학생5도 학생6 지원에 투표해야 승인됨
        m.apply(6, 2)
        out = m.vote(2, 5, YES); self.assertEqual(out.kind, "대기")
        out = m.vote(8, 5, YES); self.assertEqual(out.kind, "승인")
        out = m.vote(2, 6, YES); self.assertEqual(out.kind, "대기")
        out = m.vote(8, 6, YES); self.assertEqual(out.kind, "대기")  # 학생5의 투표가 아직 없음

    def test_no_solution_message(self):
        # 팀3에 웹 프로그래머를 맡을 학생이 없고 자리도 없는 경우
        persons = {i: self.P(i, r) for i, r in enumerate(["기획자", "테스터", "발표자", "보고서 작성자", "웹 프로그래머"], 1)}
        s = State(persons=persons, teams={1: "팀3"}, members={1: [1, 2, 3, 4]}, closed=True)
        assign, status = logic.solve_assignment(s)
        self.assertEqual(status, "none")
        out, _ = logic.assign_precheck(s)
        self.assertEqual(out.kind, "막힘")
        self.assertIn("규칙을 지키는 배정이 없음", out.text)
        self.assertIn("팀3에 웹 프로그래머를 맡을 학생이 없음", out.text)


class AiAnswer(unittest.TestCase):
    def setUp(self):
        self.m = Mem("쉬움.csv", 3)

    def test_parse_variants_and_validation(self):
        s = self.m.s
        s.closed = True
        good, status = logic.solve_assignment(s)
        self.assertEqual(status, "found")
        # 팀 id -> 학번 목록으로 바꾼 뒤 AI가 코드 블록과 설명을 붙여 답한 경우
        import json
        body = {s.teams[t]: [s.persons[i].no for i in ids] for t, ids in good.items()}
        text = "생각해 보면 다음과 같습니다.\n```json\n" + json.dumps(body, ensure_ascii=False) + "\n```\n끝."
        assign, errors = logic.parse_ai_assignment(text, s)
        self.assertEqual(errors, [])
        self.assertEqual(logic.check_assignment(s, assign), [])

    def test_bad_answers_are_rejected(self):
        s = self.m.s
        s.closed = True
        assign, errors = logic.parse_ai_assignment("죄송합니다. 배정할 수 없습니다.", s)
        self.assertTrue(errors)
        # 한 학생을 두 팀에 넣고 한 명을 빠뜨린 배정안
        text = '{"팀1": ["2026101", "2026102"], "팀2": ["2026102"], "팀3": []}'
        assign, errors = logic.parse_ai_assignment(text, s)
        self.assertEqual(errors, [])
        problems = logic.check_assignment(s, assign)
        self.assertTrue(any("여러 팀" in p for p in problems))
        self.assertTrue(any("배정되지 않음" in p for p in problems))

    def test_student_to_team_shape(self):
        s = self.m.s
        text = '{"2026101": 1, "2026102": 1}'
        assign, errors = logic.parse_ai_assignment(text, s)
        self.assertEqual(errors, [])
        self.assertEqual(len(assign[1]), 2)


class Csv(unittest.TestCase):
    def test_bom_and_empty_sub_roles(self):
        raw = "\ufeffstudent_no,name,main_role,sub_roles\n2026002,라마바,웹 프로그래머,\n2026003,사아자,테스터,기획자;발표자\n".encode("utf-8")
        rows, errors = logic.parse_roster_csv(raw)
        self.assertEqual(errors, [])
        self.assertEqual(rows[0]["subs"], [])
        self.assertEqual(rows[1]["subs"], ["기획자", "발표자"])

    def test_bad_files(self):
        _, e = logic.parse_roster_csv("a,b\n1,2\n".encode())
        self.assertTrue(e)
        _, e = logic.parse_roster_csv("student_no,name,main_role,sub_roles\n1,가,없는역할,\n".encode())
        self.assertTrue(e)
        _, e = logic.parse_roster_csv("student_no,name,main_role,sub_roles\nadmin,가,테스터,\n".encode())
        self.assertTrue(e)

    def test_practice_files_load(self):
        for name, n in [("쉬움.csv", 8), ("중간.csv", 12), ("어려움.csv", 16)]:
            with open(os.path.join(DATA, name), "rb") as f:
                rows, errors = logic.parse_roster_csv(f.read())
            self.assertEqual((len(rows), errors), (n, []))


if __name__ == "__main__":
    unittest.main()
