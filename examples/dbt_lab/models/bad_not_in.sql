select id, name from {{ ref('users') }} where id not in (select user_id from {{ ref('events') }})
