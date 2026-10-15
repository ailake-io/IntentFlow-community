select id, name from {{ ref('users') }}
where id in ({% for i in range(1, 131) %}{{ i }}{% if not loop.last %}, {% endif %}{% endfor %})
