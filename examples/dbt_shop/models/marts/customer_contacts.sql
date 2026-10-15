-- exporta contatos para marketing: carrega e-mail (PII) sem mascaramento
select customer_id, email, lifetime_value
from {{ ref('customers') }}
