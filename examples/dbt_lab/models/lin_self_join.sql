select a.id as first_id, b.id as next_id
from {{ ref('events') }} a join {{ ref('events') }} b on a.user_id = b.user_id and a.ts < b.ts
