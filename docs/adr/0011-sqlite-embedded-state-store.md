# 0011. SQLite in WAL Mode as Zero-Dependency Transactional Store

We select SQLite with Write-Ahead Logging (WAL) mode enabled as the central persistence engine over Postgres, Redis, or DuckDB. Managing 200 symbol states and ~600 active orders represents a low-throughput OLTP workload (~10-50 writes/minute) with strict crash-resilience requirements; SQLite eliminates external database daemon dependencies and network latency, while WAL mode permits concurrent dashboard reads without blocking trading order writes.
