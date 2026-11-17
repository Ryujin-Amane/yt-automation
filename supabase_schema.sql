-- ════════════════════════════════════════════════════════════════
--  AutoTube AI — Supabase schema
--  Run this ONCE in Supabase → SQL Editor → New query → Run.
--  Safe to re-run (idempotent).
-- ════════════════════════════════════════════════════════════════

-- 1. Upload history / dedup / booked publish slots
create table if not exists public.processed_videos (
    id            bigserial primary key,
    tiktok_id     text unique not null,
    tiktok_user   text not null,
    youtube_id    text,
    channel_used  integer default 1,
    caption       text,
    scheduled_at  text,                       -- ISO UTC publishAt, e.g. 2026-11-21T09:30:00Z
    uploaded_at   timestamptz not null default now()
);

-- Older installs: add the new column if it is missing
alter table public.processed_videos add column if not exists scheduled_at text;

create index if not exists processed_videos_channel_uploaded_idx
    on public.processed_videos (channel_used, uploaded_at desc);
create index if not exists processed_videos_channel_scheduled_idx
    on public.processed_videos (channel_used, scheduled_at);

-- 2. Persistent app state (queue, settings, peak schedules) as JSON blobs
create table if not exists public.app_state (
    key         text primary key,
    value       jsonb not null,
    updated_at  timestamptz not null default now()
);

-- 3. Security: lock both tables down. The bot must use the SERVICE_ROLE key
--    (Project Settings → API → service_role) which bypasses RLS.
--    The public anon key will have NO access, which is what we want.
alter table public.processed_videos enable row level security;
alter table public.app_state        enable row level security;
