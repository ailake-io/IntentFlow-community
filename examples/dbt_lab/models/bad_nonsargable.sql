select id, user_id from {{ ref('events') }}
where lower(kind) = 'click' and cast(ts as date) = date '2026-03-01'
