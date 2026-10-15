MODEL (
  name shop.revenue_by_country,
  kind FULL,
  owner finance,
  grain country
);
SELECT country, sum(amount) AS revenue, count(*) AS orders
FROM shop.fct_orders
WHERE status = 'paid'
GROUP BY country;
