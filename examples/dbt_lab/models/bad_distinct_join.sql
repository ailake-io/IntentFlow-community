select distinct u.country from {{ ref('users') }} u join {{ ref('events') }} e on e.user_id = u.id
