"""Persistent, non-preemptive queue fairness within the caller's claim transaction."""

from __future__ import annotations

import sqlite3


def initialize_scheduling(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS queue_scheduling_clocks (
        kind TEXT PRIMARY KEY, turn INTEGER NOT NULL)""")
    connection.execute("""CREATE TABLE IF NOT EXISTS queue_scheduling_turns (
        kind TEXT NOT NULL, workspace_id TEXT NOT NULL, owner_id TEXT NOT NULL,
        last_turn INTEGER NOT NULL, PRIMARY KEY(kind,workspace_id,owner_id))""")


def claim_fair_candidate(
    connection: sqlite3.Connection,
    kind: str,
    candidates_sql: str,
    parameters: tuple,
) -> sqlite3.Row | None:
    """Select and charge one turn atomically with the caller's lease acquisition.

    candidates_sql is internal SQL exposing workspace_id, owner_id, created_at,
    and scheduling_id. Never pass SQL supplied by a user. Call only inside a
    BEGIN IMMEDIATE transaction. An empty owner_id cursor represents a workspace;
    real candidate owners must be nonempty. Queue kinds have independent turns.
    """
    if kind not in {"query", "ingestion", "evaluation"}:
        raise ValueError("Unknown queue kind")
    row = connection.execute(
        f"""WITH candidates AS ({candidates_sql})
        SELECT c.* FROM candidates c
        LEFT JOIN queue_scheduling_turns w
          ON w.kind=? AND w.workspace_id=c.workspace_id AND w.owner_id=''
        LEFT JOIN queue_scheduling_turns u
          ON u.kind=? AND u.workspace_id=c.workspace_id AND u.owner_id=c.owner_id
        WHERE c.workspace_id<>'' AND c.owner_id IS NOT NULL AND c.owner_id<>''
        ORDER BY COALESCE(w.last_turn,0),COALESCE(u.last_turn,0),c.created_at,c.scheduling_id
        LIMIT 1""",
        (*parameters, kind, kind),
    ).fetchone()
    if row is None:
        return None
    connection.execute(
        """INSERT INTO queue_scheduling_clocks(kind,turn) VALUES(?,1)
        ON CONFLICT(kind) DO UPDATE SET turn=turn+1""",
        (kind,),
    )
    turn = connection.execute(
        "SELECT turn FROM queue_scheduling_clocks WHERE kind=?", (kind,)
    ).fetchone()[0]
    connection.executemany(
        """INSERT INTO queue_scheduling_turns(kind,workspace_id,owner_id,last_turn)
        VALUES(?,?,?,?) ON CONFLICT(kind,workspace_id,owner_id)
        DO UPDATE SET last_turn=excluded.last_turn""",
        [(kind, row["workspace_id"], owner, turn) for owner in ("", row["owner_id"])],
    )
    return row
