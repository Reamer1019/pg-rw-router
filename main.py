"""
Demo API showing the router in action.

- GET  /health        -> current view of the cluster (who's primary/standby)
- GET  /notes         -> READ path, served from a standby
- POST /notes         -> WRITE path, served from the primary

Run with:
    uvicorn main:app --reload --port 8000

Try it while doing a switchover on the lab cluster (efm promote -switchover)
and watch /health flip which node is "primary" within one health-check
interval, with zero code changes needed on the client side.
"""
import os

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Header, Depends
from pydantic import BaseModel

from db_router import ClusterRouter, NoPrimaryAvailable, NoReplicaAvailable

router = ClusterRouter()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await router.start()
    await _ensure_schema()
    yield
    await router.stop()


app = FastAPI(title="pg-rw-router demo", lifespan=lifespan)

API_KEY = os.environ.get("API_KEY", "DEFAULT_API_KEY")

def verify_api_key(x_api_key: str = Header(...)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return None

async def _ensure_schema():
    """Create the demo table on the primary if it doesn't exist yet."""
    try:
        pool = router.get_write_pool()
    except NoPrimaryAvailable:
        return
    async with pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS notes (
                id SERIAL PRIMARY KEY,
                body TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT now()
            )
            """
        )


class NoteIn(BaseModel):
    body: str


@app.get("/health")
async def health():
    return {"nodes": router.status()}


@app.get("/notes")
async def list_notes():
    """Read path — always routed to a standby if one is available."""
    try:
        node = router.get_read_node()
    except NoReplicaAvailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    async with node.pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, body, created_at FROM notes ORDER BY id DESC LIMIT 50")
    return {
        "served_by": {"name": node.name, "host": node.host, "role": "primary" if node.is_primary else "standby"},
        "notes": [dict(r) for r in rows],
    }


@app.post("/notes", status_code=201)
async def create_note(note: NoteIn, _: None = Depends(verify_api_key)):
    """Write path — always routed to the current primary."""
    try:
        node = router.get_write_node()
    except NoPrimaryAvailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    async with node.pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO notes (body) VALUES ($1) RETURNING id, body, created_at",
            note.body,
        )
    return {
        "served_by": {"name": node.name, "host": node.host, "role": "primary"},
        "note": dict(row),
    }
