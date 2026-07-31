"""
Cluster configuration for the read/write splitting router.

NODES describes every Postgres instance the router is allowed to talk to.
The router does NOT assume which one is primary — it discovers that at
runtime by polling `SELECT pg_is_in_recovery()` on each node. This matters
because in a real HA cluster (EFM switchover/failover), the primary can move
between nodes at any time, so hardcoding "node A is always primary" would be
wrong the moment a failover happens.
"""

import os

# List every node in the cluster. host/port should point at each Postgres
# instance directly (not through EFM or PEM).
NODES = [
    {"name": "edb-justin-01", "host": "192.168.118.120", "port": 5444},
    {"name": "edb-justin-02", "host": "192.168.118.121", "port": 5444},
]

DB_NAME = os.environ.get("PG_ROUTER_DB", "edb")
DB_USER = os.environ.get("PG_ROUTER_USER", "enterprisedb")
DB_PASSWORD = os.environ.get("PG_ROUTER_PASSWORD", "")

# How often (seconds) to re-check which node is primary vs standby.
HEALTH_CHECK_INTERVAL_SECONDS = 5

# Per-node connection pool size.
POOL_MIN_SIZE = 1
POOL_MAX_SIZE = 5

# Require SSL when talking to the cluster (matches the SSL-hardening work
# already done on the lab: pg_hba.conf rejects non-SSL connections).
SSL_MODE = os.environ.get("PG_ROUTER_SSLMODE", "require")
