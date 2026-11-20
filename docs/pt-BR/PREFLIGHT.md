# Validar antes de promover um build dbt

[English](../PREFLIGHT.md)

Recurso Unreleased do checkout: `intentflow preflight`. Wheels de releases anteriores não contêm esse comando. Complementa `check` e o `gate` antes da execução: exige evidência de **build dbt completo da versão candidata**, além da análise estática ou validação parcial do sandbox Enterprise.

## O que bloqueia a promoção

- Manifesto, catálogo ou resultado de build ausente/inválido; artefatos candidatos com mais de 24 horas por padrão, datas futuras ou sem fuso horário.
- Resultado de docs/parse/run/test usado no lugar do build; resultados vazios, recursos habilitados ausentes, IDs repetidos/incompatíveis, ausência de testes habilitados e estados de falha/pulo/desconhecidos.
- Catálogo incompleto/com erro em qualquer lado; projeto/adapter incompatíveis; SQL compilado divergente entre manifesto e build; arquivos SQL/Python atuais de modelos/testes diferentes do manifesto.
- Check obrigatório desabilitado, exceção interna de check ou consulta do warehouse que falha mesmo quando a sonda captura a exceção.
- Testes existentes, acompanhamento de regras ou interfaces removidos/alterados que exijam revisão contra a referência; colunas de catálogo ausentes em relações ativas. Veja [CHANGE_REVIEW](CHANGE_REVIEW.md).
- Achados na severidade de `--fail-on` ou acima dela, por padrão high. `--warnings-as-errors` também bloqueia avisos dbt.

Checks obrigatórios por padrão: SQL001/002, DBT002/003/004, SCH001/002/003, DBTR001/002/003 e BIZ001/002. O conjunto normal é executado, incluindo checks contínuos; a política não pode remover silenciosamente um obrigatório. `completed_checks` informa funções concluídas, não prova que toda integração, coluna ou regra foi avaliada. Um check sem sua entrada opcional pode não ter o que inspecionar.

`--require-check` acrescenta **IDs exatos**, não prefixos. PRB001–004/SQL012–014 exigem conexão disponível. PRB002/003 obrigatórios precisam de histórico: aquecimento inicial é lacuna, não aprovação. Preflight lê o histórico das sondas e não treina volume/nulos com os dados candidatos. Rode checks normais na base confiável primeiro. CTR001 obrigatório lê estritamente todos os contratos declarados; arquivos ausentes, inválidos ou sem definições suportadas não são ignorados.

A comparação de schema usa a identidade dbt quando homologação e produção têm schemas de relação diferentes. A comparação de mudanças também expõe recursos removidos e colunas/tipos alterados independentemente de consumidores conhecidos; build/testes completos e regras de negócio permanecem necessários.

## Evidência obrigatória de frescor e qualidade externa

`--require-source-freshness` exige `target/sources.json`, SLA de frescor para cada fonte declarada, IDs completos, artefato atual e revisão solicitada. Erro de dados/execução bloqueia; avisos seguem `--warnings-as-errors`. A idade do dado é reavaliada contra o SLA do manifesto no momento da promoção: uma medição antes aprovada não pode sobreviver ao SLA da fonte. Limites opcionais nulos do dbt são suportados. Cobre frescor de fontes dbt, não toda regra específica de completude/tempo do evento.

Para GX/Soda, configure `dq.results` e acrescente `--require-check DQ001`. Toda entrada precisa existir, conter resultados reconhecidos e não vazios, trazer data nativa de execução atual e corresponder inequivocamente ao projeto. Scan incompleto, resultado desconhecido, arquivo ausente ou mapeamento ambíguo bloqueiam. Os achados DQ001 aplicam a severidade configurada às falhas de qualidade; avisos podem ser elevados. Datas nativas vinculam a idade da execução, não seu commit ou snapshot exato dos dados; produza os arquivos no job confiável da candidata.

A evidência obrigatória adicional suporta SQL001–015, DBT001–005, DBTR001–003, SCH001–003, BIZ001/002, PRB001–004, CTR001 e DQ001. Outros checks conhecidos continuam disponíveis na checagem normal, mas são recusados como evidência obrigatória até terem contrato de entrada/cobertura implementado, evitando aprovação silenciosa por ausência de integração.

## Sequência na CI

1. Obtenha manifesto/catálogo de uma **base confiável aprovada**, em `baseline/`, preservando metadados de projeto e adapter. A candidata não deve ser sua própria base.
2. Construa a candidata em banco isolado de homologação com adapter real, dados representativos e versões/configurações compatíveis com produção. O IntentFlow não executa esse build por você.
3. Preserve o resultado do build antes da documentação e execute preflight no mesmo checkout/job confiável.
4. Torne o job obrigatório no PR e faça o deploy depender dele. O código de saída da CLI sozinho não impõe uma política no GitHub ou orquestrador.

Trecho shell desse job, na raiz do projeto dbt:

```bash
export DBT_ENV_CUSTOM_ENV_INTENTFLOW_REVISION="$GITHUB_SHA"
dbt build --target ci
cp target/run_results.json target/build_run_results.json
cp target/manifest.json target/build_manifest.json
# Quando o frescor for obrigatório (dbt 1.x):
dbt source freshness --target ci
dbt docs generate --target ci
cp target/build_run_results.json target/run_results.json
cp target/build_manifest.json target/manifest.json
intentflow preflight --target target --root . --base baseline \
  --revision "$GITHUB_SHA" --require-source-freshness --fail-on high --format json > preflight.json
```

Esses comandos dbt executam o código do projeto com a conta da CI. Configure-a para homologação e não forneça escrita em produção a PRs não confiáveis. Preserve/publique também artefatos de build que falhou; o `intentflow check`/watch normal ingere seus achados e histórico de regras. Se o build falhar, o job deve continuar reprovado mesmo que um diagnóstico posterior passe.

`--revision` confere `metadata.env.INTENTFLOW_REVISION` nos três artefatos candidatos. O dbt inclui essa informação nativamente: [metadados de artefatos dbt](https://docs.getdbt.com/reference/artifacts/dbt-artifacts). São metadados públicos; **não coloque segredos** nessas variáveis. Sem a opção, não se exige vínculo com o commit. É uma conferência de consistência de artefatos de CI confiável, não uma assinatura criptográfica nem verificação do worktree Git atual. Comparar SQL não verifica toda edição YAML/macro/seed; gere novamente os artefatos a partir da candidata.

Preservar o manifesto do build também evita confundir macros de tempo renderizadas novamente na documentação com o SQL executado. Regras SQL aplicam-se a modelos SQL; build/testes Python não equivalem a análise estática Python. Sem fontes, omita o comando de frescor e a opção que o exige.

Para contratos e sondas, configure o store e acrescente os IDs:

```bash
intentflow config set contracts.dir contracts
intentflow preflight --target target --root . --base baseline \
  --require-check CTR001,PRB001 --postgres-env CI_DATABASE_URL
```

Formatos de contrato e exemplos: [BUSINESS_RULES](BUSINESS_RULES.md). `--postgres-env` recebe o nome da variável com a URL. DuckDB usa `--duckdb`; outros bancos usam a configuração existente no store. Escolha severidade e tratamento de avisos conforme o negócio.

Códigos de saída: 0 passou no escopo configurado; 1 bloqueado; 2 configuração CLI inválida. JSON preserva lacunas, avisos, achados, checks obrigatórios/concluídos/com falha e contagens de recursos. É evidência da candidata nesse escopo, não certificado de prontidão para produção.

## Limites e próximas prioridades

Cobre integridade de artefatos/build dbt e os checks existentes. Não reproduz todo tráfego de produção, prova a semântica do negócio, impõe política externa de deploy, valida todas as credenciais de nuvem, descobre toda coluna sensível ou certifica atualização da origem. Frescor de fontes e resultados GX/Soda precisam ser exigidos explicitamente; sem as opções correspondentes, não são certificados pelo preflight. Integrações continuam precisando de configuração e validação próprias.

Prioridades seguintes: vincular qualidade externa ao snapshot exato dos dados; ampliar fixtures de incremental/backfill/fuso e eventos atrasados; integrar deploy e ensaiar recuperação no warehouse real. CVEs das imagens e validação de segurança/desempenho em produção continuam pendentes. Serviços, permissões e dados podem mudar depois da validação.
