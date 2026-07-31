"""
Unit tests for ClusterRouter's failover logic.

Key idea: you don't need a real database connection to test the ROUTING
DECISION itself. ClusterRouter.nodes is just a list of NodeState objects
with .reachable / .is_primary flags — manipulate those directly in each
test, skip pool creation entirely (leave node.pool as None, since these
tests never call .acquire()).
"""

import pytest
from db_router import ClusterRouter, NodeState, NoPrimaryAvailable, NoReplicaAvailable, AmbiguousPrimaryError


def make_router(*, node_a_role, node_b_role):
    """
    Helper: build a ClusterRouter with two fake nodes, without calling
    router.start() (so no real network/DB connection happens).

    node_a_role / node_b_role should each be one of:
        "primary", "standby", "down"
    """
    # construct a ClusterRouter() instance (empty nodes list is fine
    # from __init__, since we're not passing real config).
    router = ClusterRouter()
    # Manually append two NodeState(...) objects to router.nodes,
    # setting .reachable and .is_primary according to node_a_role/node_b_role.
    # ("down" means reachable=False)
    router.nodes = [
        NodeState(name="node_a", host="1.1.1.1", port=5444, reachable=node_a_role!="down", is_primary=node_a_role=="primary"),
        NodeState(name="node_b", host="2.2.2.2", port=5444, reachable=node_b_role!="down", is_primary=node_b_role=="primary"),
    ]
    return router


def test_write_goes_to_primary():
    """When one node is primary and one is standby, get_write_node()
    should return the primary."""
    router = make_router(node_a_role="primary", node_b_role="standby")

    result = router.get_write_node()

    assert result.name == "node_a"


def test_write_raises_when_no_primary():
    """When neither node is primary (e.g. both standby, or primary is
    down), get_write_node() should raise NoPrimaryAvailable."""
    router = make_router(node_a_role="standby", node_b_role="standby")

    with pytest.raises(NoPrimaryAvailable):
        router.get_write_node()


def test_read_prefers_standby():
    """When a standby is reachable, get_read_node() should return it,
    NOT the primary — even though the primary is also reachable."""
    router = make_router(node_a_role="primary", node_b_role="standby")

    result = router.get_read_node()

    assert result.name == "node_b"


def test_read_falls_back_to_primary_when_no_standby():
    """When the only standby is down but primary is up, get_read_node()
    should degrade gracefully and return the primary instead of failing."""
    router = make_router(node_a_role="primary", node_b_role="down")

    result = router.get_read_node()

    assert result.name == "node_a"
    

def test_read_raises_when_nothing_reachable():
    """When every node is down, get_read_node() should raise
    NoReplicaAvailable."""
    router = make_router(node_a_role="down", node_b_role="down")

    with pytest.raises(NoReplicaAvailable):
        router.get_read_node()

def test_write_raises_on_split_brain():
    """If two nodes both claim to be primary at once (split-brain), refuse
    to guess - raise instead of silently picking one and risking a write
    to the wrong node."""
    router = make_router(node_a_role="primary", node_b_role="primary")
    with pytest.raises(AmbiguousPrimaryError):
        router.get_write_node()
