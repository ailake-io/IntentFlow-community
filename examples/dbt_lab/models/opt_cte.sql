with a as (select * from {{ ref('users') }}),
     b as (select * from a)
select id, name from b
