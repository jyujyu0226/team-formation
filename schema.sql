-- 팀 편성 앱의 DB 테이블. 모든 이름이 tf_ 로 시작한다 (6장 실습 테이블과 같은 DB를 쓰기 때문).
-- 앱이 시작할 때 이 파일을 그대로 실행하므로(db.py ensure_ready), 이 파일이 실제 DB와 항상 같다.
-- 모든 문장이 "if not exists" 라서 여러 번 실행해도 안전하다. Supabase SQL 편집기에 붙여 넣어 실행해도 된다.

-- 학생과 관리자. 관리자는 student_no = 'admin', main_role 이 비어 있다.
create table if not exists tf_students (
    id            bigint generated always as identity primary key,
    student_no    text    not null unique,
    name          text    not null,
    main_role     text    check (main_role in ('기획자', '웹 프로그래머', '프론트엔드 디자이너', '테스터', '보고서 작성자', '발표자')),
    password_hash text    not null,                       -- bcrypt 해시. 비밀번호 원문은 저장하지 않는다
    is_admin      boolean not null default false,
    check (is_admin or main_role is not null)             -- 학생은 주 역할이 꼭 있어야 함
);

-- 보조 역할: 학생 1명에 0~2행
create table if not exists tf_student_sub_roles (
    student_id bigint not null references tf_students (id) on delete cascade,
    role       text   not null check (role in ('기획자', '웹 프로그래머', '프론트엔드 디자이너', '테스터', '보고서 작성자', '발표자')),
    primary key (student_id, role)
);

create table if not exists tf_teams (
    id   bigint generated always as identity primary key,
    name text   not null unique
);

-- 팀원. 유일성 제약이 두 가지를 DB에서 지켜 준다:
--   (team_id, slot_no) 기본 키: 같은 자리에 두 명이 동시에 들어오면 먼저 도착한 한 명만 기록됨
--   student_id unique    : 한 학생은 한 팀에만 속함
create table if not exists tf_members (
    team_id    bigint   not null references tf_teams (id) on delete cascade,
    slot_no    smallint not null check (slot_no between 1 and 4),   -- 다섯 번째 자리를 막음
    student_id bigint   not null unique references tf_students (id) on delete cascade,
    primary key (team_id, slot_no)
);

create table if not exists tf_applications (
    id         bigint generated always as identity primary key,
    team_id    bigint      not null references tf_teams (id) on delete cascade,
    student_id bigint      not null references tf_students (id) on delete cascade,
    status     text        not null check (status in ('대기', '승인', '거절')),
    reason     text,
    created_at timestamptz not null default now()
);

-- 한 학생은 결과를 기다리는 지원을 동시에 둘 가질 수 없다 (DB 수준의 안전장치)
create unique index if not exists tf_applications_one_pending
    on tf_applications (student_id) where status = '대기';

create table if not exists tf_votes (
    application_id bigint not null references tf_applications (id) on delete cascade,
    voter_id       bigint not null references tf_students (id) on delete cascade,
    vote           text   not null check (vote in ('찬성', '거부')),
    primary key (application_id, voter_id)                -- 한 번 투표하면 바꿀 수 없음
);

-- 모집 마감 여부(closed), 확정 전 배정안(proposal) 등
create table if not exists tf_settings (
    key   text primary key,
    value text not null
);

-- AI 호출 기록 (모델 이름, 결과). README의 AI 실패 사례를 찾을 때 쓴다
create table if not exists tf_ai_logs (
    id         bigint generated always as identity primary key,
    created_at timestamptz not null default now(),
    model      text,
    outcome    text,
    detail     text
);

-- 보안: Supabase 는 SQL 로 만든 표를 anon 키로 읽을 수 있게 열어 둘 수 있다(RLS 꺼짐).
-- 정책 없이 RLS 만 켜면 anon 키로는 아무것도 읽거나 쓸 수 없다. 앱은 DATABASE_URL(postgres 역할)로 접속하므로 영향이 없다.
alter table tf_students          enable row level security;
alter table tf_student_sub_roles enable row level security;
alter table tf_teams             enable row level security;
alter table tf_members           enable row level security;
alter table tf_applications      enable row level security;
alter table tf_votes             enable row level security;
alter table tf_settings          enable row level security;
alter table tf_ai_logs           enable row level security;
