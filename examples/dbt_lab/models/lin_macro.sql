select id, {{ cents_to_units('amount') }} as amount_units from {{ ref('events') }}
