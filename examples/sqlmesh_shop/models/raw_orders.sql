MODEL (
  name shop.raw_orders,
  kind SEED (path '../seeds/raw_orders.csv'),
  columns (id INT, customer_id INT, amount DECIMAL(10,2), status TEXT, ordered_at DATE),
  grain id
);
