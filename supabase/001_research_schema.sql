-- Dedicated research tables. NEVER touches OS profit/loss/manual notes.
create table if not exists public.deriv_trade_research (
  trade_id            text primary key,
  signal_id           text,
  symbol              text,
  direction           text,
  outcome             text,
  pnl                 numeric,
  entry_analysis      text,
  exit_analysis       text,
  strategy_adherence  text,
  market_behaviour    text,
  confidence          numeric,
  strengths           jsonb not null default '[]',
  weaknesses          jsonb not null default '[]',
  mistakes            jsonb not null default '[]',
  pattern_detected    text,
  risk_observations   jsonb not null default '[]',
  suggested_improvements jsonb not null default '[]',
  technical_explanation text,
  ai_summary          text,
  model               text,
  created_at          timestamptz not null default now()
);
create index if not exists idx_dtr_symbol   on public.deriv_trade_research (symbol);
create index if not exists idx_dtr_created  on public.deriv_trade_research (created_at desc);

create table if not exists public.deriv_research_knowledge (
  id          bigserial primary key,
  kind        text not null,             -- win_pattern|losing_pattern|mistake|success|market_behaviour|improvement
  pattern_key text not null,
  description text,
  occurrences int  not null default 1,
  wins        int  not null default 0,
  losses      int  not null default 0,
  last_trade_id text,
  first_seen  timestamptz not null default now(),
  last_seen   timestamptz not null default now(),
  unique (kind, pattern_key)
);

-- FK to the Personal OS trades table, added ONLY if that table exists.
-- (Names substituted safely by the service; run manually with your real names if needed.)
do $$
begin
  if exists (select 1 from information_schema.tables
             where table_schema='public' and table_name='trades')
     and exists (select 1 from information_schema.columns
             where table_schema='public' and table_name='trades' and column_name='trade_id')
     and not exists (select 1 from information_schema.table_constraints
             where constraint_name='fk_dtr_os_trade') then
    alter table public.deriv_trade_research
      add constraint fk_dtr_os_trade
      foreign key (trade_id) references public.trades(trade_id) on delete cascade;
  end if;
end $$;
