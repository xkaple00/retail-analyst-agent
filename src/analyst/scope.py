"""SQL safety: single SELECT only, per-user product scope, PII column deny-list, row limit.

The model writes SQL against the short names orders / order_items / products / users.
Each of them is replaced by a filtered subquery, so the scope is enforced in the query
itself, not by asking the model nicely.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp

from .config import DATASET, TABLES, User
from .errors import UnsafeSQL
from .pii import PII_COLUMNS, SAFE_USER_COLUMNS


# SELECT plus the set operations (UNION / EXCEPT / INTERSECT); everything else is rejected.
_SELECT_SHAPES = (exp.Select, exp.Union, exp.Except, exp.Intersect)


def _q(value: str) -> str:
    return "'" + value.replace("'", "\\'") + "'"


def _product_filter(user: User) -> str:
    """Brand is the access dimension. An empty brand list means everything (CEO)."""
    if not user.brands:
        return "TRUE"
    return "brand IN (" + ", ".join(_q(b) for b in user.brands) + ")"


def scoped_sources(user: User) -> dict[str, str]:
    """SQL text of the filtered subquery for each table."""
    pf = _product_filter(user)
    products = f"SELECT * FROM `{DATASET}.products` WHERE {pf}"
    scoped_product_ids = f"SELECT id FROM `{DATASET}.products` WHERE {pf}"
    order_items = f"SELECT * FROM `{DATASET}.order_items` WHERE product_id IN ({scoped_product_ids})"
    if user.brands:
        # num_of_item is recounted over the user's items only, so a mixed-brand order does not reveal
        # how many items of other brands it contains.
        orders = (
            f"SELECT o.* EXCEPT (num_of_item), s.num_of_item FROM `{DATASET}.orders` AS o JOIN "
            f"(SELECT order_id, COUNT(*) AS num_of_item FROM `{DATASET}.order_items` "
            f"WHERE product_id IN ({scoped_product_ids}) GROUP BY order_id) AS s ON s.order_id = o.order_id"
        )
    else:
        orders = f"SELECT * FROM `{DATASET}.orders`"
    # users: customers who bought at least one scoped product (a brand manager sees only their own buyers).
    users = (
        f"SELECT {', '.join(SAFE_USER_COLUMNS)} FROM `{DATASET}.users` WHERE id IN "
        f"(SELECT user_id FROM `{DATASET}.order_items` WHERE product_id IN ({scoped_product_ids}))"
    )
    return {"products": products, "order_items": order_items, "orders": orders, "users": users}


def _table_short_name(table: exp.Table) -> str | None:
    """Map `products`, `thelook_ecommerce.products`, `bigquery-public-data.thelook_ecommerce.products`."""
    name = table.name.lower()
    if name not in TABLES:
        return None
    db = (table.db or "").lower()
    catalog = (table.catalog or "").lower()
    if db and db != "thelook_ecommerce":
        return None
    if catalog and catalog != "bigquery-public-data":
        return None
    return name


def make_safe_sql(sql: str, user: User, max_rows: int = 200) -> str:
    """Validate and rewrite the model's SQL. Raises UnsafeSQL with a model-readable message."""
    if not sql or not sql.strip():
        raise UnsafeSQL("empty SQL")
    try:
        statements = sqlglot.parse(sql, read="bigquery")
    except sqlglot.errors.ParseError as e:  # pragma: no cover - message passthrough
        raise UnsafeSQL(f"SQL parse error: {e}") from e
    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        raise UnsafeSQL("exactly one statement is allowed")
    tree = statements[0]
    if not isinstance(tree, _SELECT_SHAPES):
        raise UnsafeSQL(f"only SELECT queries are allowed, got {type(tree).__name__}")

    # PII columns: reject any explicit reference, qualified or not.
    pii_cols = set().union(*PII_COLUMNS.values())
    for col in tree.find_all(exp.Column):
        if col.name.lower() in pii_cols:
            raise UnsafeSQL(
                f"column '{col.name}' is personal data and cannot be queried. "
                f"users exposes only: {', '.join(SAFE_USER_COLUMNS)}"
            )

    # CTE names: a CTE may not reuse a base table name (the reference would silently become the base table).
    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    shadow = cte_names & set(TABLES)
    if shadow:
        raise UnsafeSQL(f"CTE name {', '.join(sorted(shadow))} shadows a base table; rename the CTE")
    sources = scoped_sources(user)
    for table in list(tree.find_all(exp.Table)):
        if table.args.get("version") is not None:
            raise UnsafeSQL("FOR SYSTEM_TIME AS OF is not allowed")
        if table.args.get("sample") is not None:
            raise UnsafeSQL("TABLESAMPLE is not allowed")
        short = _table_short_name(table)
        if short is None:
            if table.name.lower() in cte_names and not table.db and not table.catalog:
                continue  # only an unqualified name can refer to a CTE; `x.y.cte` is a real table
            if table.name.lower().startswith("information_schema") or (table.db or "").upper() == "INFORMATION_SCHEMA":
                raise UnsafeSQL("INFORMATION_SCHEMA is not available, use get_schema instead")
            raise UnsafeSQL(
                f"unknown table '{table.sql(dialect='bigquery')}'. Available: {', '.join(TABLES)}"
            )
        alias = table.alias or short
        sub = sqlglot.parse_one(sources[short], read="bigquery")
        table.replace(exp.Subquery(this=sub, alias=exp.TableAlias(this=exp.to_identifier(alias))))

    # Row cap: only on the outermost query (SELECT or a set operation).
    if tree.args.get("limit") is None:
        tree = tree.limit(max_rows)
    return tree.sql(dialect="bigquery", pretty=True)
