select u.id as user_id, e.id as event_id
from {{ ref('users') }} u join {{ ref('events') }} e on e.user_id = u.id or e.kind = 'buy'
