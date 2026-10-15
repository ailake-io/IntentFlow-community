MODEL (
  name shop.raw_customers,
  kind SEED (path '../seeds/raw_customers.csv'),
  columns (id INT, name TEXT, email TEXT, country TEXT),
  grain id
);
