import pytest

from analyst.errors import UnsafeSQL
from analyst.scope import make_safe_sql

ALICE_FILTER = "brand IN ('Calvin Klein', 'Levi\\'s')"


def test_products_are_scoped_to_brands(alice):
    sql = make_safe_sql("SELECT name, retail_price FROM products ORDER BY retail_price DESC", alice)
    assert ALICE_FILTER in sql
    assert "bigquery-public-data.thelook_ecommerce.products" in sql
    assert "LIMIT 200" in sql


def test_ceo_sees_everything(ceo):
    sql = make_safe_sql("SELECT COUNT(*) FROM products", ceo)
    assert "brand IN" not in sql
    assert "WHERE TRUE" in " ".join(sql.split())


def test_alias_is_kept(alice):
    sql = make_safe_sql("SELECT p.category, SUM(oi.sale_price) FROM order_items oi JOIN products p ON p.id = oi.product_id GROUP BY 1", alice)
    assert " AS oi" in sql and " AS p" in sql
    assert "product_id IN (" in sql


def test_orders_and_users_are_scoped_through_order_items(bob):
    sql = make_safe_sql("SELECT u.state, COUNT(*) FROM orders o JOIN users u ON u.id = o.user_id GROUP BY 1", bob)
    assert "brand IN ('Diesel')" in sql
    flat = " ".join(sql.split())
    assert "SELECT id, age, gender, state, city, country, traffic_source, created_at FROM" in flat
    assert "first_name" not in sql and "email" not in sql


def test_pii_column_is_rejected(alice):
    with pytest.raises(UnsafeSQL, match="personal data"):
        make_safe_sql("SELECT email FROM users", alice)
    with pytest.raises(UnsafeSQL, match="personal data"):
        make_safe_sql("SELECT u.first_name FROM users u", alice)


def test_only_select_allowed(alice):
    with pytest.raises(UnsafeSQL):
        make_safe_sql("DELETE FROM orders WHERE 1=1", alice)
    with pytest.raises(UnsafeSQL, match="one statement"):
        make_safe_sql("SELECT 1; SELECT 2", alice)


def test_unknown_table_rejected(alice):
    with pytest.raises(UnsafeSQL, match="unknown table"):
        make_safe_sql("SELECT * FROM inventory_items", alice)


def test_fully_qualified_names_are_mapped(alice):
    sql = make_safe_sql("SELECT COUNT(*) FROM `bigquery-public-data.thelook_ecommerce.orders`", alice)
    assert ALICE_FILTER in sql


def test_model_cannot_widen_scope_with_its_own_brand_filter(bob):
    # The model asks for Calvin Klein; the scoped subquery still only contains Diesel rows.
    sql = make_safe_sql("SELECT COUNT(*) FROM products WHERE brand = 'Calvin Klein'", bob)
    assert "brand IN ('Diesel')" in sql and "brand = 'Calvin Klein'" in sql


def test_cte_names_are_not_rewritten(alice):
    sql = make_safe_sql("WITH m AS (SELECT user_id FROM orders) SELECT COUNT(*) FROM m", alice)
    assert sql.count(ALICE_FILTER) == 1


def test_existing_limit_is_kept(alice):
    sql = make_safe_sql("SELECT * FROM products LIMIT 5", alice)
    assert "LIMIT 5" in sql and "LIMIT 200" not in sql


def test_bypass_attempts_are_scoped_or_rejected(alice):
    import pytest

    from analyst.errors import UnsafeSQL

    # every reference, however nested, is rewritten to the scoped subquery
    for sql in (
        "SELECT * FROM products p WHERE p.id IN (SELECT product_id FROM order_items)",
        "SELECT (SELECT COUNT(*) FROM products) AS n",
        "SELECT brand FROM products UNION ALL SELECT brand FROM `bigquery-public-data.thelook_ecommerce.products`",
        "SELECT * FROM UNNEST((SELECT ARRAY_AGG(brand) FROM products)) AS b",
        "SELECT brand FROM products EXCEPT DISTINCT SELECT brand FROM products WHERE brand = 'x'",
        "WITH t AS (SELECT * FROM products) SELECT * FROM t",
    ):
        out = " ".join(make_safe_sql(sql, alice).split())
        assert out.count("brand IN (") == out.lower().count("thelook_ecommerce.products`"), sql
    for sql, msg in (
        ("WITH products AS (SELECT 1 AS brand) SELECT * FROM products", "shadows a base table"),
        ("SELECT * FROM products FOR SYSTEM_TIME AS OF TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 1 DAY)", "SYSTEM_TIME"),
        ("SELECT * FROM products TABLESAMPLE SYSTEM (10 PERCENT)", "TABLESAMPLE"),
        ("SELECT * FROM `other-project.thelook_ecommerce.products`", "unknown table"),
        ("SELECT * FROM thelook_ecommerce.INFORMATION_SCHEMA.COLUMNS", "INFORMATION_SCHEMA"),
        ("SELECT TO_JSON_STRING(u) FROM users u WHERE u.email LIKE '%a%'", "personal data"),
        ("SELECT * FROM orders; SELECT 1", "exactly one statement"),
    ):
        with pytest.raises(UnsafeSQL, match=msg):
            make_safe_sql(sql, alice)


def test_orders_item_count_is_recounted_over_scoped_items(alice, ceo):
    sql = " ".join(make_safe_sql("SELECT AVG(num_of_item) FROM orders", alice).split())
    assert "EXCEPT (num_of_item)" in sql and "COUNT(*) AS num_of_item" in sql and "brand IN (" in sql
    ceo_sql = " ".join(make_safe_sql("SELECT AVG(num_of_item) FROM orders", ceo).split())
    assert "EXCEPT" not in ceo_sql  # CEO sees the raw table


def test_qualified_table_named_like_a_cte_is_not_exempt(alice):
    # `other.x` is a real table in another dataset even when a CTE is also called x
    sql = "WITH x AS (SELECT 1 AS n) SELECT * FROM `secret_dataset.x`"
    with pytest.raises(UnsafeSQL, match="unknown table"):
        make_safe_sql(sql, alice)
    sql = "WITH x AS (SELECT 1 AS n) SELECT * FROM `other-project.thelook_ecommerce.x`"
    with pytest.raises(UnsafeSQL, match="unknown table"):
        make_safe_sql(sql, alice)
