select
    o.order_id,
    o.customer_id,
    o.order_date,
    o.status,
    sum(p.amount) as amount
from {{ ref('stg_orders') }} o
left join {{ ref('stg_payments') }} p on p.order_id = o.order_id
group by 1, 2, 3, 4
