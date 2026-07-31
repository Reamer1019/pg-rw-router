"""
Core routing logic.

ClusterRouter keeps one asyncpg connection pool per node, and a background
task that periodically asks each node "are you in recovery?" to figure out
which one is currently the primary and which are standbys. Application code
never picks a node directly — it asks the router for "a write connection"
or "a read connection" and the router hands back a connection from whichever
pool is currently correct.

Why poll instead of trusting a static config:
- After an EFM switchover/failover, the primary can move to a different
  node. A router that hardcodes "primary = node X" would keep sending
  writes to a demoted standby and fail.
- Polling `pg_is_in_recovery()` is cheap and is the same signal EFM itself
  uses to reason about node roles.
"""

import asyncio
import logging
import random
from dataclasses import dataclass, field
from typing import Optional

import asyncpg

import config

logger = logging.getLogger("pg_router")


@dataclass
class NodeState:
    name: str
    host: str
    port: int
    pool: Optional[asyncpg.Pool] = None
    is_primary: bool = False
    reachable: bool = False


class NoPrimaryAvailable(Exception):
    pass


class NoReplicaAvailable(Exception):
    pass

class AmbiguousPrimaryError(Exception):
    pass


class ClusterRouter:
    def __init__(self, nodes=None):
        node_defs = nodes if nodes is not None else config.NODES
        self.nodes: list[NodeState] = [
            NodeState(name=n["name"], host=n["host"], port=n["port"])
            for n in node_defs
        ]
        self._health_task: Optional[asyncio.Task] = None
        self._stopping = False

    async def start(self):
        """Open a pool per node and kick off the background health checker."""
        for node in self.nodes:
            try:
                node.pool = await asyncpg.create_pool(
                    host=node.host,
                    port=node.port,
                    database=config.DB_NAME,
                    user=config.DB_USER,
                    password=config.DB_PASSWORD,
                    ssl=config.SSL_MODE if config.SSL_MODE != "disable" else None,
                    min_size=config.POOL_MIN_SIZE,
                    max_size=config.POOL_MAX_SIZE,
                )
                node.reachable = True
                logger.info("Pool created for %s (%s:%s)", node.name, node.host, node.port)
            except Exception as exc:
                node.reachable = False
                logger.warning("Could not create pool for %s: %s", node.name, exc)

        # Do one synchronous round of role detection before serving traffic,
        # then keep refreshing in the background.
        await self._refresh_roles()
        self._health_task = asyncio.create_task(self._health_check_loop())

    async def stop(self):
        self._stopping = True
        if self._health_task:
            self._health_task.cancel()
        for node in self.nodes:
            if node.pool:
                await node.pool.close()

    async def _health_check_loop(self):
        while not self._stopping:
            await asyncio.sleep(config.HEALTH_CHECK_INTERVAL_SECONDS)
            await self._refresh_roles()

    async def _refresh_roles(self):
        for node in self.nodes:
            if node.pool is None:
                # Was unreachable at startup; try to reconnect.
                try:
                    node.pool = await asyncpg.create_pool(
                        host=node.host,
                        port=node.port,
                        database=config.DB_NAME,
                        user=config.DB_USER,
                        password=config.DB_PASSWORD,
                        ssl=config.SSL_MODE if config.SSL_MODE != "disable" else None,
                        min_size=config.POOL_MIN_SIZE,
                        max_size=config.POOL_MAX_SIZE,
                    )
                except Exception:
                    node.reachable = False
                    continue
            try:
                async with node.pool.acquire(timeout=3) as conn:
                    in_recovery = await conn.fetchval("SELECT pg_is_in_recovery()")
                    node.is_primary = not in_recovery
                    node.reachable = True
            except Exception as exc:
                logger.warning("Health check failed for %s: %s", node.name, exc)
                node.reachable = False
                node.is_primary = False

        primary = [n.name for n in self.nodes if n.reachable and n.is_primary]
        standbys = [n.name for n in self.nodes if n.reachable and not n.is_primary]
        logger.info("Cluster roles -> primary: %s | standby: %s", primary or "NONE", standbys)

    def get_write_node(self) -> NodeState:
        """Return the NodeState (not just the pool) currently acting as primary.

        Exposing the node itself — not just its pool — lets callers report
        back which physical host actually served a given request, so you can
        prove the routing decision instead of taking it on faith.
        """
        candidates = [n for n in self.nodes if n.reachable and n.is_primary]
        if not candidates:
            raise NoPrimaryAvailable("No reachable primary node right now.")
        elif len(candidates) > 1:
            raise AmbiguousPrimaryError("Should not appear more than one primary.")
        node = candidates[0]
        logger.info("WRITE routed to %s (%s)", node.name, node.host)
        return node

    def get_read_node(self) -> NodeState:
        """Prefer a standby; fall back to the primary if no standby is up."""
        standbys = [n for n in self.nodes if n.reachable and not n.is_primary]
        if standbys:
            node = random.choice(standbys)
            logger.info("READ routed to %s (%s, standby)", node.name, node.host)
            return node
        # Degrade gracefully rather than fail reads outright.
        candidates = [n for n in self.nodes if n.reachable and n.is_primary]
        if not candidates:
            raise NoReplicaAvailable("No reachable node (primary or standby) right now.")
        node = candidates[0]
        logger.warning("No standby reachable — READ routed to %s (%s, primary fallback)", node.name, node.host)
        return node

    def get_write_pool(self) -> asyncpg.Pool:
        return self.get_write_node().pool

    def get_read_pool(self) -> asyncpg.Pool:
        return self.get_read_node().pool

    def status(self) -> list[dict]:
        return [
            {
                "name": n.name,
                "host": n.host,
                "port": n.port,
                "role": "primary" if n.is_primary else "standby",
                "reachable": n.reachable,
            }
            for n in self.nodes
        ]
