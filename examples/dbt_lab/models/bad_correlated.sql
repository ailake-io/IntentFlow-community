select u.id as user_id,
       (select max(e.ts) from {{ ref('events') }} e where e.user_id = u.id) as last_event
from {{ ref('users') }} u
