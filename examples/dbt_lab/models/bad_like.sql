select id, name from {{ ref('users') }} where name like '%son'
