select u.id as user_id, e.kind from {{ ref('users') }} u cross join {{ ref('events') }} e
