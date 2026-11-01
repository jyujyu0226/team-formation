"""Supabase 무료 DB가 일시정지되지 않게 DB에 질의 한 번을 보낸다 (GitHub Actions 가 매일 실행).
DATABASE_URL 은 GitHub 저장소의 Settings > Secrets and variables > Actions 에 넣어 두며, 값은 출력하지 않는다."""
import os

import psycopg2

conn = psycopg2.connect(os.environ["DATABASE_URL"], connect_timeout=15)
with conn, conn.cursor() as cur:
    cur.execute("select count(*) from tf_settings")
    print("keepalive ok:", cur.fetchone()[0])
conn.close()
