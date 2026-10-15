MODEL (
  name shop.fct_orders,
  kind FULL,
  owner 'data-platform',
  grain order_id
);
SELECT o.id AS order_id, o.customer_id, o.amount, o.status, o.ordered_at, c.country
FROM shop.raw_orders AS o
LEFT JOIN shop.stg_customers AS c ON o.customer_id = c.customer_id
WHERE o.status <> 'cancelled';
