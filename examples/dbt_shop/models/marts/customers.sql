with orders as (select * from {{ ref('stg_orders') }}),
payments as (select * from {{ ref('stg_payments') }}),
order_totals as (
    select orders.customer_id, count(*) as number_of_orders, sum(payments.amount) as lifetime_value
    from orders left join payments on payments.order_id = orders.order_id
    group by orders.customer_id
)
select
    c.customer_id,
    c.first_name,
    c.last_name,
    c.email,
    coalesce(t.number_of_orders, 0) as number_of_orders,
    coalesce(t.lifetime_value, 0) as lifetime_value
from {{ ref('stg_customers') }} c
left join order_totals t on t.customer_id = c.customer_id
