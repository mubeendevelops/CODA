-- pgcrypto supplies gen_random_uuid(), used as the default for every primary key.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- citext gives case-insensitive text equality/uniqueness, used for
-- users.email so Foo@x.com and foo@x.com cannot become two accounts.
CREATE EXTENSION IF NOT EXISTS citext;
