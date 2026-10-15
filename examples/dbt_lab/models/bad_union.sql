select id, kind from {{ ref('events') }} where kind = 'click'
union
select id, kind from {{ ref('events') }} where kind = 'buy'
