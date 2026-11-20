# Estado de validação — 19/11/2026

[English](../VALIDATION_STATUS.md)

Este documento registra evidências das rodadas de novembro. Números abaixo pertencem às configurações descritas; testes ignorados não contam como aprovados. Relatórios datados de outubro continuam como histórico.

## Código, instalação e CI

- Community no checkout: `0.3.5`; Enterprise: `0.3.4`, exigindo `intentflow>=0.3.5` e `cryptography`. Mudanças posteriores às releases ficam em `Unreleased`.
- Community funciona sozinho; Enterprise é plugin instalado no mesmo ambiente Python e depende de uma licença válida para habilitar recursos.
- Wheels dos dois checkouts foram construídos e instalados juntos em um venv limpo com Python 3.14.6; `pip check` e `intentflow --version` passaram. Isso não verifica acesso ao índice privado de clientes.
- CI em `main`: Python 3.11, 3.13 e 3.14. Actions usam Node 24 (`checkout@v6`, `setup-python@v6`; Community também `setup-java@v5`).
- Rodadas locais com Python 3.13.11 e 3.14.6: Community **287 aprovados / 44 ignorados**; Enterprise **120 aprovados / 16 ignorados**. São rodadas distintas, com extras instalados diferentes dos usados nas imagens.
- Rodada Enterprise em PostgreSQL 16: **120 aprovados / 16 ignorados**, com `INTENTFLOW_SECRET_KEY` descartável.
- CI Community também passou com PostgreSQL/Mailpit, Spark 3.5 e `dbt-ol` (estes dois últimos em Python 3.12, em jobs separados). Compatibilidade do núcleo com 3.14 não garante compatibilidade de runtimes externos com 3.14.
- CI Enterprise passa nas três versões; Keycloak e sandbox não são preparados por esse workflow. Keycloak real foi testado separadamente: **8 aprovados** após alinhar as asserções de idioma.

## Interface Enterprise e recuperação

No Chrome headless, usando cópia temporária de `dbt_shop` e SQLite: criar proposta → aprovar → validar build no container → aplicar → reverter passou. A reversão restaurou os bytes originais do SQL; nenhum erro JavaScript foi capturado. A licença/chave eram descartáveis, aceitas apenas pelo processo de teste.

O backup SQLite foi reaberto em outro Store: proposta, achados e auditoria coincidiram com o banco original. Isso não substitui restauração no PostgreSQL e na infraestrutura de destino. O teste de expansão de `SELECT *` validada por build no sandbox real também passou.

A aplicação em vários arquivos usa journal e recuperação; o rename é atômico **por arquivo**. Leitores externos que não respeitam o lock podem observar um lote parcial. A limitação de validações simultâneas do sandbox vale **por processo Python**, não como um limite global entre processos do host.

## Imagem auditada

Imagem `intentflow-community:local-review`, ID `sha256:6ceb078bc1a2d81e94067d3864cc73b1193000cdb9f2f0b534f440a137cef630`. [Snapshot Trivy da rodada](../security/scan-2026-11-19.json).

- Base Debian: **50 ocorrências HIGH / 13 CVEs distintos / 0 CRITICAL** no filtro usado.
- **3 ocorrências** tinham correção indicada; as demais não tinham versão corrigida indicada para essa base no banco consultado.
- Pacotes Python da imagem: **0 HIGH/CRITICAL**; severidades menores ficaram fora desta consulta.
- Os resultados indicam pacotes potencialmente afetados, não exploração comprovada do aplicativo. A auditoria de uma imagem não cobre todas as imagens customizadas Enterprise nem todos os extras opcionais.
- Tratar correções disponíveis, reavaliar os demais achados e repetir o scan antes de produção. A descoberta foi registrada; os CVEs não foram corrigidos por esta atualização de documentação.

## Laboratório dbt local

O projeto separado `../intentflow-dbt-tests` foi criado nesta máquina, fora dos dois repositórios. Não faz parte dos wheels nem de um clone novo. Tem 3 seeds, 7 modelos, 16 testes dbt, dados fictícios `example.invalid` e Python 3.13.11. Consulte seu README.

- `baseline`: 26 recursos dbt concluídos, sem falha; IntentFlow detectou SQL001, SQL003, DBT002, DBT003 e DBT004; gate `--fail-on high` retornou 1 como esperado.
- `data-error`: uma falha de unicidade esperada, três dependentes pulados; IntentFlow detectou DBTR001 adicional.
- Baseline foi reexecutado ao terminar para restaurar os dados. O script usa `--full-refresh`; não o use em um banco de produção.
- `dbt docs generate` substituiu `run_results.json` na versão utilizada. O roteiro preserva o resultado do build e o restaura depois da geração do catálogo.
- A primeira rodada do laboratório expôs uma lacuna em DBTR003 após falha de teste. Uma correção Unreleased na develop agora informa esses modelos pulados, além daqueles pulados após erro de modelo.

## Ainda pendente

Piloto com pipeline e dados reais de usuário; acesso ao canal privado de distribuição; provedores e integrações externas sem conta de teste; tratamento das vulnerabilidades das imagens; verificação no ambiente de produção. O catálogo [INTEGRATIONS](INTEGRATIONS.md) do Community contém resultados de rodadas anteriores, não promete cobertura de todos os serviços a cada execução da suíte.

## Correções Unreleased do piloto com dados reais (develop)

Um projeto local separado, `../intentflow-retail-pilot`, baixa o UCI Online Retail completo (541.909 linhas; CC BY 4.0). Preserva XLSX e hashes e distingue características naturais dos dados de falhas injetadas. Antes da ampliação preventiva, a rodada das correções concluiu novamente todos os cenários Community, a validação isolada Enterprise pelo navegador, aplicação, rollback byte a byte e reabertura do backup SQLite. O baseline dbt terminou com 16 construções bem-sucedidas, 22 testes aprovados e 4 avisos de qualidade da origem, sem falhas ou pulos. Premissas e medições ficam no README e nos relatórios do laboratório; isso não é um piloto de produção.

- Community: DBTR003 agora informa modelos pulados após falha/erro de teste, mesmo sem modelo com status error. Quatro casos de regressão reproduzem as falhas e descartam falsos alertas por warn/pass. Testes locais relacionados: 28 aprovados.
- Enterprise: o build isolado usa seleção indireta buildable, evitando testes que referenciam irmãs não construídas. A regressão em container real também verifica que teste inválido dentro do escopo continua sendo recusado. Testes locais de validação/recuperação: 25 aprovados. O escopo é registrado no resultado; testes de irmãs fora dele não são cobertos pelo sandbox.
- As vulnerabilidades originais da imagem Debian continuam pendentes; essas correções de código não tratam o SO base. As correções do checkout não substituem wheels de releases já publicadas.

## Validação preventiva e regras de negócio (Unreleased)

O checkout develop acrescenta o preflight de evidência de build completo e a CLI/UI de regras baseadas em testes nativos dbt marcados. Veja [PREFLIGHT](PREFLIGHT.md) e [BUSINESS_RULES](BUSINESS_RULES.md) para escopo e lacunas. Regressões cobrem artefatos incompletos/antigos/misturados, checks desabilitados/com erro, falhas de consulta capturadas, aquecimento de histórico, contratos estritos, sete regras com dbt real, frescor com comando dbt real e fixtures externas atuais/reconhecidas/mapeadas. O sandbox parcial Enterprise não substitui build completo no warehouse alvo.

O baseline de varejo ampliado tem 6 seeds, 10 modelos e 30 testes: 26 aprovados, 4 avisos naturais e nenhuma falha/pulo. Cinco regras e sua UI/histórico passaram. O gate integral continua bloqueando repetições candidatas na origem preservada; o preflight positivo usa exceção explícita de PRB001 somente em outro store de QA, mantendo os testes da chave da fato.

## Revisão de mudanças — 2026-11-20 (Unreleased)

A [revisão de mudanças](CHANGE_REVIEW.md) compara testes, schema/interfaces, contratos nativos, chaves incrementais e políticas de frescor existentes. O preflight bloqueia controles removidos/alterados mesmo quando os testes restantes passam. Há revisão estrita opcional de código e vínculo à revisão implantada esperada. Aceites exigem ids/justificativas exatos e mantêm as demais validações obrigatórias.

- Suíte Community local completa: **415 aprovados / 44 pulados**. Uma regressão dbt nativa adicionada depois foi validada na suíte específica de comparação, com **37 aprovados**; os conjuntos se sobrepõem e não devem ser somados. A CI cobre a coleta final.
- Suíte Enterprise: **129 aprovados / 10 pulados**, incluindo sandbox Docker local. Keycloak (8), GitLab (1) e Ollama (1) ficaram sem configuração.
- Aceitação dbt real: referência aprovada; regra removida, regra com `where 1=0` e expectativa unitária embutida reduzida passam no dbt e são bloqueadas pelo preflight. Aceites exatos de mudanças intencionais passam; a fixture foi restaurada ao final.
- Tela de revisão em português no Chrome: ids/estado de revisão corretos e nenhum erro JavaScript. Os artefatos incluem um teste unitário nativo.
- Comparação sintética de metadados de 5.000 modelos: **0,486 s**, com prévia de impacto limitada a 100 recursos e truncamento explícito. É uma medição de metadados, sem benchmark do warehouse de produção.

A fixture, logs, JSON e captura de tela ficam separadamente em `../intentflow-retail-pilot/reports/change-review/` e `change-review-results.json`, fora dos pacotes. O guia detalha limites de consumidores, execução e snapshots. CVEs das imagens e ensaios de deploy/recuperação no warehouse de destino seguem pendentes.

A validação adicional com o baseline UCI completo de 541.909 linhas expôs dependências internas dbt presentes no manifest de build e ausentes no de docs. Esses vínculos conhecidos agora são normalizados, preservando revisão de alterações no código das macros. O build de varejo atualizado tem 16 builds aprovados, 26 testes aprovados, 4 avisos naturais e nenhuma falha/pulo; a revisão tem zero mudanças/lacunas e a exceção PRB001 documentada de QA isolado passa. Warehouse original, XLSX e seis CSVs mantêm seus hashes. Regressões relacionadas de comparação/preflight/regras após a correção: **113 aprovadas**, com sobreposição às rodadas anteriores.
