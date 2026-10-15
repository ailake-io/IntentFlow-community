select id, unnest(string_split(tags, ';')) as tag from {{ ref('events') }}
