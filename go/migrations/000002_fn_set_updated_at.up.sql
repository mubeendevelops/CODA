-- Shared trigger function: every table with an updated_at column gets a
-- BEFORE UPDATE trigger calling this, so updated_at cannot be forgotten or
-- spoofed by application code.
CREATE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
