select user_id,
       sum(case when kind = 'click' then amount end) as click_amount,
       sum(case when kind = 'buy' then amount end) as buy_amount
from {{ ref('events') }} group by user_id
