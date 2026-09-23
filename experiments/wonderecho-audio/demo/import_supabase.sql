-- Synthetic demo data ONLY. Run supabase_setup.sql first.
-- Existing keys win. No main safety-history tables are modified.
-- Routing rules are PC JSON, not SQL: use db_transfer.py rules to extract them.
BEGIN;
SET LOCAL standard_conforming_strings = on;
INSERT INTO public."settings" ("key", "value", "updated_at") VALUES ('follow_s', '20.0', '2026-09-20T11:59:28+09:00') ON CONFLICT DO NOTHING;
INSERT INTO public."settings" ("key", "value", "updated_at") VALUES ('follow_min_chars', '3', '2026-09-20T11:59:28+09:00') ON CONFLICT DO NOTHING;
INSERT INTO public."settings" ("key", "value", "updated_at") VALUES ('stt_prompt', '메카독, 비상정지, 긴급정지, 스톱, 수동모드, 수동제어, 자동모드, 수동해제, 순찰시작, 순찰정지, 순찰멈춰, 메카독 로봇 음성 명령.', '2026-09-20T11:59:28+09:00') ON CONFLICT DO NOTHING;
INSERT INTO public."settings" ("key", "value", "updated_at") VALUES ('robot_api_base', 'http://127.0.0.1:8000', '2026-09-20T11:59:28+09:00') ON CONFLICT DO NOTHING;
INSERT INTO public."roster" ("name", "note") VALUES ('김민수', '직원명단.txt') ON CONFLICT DO NOTHING;
INSERT INTO public."roster" ("name", "note") VALUES ('이지영', '직원명단.txt') ON CONFLICT DO NOTHING;
INSERT INTO public."roster" ("name", "note") VALUES ('박성훈', '직원명단.txt') ON CONFLICT DO NOTHING;
INSERT INTO public."roster" ("name", "note") VALUES ('최다은', '직원명단.txt') ON CONFLICT DO NOTHING;
INSERT INTO public."roster" ("name", "note") VALUES ('정우진', '직원명단.txt') ON CONFLICT DO NOTHING;
INSERT INTO public."roster" ("name", "note") VALUES ('한소연', '직원명단.txt') ON CONFLICT DO NOTHING;
COMMIT;
