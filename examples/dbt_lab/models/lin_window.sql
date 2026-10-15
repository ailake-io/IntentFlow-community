select user_id, amount,
       sum(amount) over (partition by user_id order by ts) as running_amount,
       row_number() over (partition by user_id order by ts) as rn
from {{ ref('events') }}
