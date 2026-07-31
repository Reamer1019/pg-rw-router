# pg-rw-router

A small FastAPI service that automatically routes database writes to the
current Postgres **primary** and reads to a **standby**, without ever
hardcoding which node holds which role.

## Why this exists

Most read/write splitting examples assume the primary never changes. In a
real HA cluster (this one is backed by EDB Postgres Advanced Server 18 +
EDB Failover Manager, running a 3-node Primary/Standby/Witness setup), the
primary *does* change — during a planned switchover or an automatic
failover. A router that hardcodes "node A = primary" breaks the moment
that happens.

This project instead polls `SELECT pg_is_in_recovery()` on every node every
few seconds (the same signal EFM itself relies on) and updates its routing
table in the background. Application code just asks for "a write
connection" or "a read connection" and never has to know or care which
physical node currently holds that role.

## Architecture

```
            ┌──────────────┐
 client --> │   FastAPI    │
            │  /notes GET  │──> read pool  --> current standby
            │  /notes POST │──> write pool --> current primary
            └──────┬───────┘
                    │
            ┌───────▼────────┐
            │  ClusterRouter │  <- background task polls
            └───┬────────┬───┘     pg_is_in_recovery() every 5s
                │        │
          node .120   node .121
         (asyncpg    (asyncpg
            pool)       pool)
```

## Try it

```bash
pip install -r requirements.txt
export PG_ROUTER_PASSWORD=your_password
uvicorn main:app --reload --port 8000
```

```bash
curl http://localhost:8000/health
curl -X POST http://localhost:8000/notes -H "Content-Type: application/json" \
     -d '{"body": "hello from the primary"}'
curl http://localhost:8000/notes
```

## The interesting demo

While the service is running, trigger a switchover on the lab cluster:

```bash
efm promote efm -switchover
```

Poll `/health` and watch the `role` field flip for both nodes within one
health-check interval — with no restart, no config change, and no dropped
writes beyond whatever the switchover itself takes.

## Notes / known limitations

- This is a connection-pool-level router, not a wire-protocol proxy like
  pgbouncer or pgpool — the client (FastAPI app) is aware of the router as
  a Python object, not a transparent network proxy. That's a reasonable
  next step if this project grows.
- Read-your-writes consistency isn't guaranteed: a read routed to a standby
  immediately after a write to the primary can see stale data until
  replication catches up. Worth calling out explicitly in an interview —
  it's a real tradeoff of any read/write split, not a bug.
- `get_read_pool()` falls back to the primary if no standby is reachable,
  so reads degrade gracefully instead of failing outright during a standby
  outage.
