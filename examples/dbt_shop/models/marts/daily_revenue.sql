{{ config(materialized='incremental') }}
select order_date, sum(amount) as revenue
from {{ ref('orders') }}
{% if is_incremental() %}
where order_date > (select max(order_date) from {{ this }})
{% endif %}
group by 1
