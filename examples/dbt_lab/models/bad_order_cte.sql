with ordered as (select id, amount from {{ ref('events') }} order by amount desc)
select id, amount from ordered where amount > 1
