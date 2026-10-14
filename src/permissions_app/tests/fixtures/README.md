# Test fixtures

`genie_space_get_serialized.json` — the response of
`GET /api/2.0/genie/spaces/{space_id}?include_serialized_space=true`, recorded
read-only from a dev workspace (2026-10-14) and sanitized:

* The envelope keys and the `serialized_space` *string* (JSON-in-JSON,
  `version: 2`) are exactly as returned. Its `data_sources` keys seen live were
  `tables` (`[{identifier, column_configs?}]`) and `volumes` (`[{path}]`).
* Catalog / schema / table names, ids, questions and SQL were replaced with
  generic values; column configs were trimmed.
* Live, the `tables` list held managed tables, views, materialized views and
  streaming tables (all distinguished only via the UC tables API
  `table_type`), and references to tables that no longer exist — mirrored
  here by `sales.dropped_table`.
* `finance.revenue_metrics` stands in for a metric view. No metric view existed
  in the workspace, so its placement under `data_sources.tables` is an
  assumption (metric views are UC table securables).
