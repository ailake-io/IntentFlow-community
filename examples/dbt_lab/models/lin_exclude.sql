select * exclude (tags) from {{ ref('events') }}
