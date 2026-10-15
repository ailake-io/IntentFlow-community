select id as entity_id, name as label from {{ ref('users') }}
union all
select id as entity_id, label from {{ ref('dup_keys') }}
