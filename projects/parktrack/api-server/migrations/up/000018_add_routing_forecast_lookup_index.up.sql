CREATE INDEX IF NOT EXISTS idx_forecasts_routing_lookup
ON forecasts (zone_id, predicted_for, generated_at DESC, forecast_id DESC);
