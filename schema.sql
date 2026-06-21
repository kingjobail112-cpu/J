-- ডেটাবেস তৈরির SQL স্ক্রিপ্ট
-- চালান: psql -U your_user -d your_db -f schema.sql

CREATE TABLE IF NOT EXISTS users (
    id         SERIAL PRIMARY KEY,
    username   VARCHAR(100) NOT NULL,
    phone      VARCHAR(15)  NOT NULL UNIQUE,
    points     INTEGER      NOT NULL DEFAULT 0,
    created_at TIMESTAMP    NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS game_sessions (
    id            SERIAL PRIMARY KEY,
    user_id       INTEGER   NOT NULL REFERENCES users(id),
    game_id       INTEGER   NOT NULL,
    won           BOOLEAN   NOT NULL,
    points_earned INTEGER   NOT NULL DEFAULT 0,
    played_at     TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS ad_watches (
    id            SERIAL PRIMARY KEY,
    user_id       INTEGER   NOT NULL REFERENCES users(id),
    points_earned INTEGER   NOT NULL DEFAULT 0,
    watched_at    TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS withdrawals (
    id             SERIAL PRIMARY KEY,
    user_id        INTEGER       NOT NULL REFERENCES users(id),
    points         INTEGER       NOT NULL,
    amount         NUMERIC(10,2) NOT NULL,
    method         VARCHAR(20)   NOT NULL,
    account_number VARCHAR(20)   NOT NULL,
    status         VARCHAR(20)   NOT NULL DEFAULT 'pending',
    created_at     TIMESTAMP     NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS sessions (
    chat_id    BIGINT       PRIMARY KEY,
    phone      VARCHAR(15)  NOT NULL,
    updated_at TIMESTAMP    NOT NULL DEFAULT NOW()
);

-- ইনডেক্স
CREATE INDEX IF NOT EXISTS idx_users_phone       ON users(phone);
CREATE INDEX IF NOT EXISTS idx_game_sessions_uid ON game_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_ad_watches_uid    ON ad_watches(user_id);
CREATE INDEX IF NOT EXISTS idx_withdrawals_uid   ON withdrawals(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_phone    ON sessions(phone);
