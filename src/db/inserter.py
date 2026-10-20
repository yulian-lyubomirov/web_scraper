"""Insert pandas DataFrames into PostgreSQL tables."""

from __future__ import annotations

from typing import Any

import pandas as pd
import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb


class Inserter:
    """Small PostgreSQL dataframe inserter using psycopg 3."""

    def __init__(self, conn: psycopg.Connection | None = None) -> None:
        self.conn = conn

    def connect(self, conninfo: str | None = None, *, service: str | None = None,
                **kwargs: Any) -> Inserter:
        if conninfo is None and service is None:
            raise ValueError("Provide either conninfo or service")
        if service is not None:
            conninfo = f"service={service}"
        self.conn = psycopg.connect(conninfo, **kwargs)
        return self

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def __enter__(self) -> Inserter:
        if self.conn is None:
            raise RuntimeError("Call connect() before using Inserter as a context manager")
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.close()

    def insert(
        self,
        table: str,
        df: pd.DataFrame,
        *,
        schema: str = "public",
        columns: list[str] | None = None,
    ) -> int:
        """Insert dataframe rows and return the number of rows inserted.

        If ``columns`` is supplied, it selects and orders the DataFrame columns
        to insert. Identifiers are quoted by psycopg; values are parameterized.
        """
        if self.conn is None:
            raise RuntimeError("Connect to a database before inserting")
        if df.empty:
            return 0

        selected = columns or list(df.columns)
        if not selected:
            raise ValueError("At least one DataFrame column is required")
        missing = [column for column in selected if column not in df.columns]
        if missing:
            raise ValueError(f"DataFrame is missing columns: {', '.join(missing)}")

        query = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
            sql.Identifier(schema, table),
            sql.SQL(", ").join(map(sql.Identifier, selected)),
            sql.SQL(", ").join(sql.Placeholder() for _ in selected),
        )
        rows = [
            tuple(_adapt_value(value) for value in record)
            for record in df.loc[:, selected].itertuples(index=False, name=None)
        ]
        with self.conn.cursor() as cursor:
            cursor.executemany(query, rows)
        return len(rows)


def _adapt_value(value: Any) -> Any:
    """Convert pandas missing values to SQL NULL and JSON containers for psycopg."""
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, (dict, list)):
        return Jsonb(value)
    if isinstance(value, float) and pd.isna(value):
        return None
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, (dict, list)):
        return Jsonb(value)
    return value
