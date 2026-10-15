select u.id as user_id, e.amount from {{ ref('users') }} u, {{ ref('events') }} e
