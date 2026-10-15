# Auditoria de pipeline com IntentFlow: proposta e modelo de relatório

[English](../PILOT_OFFER.md)

Material comercial do piloto pago. O roteiro técnico, passo a passo, está em [PILOT](PILOT.md).

## Proposta (1 página)

**Problema:** mudanças em modelos dbt, jobs Spark/Python e DAGs quebram consumidores e dashboards em produção, e a equipe só
descobre depois do incidente.

**O que entregamos em 2 a 4 semanas:**

1. **Mapa de impacto**: linhagem de coluna do projeto inteiro, incluindo jobs fora do dbt (Spark, Python, notebooks via OpenLineage).
2. **Relatório do que pode quebrar**: cada risco com o que quebra e como corrigir, priorizado por severidade.
3. **Achados preventivos**: SQL caro ou frágil, chaves duplicadas, PII sem máscara, incrementais sem chave, DAGs sem retry/timeout.
4. **Gate de PR ligado no CI** de vocês, com o limiar de falso positivo combinado.
5. **Apresentação de 1 hora** com os 10 riscos principais e as correções recomendadas, incluindo as que já podemos aplicar.

**O que precisamos:**

- Acesso de leitura ao repositório dbt (ou aos artefatos `manifest.json` e `catalog.json`).
- Opcionais: warehouse de desenvolvimento só para leitura; URL do Airflow ou Kestra; endpoint OpenLineage.
- 2 horas de uma pessoa do time: abertura e revisão dos achados.

**Segurança:**

- Tudo roda no ambiente de vocês e é somente leitura.
- Nenhum dado sai do ambiente. A IA fica desligada, salvo pedido.
- Ao final, removemos o armazém local.

**Critério de sucesso combinado na abertura:** ao menos um risco real que vocês não conheciam, e falso positivo abaixo de 50%.

**Investimento:** [valor fixo] · **Depois do piloto:** IntentFlow open source no CI sem custo, ou edição Enterprise
(modo equipe, SSO, correção governada, suporte), ver [EDITIONS](EDITIONS.md).

## Modelo de relatório

Cada seção sai de um comando. Gere tudo numa pasta e cole no documento do cliente.

```bash
mkdir -p relatorio
intentflow risks  --target target --base ../main/target            > relatorio/1-riscos.md
intentflow check  --target target --base ../main/target --format markdown > relatorio/2-achados.md
intentflow lineage --target target --column <modelo.coluna_critica> --impact > relatorio/3-impacto.txt   # 3 a 5 colunas
intentflow lineage --target target --coverage                      > relatorio/4-cobertura.txt
intentflow stats                                                  > relatorio/5-precisao.txt              # depois de marcar falsos positivos
```

| Seção | Conteúdo | Fonte |
|---|---|---|
| Resumo executivo | 3 riscos principais, quantos achados, % útil, recomendação de gate | você, a partir das seções abaixo |
| 1. O que pode quebrar | riscos por área (visibilidade, fragilidade, jobs externos, processo) | `risks` |
| 2. Achados | por severidade, com dono e correção | `check` |
| 3. Impacto das colunas críticas | consumidores diretos e transitivos, jobs externos | `lineage --impact` |
| 4. Pontos cegos | jobs com propagação conservadora e datasets sem casamento | `lineage --coverage` e `--unmatched` |
| 5. Qualidade do próprio IntentFlow | falso positivo por check, falsos negativos de linhagem | `stats` e tabela "O que devolver" do [PILOT](PILOT.md) |
| 6. Próximos passos | gate no CI, correções a aplicar, o que declarar em `intentflow.lineage.json` | você |
