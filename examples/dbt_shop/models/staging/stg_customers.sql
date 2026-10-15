select
    id as customer_id,
    first_name,
    last_name,
    email,
    signup_date
from {{ ref('raw_customers') }}
