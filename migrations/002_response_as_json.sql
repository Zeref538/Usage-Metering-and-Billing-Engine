-- jsonb stores keys in its own order, so a replayed response came back with the same
-- data but reordered keys. json keeps the text exactly as written: the replay is identical.
ALTER TABLE usage_events ALTER COLUMN response TYPE json USING response::json;
