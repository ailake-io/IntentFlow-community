select w.user_id, w.running_amount, u.name
from {{ ref('lin_window') }} w left join {{ ref('users') }} u on u.id = w.user_id
