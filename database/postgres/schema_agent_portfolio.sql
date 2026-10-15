BEGIN;

CREATE SCHEMA IF NOT EXISTS agent;

CREATE TABLE IF NOT EXISTS agent.watchlists (
  watchlist_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL UNIQUE,
  description text,
  owner_key text NOT NULL DEFAULT 'default',
  status text NOT NULL DEFAULT 'active',
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT watchlists_status_chk CHECK (status IN ('active', 'archived'))
);

CREATE TABLE IF NOT EXISTS agent.watchlist_items (
  watchlist_id uuid NOT NULL REFERENCES agent.watchlists(watchlist_id) ON DELETE CASCADE,
  stock_code text NOT NULL,
  thesis text,
  tags text[] NOT NULL DEFAULT '{}'::text[],
  priority smallint NOT NULL DEFAULT 3,
  added_at timestamptz NOT NULL DEFAULT now(),
  removed_at timestamptz,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  PRIMARY KEY (watchlist_id, stock_code),
  CONSTRAINT watchlist_priority_chk CHECK (priority BETWEEN 1 AND 5)
);

CREATE INDEX IF NOT EXISTS watchlist_items_code_idx ON agent.watchlist_items (stock_code) WHERE removed_at IS NULL;
CREATE INDEX IF NOT EXISTS watchlist_items_tags_idx ON agent.watchlist_items USING gin (tags);

CREATE TABLE IF NOT EXISTS agent.portfolios (
  portfolio_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL UNIQUE,
  description text,
  owner_key text NOT NULL DEFAULT 'default',
  base_currency text NOT NULL DEFAULT 'CNY',
  status text NOT NULL DEFAULT 'active',
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT portfolios_status_chk CHECK (status IN ('active', 'archived'))
);

CREATE TABLE IF NOT EXISTS agent.portfolio_holdings (
  portfolio_id uuid NOT NULL REFERENCES agent.portfolios(portfolio_id) ON DELETE CASCADE,
  stock_code text NOT NULL,
  as_of_date date NOT NULL,
  quantity numeric,
  market_value numeric,
  weight double precision,
  cost_basis numeric,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (portfolio_id, stock_code, as_of_date),
  CONSTRAINT portfolio_weight_chk CHECK (weight IS NULL OR weight >= 0)
);

CREATE INDEX IF NOT EXISTS portfolio_holdings_latest_idx ON agent.portfolio_holdings (portfolio_id, as_of_date DESC);
CREATE INDEX IF NOT EXISTS portfolio_holdings_code_idx ON agent.portfolio_holdings (stock_code, as_of_date DESC);

GRANT USAGE ON SCHEMA agent TO finance_app, finance_ai_reader;
GRANT SELECT, INSERT, UPDATE, DELETE ON agent.watchlists, agent.watchlist_items, agent.portfolios, agent.portfolio_holdings TO finance_app;
GRANT SELECT ON agent.watchlists, agent.watchlist_items, agent.portfolios, agent.portfolio_holdings TO finance_ai_reader;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA agent TO finance_app;

COMMIT;
