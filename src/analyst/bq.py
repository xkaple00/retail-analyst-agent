"""BigQuery access with dry-run cost check, timeout, row cap and cached schemas."""

from __future__ import annotations

import concurrent.futures

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from .config import DATASET, TABLES
from .errors import DataUnavailable, QueryTimeout

log = logging.getLogger(__name__)

TABLE_NOTES = {
    "orders": "One row per order. status: Complete, Shipped, Processing, Cancelled, Returned. created_at is the order time.",
    "order_items": "One row per item in an order. sale_price is the realised price (use for revenue). status per item.",
    "products": "Catalog. department (Women/Men), category, brand, name, retail_price, cost.",
    "users": "Customer demographics. Only id, age, gender, state, city, country, traffic_source, created_at are available.",
}


@dataclass
class QueryResult:
    rows: list[dict[str, Any]]
    row_count: int
    bytes_processed: int
    duration_ms: int
    truncated: bool = False


class QueryRunner(Protocol):
    def dry_run(self, sql: str) -> int: ...
    def query(self, sql: str, timeout_s: int, max_rows: int) -> QueryResult: ...
    def schema(self, table: str) -> list[dict[str, str]]: ...


def _jsonable(v: Any) -> Any:
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray)):
        return v.decode("utf-8", "replace")
    try:
        import decimal

        if isinstance(v, decimal.Decimal):
            return float(v)
    except Exception:  # pragma: no cover
        pass
    return v


# Table schemas change rarely; one metadata call per table per process, not per request
# (an Agent and its runner are built per request, and four metadata calls cost 2-15 s on a slow link).
_SCHEMA_CACHE: dict[str, list[dict[str, str]]] = {}


class BigQueryRunner:
    """Thin wrapper over google-cloud-bigquery. Construction is lazy so the CLI starts without creds."""

    def __init__(self, project: str | None, max_bytes: int) -> None:
        self._project = project
        self._max_bytes = max_bytes
        self._client = None
        self._schema_cache = _SCHEMA_CACHE

    @property
    def client(self):
        if self._client is None:
            try:
                from google.cloud import bigquery

                self._client = bigquery.Client(project=self._project)
            except Exception as e:  # DefaultCredentialsError, missing project, etc.
                raise DataUnavailable(
                    "BigQuery is not reachable. Run `gcloud auth application-default login` "
                    f"and set GOOGLE_CLOUD_PROJECT. Details: {e}"
                ) from e
        return self._client

    @staticmethod
    def _raise_if_outage(e: Exception) -> None:
        """Service/auth/timeout failures are an outage (DataUnavailable), not a SQL mistake for the model to fix."""
        from google.api_core import exceptions as gx

        if isinstance(e, (gx.ServiceUnavailable, gx.InternalServerError, gx.GatewayTimeout, gx.DeadlineExceeded,
                          gx.Forbidden, gx.Unauthorized, gx.TooManyRequests)):
            raise DataUnavailable(f"BigQuery unavailable: {type(e).__name__}: {str(e)[:200]}") from e

    def dry_run(self, sql: str) -> int:
        from google.cloud import bigquery

        try:
            job = self.client.query(sql, job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False))
            return int(job.total_bytes_processed or 0)
        except Exception as e:
            self._raise_if_outage(e)
            raise

    def query(self, sql: str, timeout_s: int, max_rows: int) -> QueryResult:
        from google.cloud import bigquery

        cfg = bigquery.QueryJobConfig(maximum_bytes_billed=self._max_bytes)
        t0 = time.perf_counter()
        job = None
        try:
            job = self.client.query(sql, job_config=cfg)
            it = job.result(timeout=timeout_s, max_results=max_rows + 1)
        except (concurrent.futures.TimeoutError, TimeoutError) as e:
            if job is not None:
                try:
                    job.cancel()  # do not leave a slow query running (and billing) in the background
                except Exception:  # pragma: no cover - best effort
                    pass
            raise QueryTimeout(f"query cancelled after {timeout_s}s") from e
        except Exception as e:
            self._raise_if_outage(e)
            raise
        rows = [{k: _jsonable(v) for k, v in dict(r).items()} for r in it]
        truncated = len(rows) > max_rows
        rows = rows[:max_rows]
        return QueryResult(
            rows=rows,
            row_count=len(rows),
            bytes_processed=int(job.total_bytes_processed or 0),
            duration_ms=int((time.perf_counter() - t0) * 1000),
            truncated=truncated,
        )

    def schema(self, table: str) -> list[dict[str, str]]:
        if table not in self._schema_cache:
            t = self.client.get_table(f"{DATASET}.{table}")
            self._schema_cache[table] = [
                {"name": f.name, "type": f.field_type, "description": f.description or ""} for f in t.schema
            ]
        return self._schema_cache[table]


@dataclass
class FakeBigQuery:
    """Test double: scripted results / errors, records every SQL it sees."""

    responses: list[Any] = field(default_factory=list)  # QueryResult | Exception
    seen: list[str] = field(default_factory=list)
    dry_run_bytes: int = 1000
    schemas: dict[str, list[dict[str, str]]] = field(default_factory=dict)

    def dry_run(self, sql: str) -> int:
        return self.dry_run_bytes

    def query(self, sql: str, timeout_s: int, max_rows: int) -> QueryResult:
        self.seen.append(sql)
        if not self.responses:
            return QueryResult(rows=[], row_count=0, bytes_processed=0, duration_ms=1)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def schema(self, table: str) -> list[dict[str, str]]:
        return self.schemas.get(table, [{"name": "id", "type": "INTEGER", "description": ""}])


def describe_tables(runner: QueryRunner, table: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"dataset": DATASET, "tables": {}}
    for t in TABLES:
        if table and t != table:
            continue
        cols = runner.schema(t)
        if t == "users":
            from .pii import SAFE_USER_COLUMNS

            cols = [c for c in cols if c["name"] in SAFE_USER_COLUMNS]
        out["tables"][t] = {"note": TABLE_NOTES[t], "columns": cols}
    out["joins"] = (
        "order_items.order_id = orders.order_id; order_items.product_id = products.id; "
        "order_items.user_id = users.id; orders.user_id = users.id"
    )
    return out
