select id, 'v1' as version, 1 as one, current_date as loaded_at from {{ ref('users') }}
