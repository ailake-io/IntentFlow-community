# Acompanhar regras de negócio com o IntentFlow

[English](../BUSINESS_RULES.md)

Recurso Unreleased do checkout. Declare regras como **testes nativos do dbt** com a tag `business_rule`; o IntentFlow lê as definições e os resultados. O responsável pelo negócio precisa definir receita correta, moedas permitidas, condições de cliente/tempo e a ação em caso de falha. Não é necessário um YAML próprio do IntentFlow ou outro motor SQL.

## Adicionar uma regra

Crie `tests/paid_orders_reconcile.sql` no projeto dbt:

```sql
{{ config(tags=['business_rule'], severity='error', meta={'owner': 'finance'}) }}

with paid as (
  select order_id, sum(amount) as paid_amount
  from {{ ref('payments') }}
  where status = 'settled'
  group by order_id
)
select o.order_id
from {{ ref('orders') }} o
left join paid p on p.order_id = o.order_id
where o.status = 'paid'
  and abs(o.total - coalesce(p.paid_amount, 0)) > {{ var('payment_tolerance', 0.01) }}
```

O teste retorna violações, como no dbt normal. Adapte nomes, liquidação/cancelamento, tipos monetários, moedas e tolerância ao negócio. `failures` é a contagem produzida pela configuração do teste: uma reconciliação agregada pode retornar uma violação, não cada transação afetada. Valor negativo pode ser devolução legítima; a condição precisa considerar o tipo/estado do evento.

Execute dbt, preserve o run_results do **build** antes da documentação e ingira com `intentflow check --target target --root .`. Configure `dbt.target`/`dbt.root` no store para watch/UI:

```bash
intentflow rules --target target --root .
intentflow rules --target target --root . --format json
```

`rules` é um comando de consulta; seu código de saída não é um bloqueio de qualidade. Na CI, use preflight ou check com --fail-on.

A tela **Regras de negócio** mostra nome, descrição quando presente nos metadados dbt, responsável, último resultado, contagem de falhas, modelos lidos, impacto nos dependentes e execuções recentes. `--all` inclui testes sem a tag no catálogo CLI; somente os marcados têm histórico dedicado.

Testes genéricos podem usar os mesmos `config.tags` e `config.meta.owner` no YAML nativo do dbt. Testes unitários também são suportados quando suas definições/resultados estão presentes. O responsável explícito do teste prevalece; sem ele, herda-se o único responsável inequívoco dos modelos de entrada. Se há responsáveis diferentes, declare o da regra.

## O que é acompanhado

- DBTR001 informa falha/aviso/erro de testes, incluindo testes SQL com várias referências e testes unitários dbt. Mantém ID, responsável, dependências e impacto; as rotas existentes podem usar o responsável.
- DBTR003 informa modelos pulados após testes falharem. Testes unitários pulados não são contados como modelos.
- BIZ001 informa execução ausente, pulada ou desatualizada; padrão de 24 horas, configurável com `policy.max_rule_age_hours`. BIZ002 informa regra sem responsável explícito ou herdado inequivocamente.
- Resultado pode ser pass/fail/warn/error/skipped. Ausência e desatualização aparecem separadamente e nunca como aprovação. Zero violações após execução válida é aprovação.
- Checks normais persistem as últimas **20 invocações observadas por ID de teste marcado**, no store do projeto, sem repetir a mesma invocação dbt. É histórico limitado de execuções, não série ilimitada de indicadores de negócio. Preserve/faça backup do store; mudar o ID dbt cria outro histórico.
- Preflight bloqueia falhas/pulos/lacunas de build completo independentemente da severidade do achado e pode bloquear todos os avisos. Falha na validação dos artefatos retorna diagnóstico antes da ingestão; check/watch normal acompanha essas execuções reprovadas no store compartilhado.

A UI consulta resultados; edição e execução das regras acontecem no repositório dbt e na CI/deploy normal. Declarar uma regra não a agenda. Agende dbt e watch/check e configure as notificações. A [revisão de mudanças](CHANGE_REVIEW.md) identifica definições removidas/alteradas, políticas e retirada do acompanhamento contra uma referência confiável. O preflight exige aceite exato; a semântica de SQL arbitrário alterado continua precisando de revisão humana.

## Casos do dia a dia e como representar

| Caso | Regra/evidência |
|---|---|
| Chave duplicada/replay | unique + not_null, unique_key no incremental, PRB001 e ensaio de retry/backfill |
| Cliente/pedido órfão | relationships ou teste anti-join |
| Campo obrigatório nulo | not_null, required no contrato e PRB003 com histórico confiável |
| Status/moeda inválidos | accepted_values ou teste contextual |
| Pagamento diferente da nota | reconciliação agrupada com tolerância explícita |
| Cálculo/unidade incorretos | cálculo esperado independente ou teste unitário; data diff mostra mudança, não qual versão está certa |
| Totais diário/cliente/contábil divergentes | reconciliação entre fatos/marts/razão, incluindo clientes desconhecidos |
| Lote/partição incompletos | contagens origem/destino, PRB002 e testes de completude |
| Evento futuro, fora de ordem ou atrasado | relógio de referência, fuso e janela de atraso explícitos; regras de tempo do evento e de ingestão separadas |
| Devolução, brinde ou ajuste legítimos | regras por estado/tipo e política acordada de preservação/quarentena |
| Schema alterado/dependente quebrado | catálogo da base + SCH e build/testes completos |
| Dado sensível propagado | metadados PII declarados + DBT004/OL003; descoberta e segurança adicionais quando necessárias |

A regressão automatizada com dbt real exercita sete regras com dados válidos e inválidos: reconciliação de pagamento, status, moeda, valor pago positivo, cliente presente, cliente existente e evento futuro. Cancelamento negativo permanece válido. O laboratório UCI exercita à parte reconciliação, duplicação, extração parcial, preço, schema e retry incremental. Isso cobre os cenários declarados, não prova todos os padrões de produção.

## Contratos e ferramentas externas

Contratos existentes em Data Contract Specification `models:` e ODCS v3 `schema:` podem declarar nomes/tipos, obrigatoriedade, unicidade e PII. Configure `contracts.dir`; CTR001 compara com o catálogo e a aplicação declarada das restrições. Não é um avaliador geral de toda expressão de qualidade ODCS/DCS. Os testes precisam executar e passar; `unique_key` é configuração, não prova executada de unicidade.

Resultados GX/Soda já entram por `dq.results`; Elementary tem a integração existente. Não viram automaticamente regras dbt marcadas ou entradas desse histórico. Para obrigar disponibilidade de contratos, use preflight `--require-check CTR001`. Veja [PREFLIGHT](PREFLIGHT.md) para bloqueios, proveniência na CI e limites restantes.

O modelo do contrato precisa ter correspondência inequívoca. Se vários pacotes definem orders, use sales.orders, model.sales.orders ou a relação física completa. Nome simples ambíguo gera CTR001 high; identidades semânticas pacote/modelo são mais portáveis entre ambientes que schemas físicos.
