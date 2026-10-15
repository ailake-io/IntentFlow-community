select t.user_id, t.n from (select user_id, count(*) as n from {{ ref('events') }} group by user_id) t
