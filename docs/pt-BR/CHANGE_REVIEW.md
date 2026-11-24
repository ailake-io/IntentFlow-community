# Revisar mudanças antes da produção

[English](../CHANGE_REVIEW.md)

Recurso Unreleased, disponível pelo checkout do código. `intentflow changes` compara manifests/catálogos dbt nativos da referência aprovada e da versão candidata. A tela **Mudanças antes da produção** apresenta a mesma comparação. O `preflight` exige revisão de mudanças em controles/interfaces existentes mesmo quando todos os testes dbt restantes passam.

## Mudanças cobertas

| Mudança | Tratamento padrão |
|---|---|
| Teste de dados/unitário existente removido ou desativado | Revisão obrigatória |
| SQL, argumentos genéricos, severidade, limites de falha, filtro, limite de linhas ou outra configuração de execução de teste alterados | Revisão obrigatória |
| Entradas, expectativas ou overrides embutidos em teste unitário alterados | Revisão obrigatória |
| Dependências declaradas de macros de testes alteradas, inclusive transitivas | Revisão obrigatória |
| Tag `business_rule` removida | Revisão obrigatória |
| Modelo/seed/snapshot/fonte existente removido ou desativado; alias/identificador físico alterado | Revisão obrigatória |
| Coluna removida ou tipo alterado, mesmo sem consumidores conhecidos | Revisão obrigatória |
| Contrato nativo obrigatório desativado; restrições declaradas removidas/substituídas | Revisão obrigatória |
| Materialização incremental, chave única ou estratégia alteradas | Revisão obrigatória |
| Limites, filtro ou relógio de referência de frescor existente alterados | Revisão obrigatória |
| Recurso, teste ou coluna adicionado | Informativo; consumidores estritos ainda podem quebrar |
| SQL/Python do modelo ou dependências declaradas de macros alterados | Informativo; obrigatório com `--review-model-logic` |

SQL arbitrário alterado exige revisão para avaliar se a regra ficou mais rigorosa ou permissiva. Argumentos de testes genéricos podem alterar o id gerado pelo dbt; a comparação usa uma identidade estável e expõe conjuntos ambíguos sem presumir correspondências. Comentários, formatação SQL, metadados descritivos e diferenças de banco/schema entre ambientes são ignorados. Literais e indentação Python continuam significativos. Chamadas puras de `config()` são comparadas pela configuração efetiva nos itens cobertos acima. Filtros de partição renderizados em execução usam o template original quando disponível. Helpers conhecidos de execução de testes dbt são incluídos consistentemente entre fases de docs/build; alterações nos corpos dessas macros continuam exigindo revisão.

A prévia de impacto percorre a união das dependências declaradas na referência e na candidata, incluindo exposures e recursos semânticos. Limita-se a 100 recursos por mudança e sinaliza truncamento. A UI exibe até 100 mudanças; o JSON da CLI contém todas. Colunas ausentes do catálogo geram lacunas explícitas e bloqueiam o preflight. Arquivos de artefatos ausentes geram erro.

## Uso local e no CI

Produza build completo e catálogo da candidata conforme [PREFLIGHT](PREFLIGHT.md). Preserve manifest/catálogo da versão aprovada e implantada em outro diretório. A referência precisa vir dessa versão, preservada independentemente do PR. Diretórios resolvidos iguais são rejeitados; arquivos copiados ou modificados ainda exigem armazenamento confiável de artefatos.

```bash
intentflow changes --target target --base baseline --format json > changes.json
# A prévia retorna 0 mesmo com revisões pendentes. Para bloquear:
intentflow changes --target target --base baseline --fail-on-review
# Exigir revisão também do código dos modelos e macros declaradas:
intentflow preflight --target target --root . --base baseline --review-model-logic
```

Códigos de saída de `changes`: 0 comparação concluída (ou sem revisões/lacunas com `--fail-on-review`); 1 revisões/lacunas pendentes com essa flag; 2 configuração/artefatos inválidos. A prévia precisa ser acompanhada do `preflight`, que verifica o build completo e os checks para decidir a promoção.

Uma mudança intencional exige o **id exato atual e uma justificativa preenchida**:

```bash
intentflow preflight --target target --root . --base baseline \
  --accept-change 'CHG-<id-do-relatorio>=Migração revisada; responsáveis validaram substituição'
```

Repita a flag para mudanças separadas. Curingas, ids desconhecidos/antigos, ids repetidos e justificativas vazias são rejeitados. O id vincula a transição antes/depois relatada; sua abrangência é essa mudança. Justificativas constam no JSON e na auditoria do store. Builds com falha, testes pulados, catálogos incompletos, checks indisponíveis e outros achados continuam bloqueando. A UI/API oferece consulta desta revisão, sem endpoint HTTP de aprovação.

O aceite é uma declaração do chamador da CLI. Revisão por outra pessoa, configuração protegida do CI e retenção confiável de artefatos precisam controlar quem pode fornecê-lo. PRs não confiáveis precisam ficar sem controle da referência e da própria política de checks obrigatórios. A aprovação de propostas do Enterprise é um fluxo separado; ela não aprova automaticamente estas mudanças.

Opcionalmente vincule os metadados da referência à revisão implantada esperada:

```bash
intentflow changes --target target --base baseline --base-revision "$DEPLOYED_SHA"
intentflow preflight --target target --root . --base baseline \
  --base-revision "$DEPLOYED_SHA" --revision "$CANDIDATE_SHA"
```

Manifest e catálogo de referência precisam conter o mesmo `metadata.env.INTENTFLOW_REVISION` exato, produzido com `DBT_ENV_CUSTOM_ENV_INTENTFLOW_REVISION` ao gerar essa referência. É uma verificação de consistência de metadados. A autenticidade depende da origem confiável dos artefatos. A flag `--revision` da candidata mantém as verificações existentes. Para a UI, salve `dbt.target`, `dbt.base_target` e opcionalmente `dbt.base_revision` no store do projeto selecionado.

## Diff do lineage de colunas

`intentflow lineage --target target --base base/target` lista as arestas de coluna adicionadas (`+`), removidas (`-`) ou com tipo alterado
(`~`, por exemplo `direct → derived`) e os modelos adicionados/removidos, pelo nome do modelo; `--json` imprime o mesmo como dados. Na UI,
**Lineage e impacto** mostra o painel quando `dbt.base_target` está configurado e nenhum modelo está selecionado. `intentflow lineage --json`
(sem `--base`) exporta todas as arestas; `intentflow openlineage emit --dry-run` imprime eventos OpenLineage para catálogos.

## Limites e validação complementar

A comparação lê metadados sem executar código do projeto. Consumidores não registrados, semântica de negócio, dispatch dinâmico de macros, comportamento Python dinâmico e todas as variáveis de execução, fixtures externas e versões dos dados precisam de validação complementar. Regenere artefatos em CI confiável após alterações; fixe dependências e fixtures. A flag estrita de código cobre SQL/Python registrados e macros declaradas, com limite às informações gravadas no manifest.

Mantenha build completo no banco de destino, testes de negócio, comparação exata ou amostrada de dados com limite explícito, cenários incrementais de reexecução/backfill/eventos atrasados e ensaios de implantação/rollback. Uma coluna adicionada pode quebrar um consumidor estrito. Uma comparação aprovada não prevê novos dados de produção ou falhas externas. Frescor de fontes e qualidade externa são obrigatórios somente quando exigidos no preflight. Vulnerabilidades das imagens continuam exigindo correção separada.
