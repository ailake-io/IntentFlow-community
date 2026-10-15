MODEL (
  name shop.stg_customers,
  kind VIEW,
  owner 'data-platform',
  description 'Clientes limpos',
  column_descriptions (email = 'E-mail do cliente (PII)'),
  grain customer_id,
  audits (not_null(columns := (customer_id)), unique_values(columns := (customer_id)))
);
SELECT id AS customer_id, lower(name) AS name, lower(email) AS email, country FROM shop.raw_customers;
