# Validação dbt isolada e recuperação de alterações

[English](../VALIDATION_RECOVERY.md)

Estes recursos vêm no pacote principal (antes a extensão privada Enterprise); instalação: [INSTALLATION](INSTALLATION.md).

A varredura da imagem sandbox de 19/11/2026 encontrou 50 ocorrências HIGH de pacotes Debian (13 CVEs distintos), nenhuma CRITICAL e nenhuma HIGH/CRITICAL Python. O tratamento permanece pendente; veja [VALIDATION_STATUS](VALIDATION_STATUS.md).

## Preparação local

Requisitos: Linux, Docker com socket Unix local e armazém persistente para recuperação após reiniciar o processo.
O limite padrão de uma validação é 2 CPUs, 1 GiB de RAM, 128 processos e 600 segundos. Não usa GPU.
No máximo 2 validações rodam ao mesmo tempo **por processo Python** (`INTENTFLOW_DBT_MAX_VALIDATIONS`); a terceira nesse processo recebe a mensagem "tente de novo". Processos distintos têm semáforos separados: não existe um teto global coordenado para o host.

Construa a imagem dedicada a partir da raiz do repositório:

~~~bash
docker build -f docker/dbt-sandbox.Dockerfile -t intentflow-dbt-sandbox:local docker
~~~

A preparação baixa a imagem Python e bibliotecas públicas se não estiverem disponíveis. Ela não recebe arquivos do projeto dbt.
As validações seguintes usam a imagem instalada, resolvida pelo ID, com --pull=never; não baixam imagens ou pacotes.
Para preparar em ambiente sem internet, transporte a imagem previamente construída usando docker save/load.

A imagem usa dbt-core 1.12.5 e dbt-duckdb 1.11.0. As dependências transitivas e a base ainda não têm lock completo.
Configuração opcional, somente no ambiente do processo IntentFlow:

~~~bash
export INTENTFLOW_DBT_IMAGE=intentflow-dbt-sandbox:local
export INTENTFLOW_DBT_DOCKER_SOCKET=/var/run/docker.sock
~~~

Para Docker rootless, informe seu socket Unix local. Daemons TCP/remotos não são aceitos.
O servidor precisa conseguir usar esse socket. A instalação padrão da aplicação em Docker não monta o socket automaticamente;
para começar, rode a CLI/UI no host Linux. Acesso ao daemon deve ficar restrito ao operador confiável.

## O que a validação executa

~~~bash
intentflow fix validate ID --mode build
~~~

- Gera uma entrada com os caminhos de modelos, macros, seeds, testes, snapshots, análises e pacotes dbt declarados no projeto.
- Copia apenas arquivos SQL/YAML/CSV/Markdown/Python pertinentes. Exclui perfis originais, arquivos ocultos, bancos, chaves e caches.
- Recusa caminhos externos, symlinks, hardlinks, arquivos especiais, entradas acima de 128 MiB/20 mil arquivos e edições acima de 32 MiB.
- Aplica o diff somente nessa entrada e verifica novamente o hash original durante a cópia.
- Monta a entrada somente para leitura. Dentro do contêiner, copia para tmpfs e gera um perfil DuckDB apontando a /work/validation.duckdb.
- Usa usuário 10001, filesystem raiz somente para leitura, nenhuma capability adicional, no-new-privileges, rede desabilitada e recursos limitados.
- Não transmite credenciais do ambiente IntentFlow nem usa o profiles.yml original. O cliente Docker também recebe ambiente mínimo.
- parse verifica o projeto com o perfil de demonstração. build executa seeds e a seleção dos modelos modificados, seus ancestrais e descendentes.
- Registra modo, resultado, ID da imagem, adapter=duckdb e scope=local-demo. A saída retida é limitada; o contêiner é removido ao terminar.

Se Docker, a imagem ou o socket não estiverem disponíveis, a validação falha. **Não há fallback para executar dbt no host.**
A aplicação recusa validações antigas sem a identificação do isolamento; é necessário validar novamente.

O build usa `--indirect-selection buildable`: testes com dependências fora da seleção/ancestrais não são executados. Isso evita erros por tabelas irmãs não construídas; não equivale a testar o projeto inteiro. O resultado registra `test_selection=buildable`. A semântica está na [documentação de seleção de testes do dbt](https://docs.getdbt.com/reference/node-selection/test-selection-examples). Testes do escopo continuam obrigatórios e suas falhas bloqueiam a aplicação.

### Validação a partir de contêiner sem Docker (fila + worker)

Defina `INTENTFLOW_VALIDATION=remote` no processo da UI (imagem enxuta). `validate` passa a gravar um pedido e responder `202 {"queued": true}`
em vez de rodar o sandbox. Num host ou runner com Docker e o mesmo armazém, rode:

```bash
intentflow validate-worker            # em laço; --once para uma passada, --interval N segundos
```

O worker executa cada validação enfileirada exatamente como uma validação direta (mesmo isolamento, mesmas verificações, auditada como
quem pediu) e grava o resultado na proposta. O pedido é descartado se a proposta mudou depois de enfileirado. O worker precisa enxergar os arquivos do projeto no **mesmo caminho absoluto** registrado pela UI na proposta (monte o diretório no mesmo caminho). Espera-se um worker por
armazém; o teto de validações vale por processo; workers distintos não compartilham um semáforo global do host.

### Limites de validade

Este é um ambiente **local de demonstração DuckDB**, com banco temporário e dados de seeds do próprio projeto.
Não valida o comportamento de Snowflake, BigQuery, PostgreSQL ou credenciais/perfis de produção.
Projetos que dependem de fontes não reconstruíveis localmente, extensões não instaladas ou pacotes dbt ausentes podem falhar.
Pacotes devem estar preparados em dbt_packages (ou no caminho declarado); não há dbt deps com rede durante a validação.
Código e seeds copiados podem conter literais sensíveis; o isolamento não anonimiza o conteúdo.
A configuração de uma imagem alternativa é uma ação administrativa confiável.

O processo controlador encerra a execução em 600 s; o supervisor dentro da imagem também limita os comandos dbt a 570 s acumulados.
Um operador pode inspecionar contêineres remanescentes pelo label io.intentflow.validation=true em caso de falha do daemon/host.
Limites de Docker dependem do kernel/cgroups; contêiner não é uma VM independente.

## Aplicação e rollback recuperáveis

~~~bash
intentflow fix approve ID --user revisora
intentflow fix apply ID
intentflow fix rollback ID
~~~

Para cada projeto:

1. Obtém lock exclusivo entre operações IntentFlow no mesmo filesystem. Criação de propostas e leitura do contexto enviado à IA usam
   locks compartilhados/exclusivos no mesmo protocolo; outra operação concorrente recebe uma mensagem para tentar novamente.
2. Verifica todos os arquivos e hashes antes de escrever.
3. Grava .intentflow-transactions/active.json com bytes anteriores/posteriores e dados da proposta; confirma a gravação com fsync.
4. Persiste o estado applying ou rolling_back no armazém.
5. Substitui cada arquivo por rename atômico, preservando o modo POSIX; confirma arquivo e diretório com fsync.
6. Confirma applied ou rolled_back no armazém com o identificador da transação, registra auditoria e remove o journal.

O diretório do journal tem permissão 700 e o arquivo 600; ambos ficam fora do Git/contexto Docker.
O rollback preserva os bytes originais, incluindo CRLF. ACLs, atributos estendidos e metadados completos não são preservados.
Falha comum antes do commit tenta restaurar o lote automaticamente. Se a recuperação também falhar, o journal permanece disponível.

## Recuperar após interrupção

~~~bash
intentflow --store /caminho/intentflow.db fix recover ID --user revisora
~~~

Na interface: **Correções → Recuperar operação**. O endpoint é POST /api/proposals/ID/recover, com permissão de aplicar correções
(engineer/admin e escopo do projeto). Uma chamada repetida sem journal não altera arquivos.

- **Sem commit confirmado:** restaura o lote ao estado anterior e restaura o estado anterior da proposta.
- **Commit já confirmado:** confere os arquivos resultantes e conclui apenas a limpeza do journal.
- **Conteúdo diferente do anterior e do proposto:** recusa a recuperação inteira antes de restaurar qualquer arquivo, preservando a edição.
- **Outro armazém/proposta:** recusa; use o armazém que iniciou a operação.

Não apague o journal para liberar o projeto. Quando houver conflito, salve a edição posterior fora dos arquivos-alvo e compare com
os conteúdos do journal. Restaure deliberadamente uma das versões conhecidas nos arquivos em conflito, execute a recuperação e
reaplique a edição salva depois. Faça isso somente após encerrar qualquer operação ainda em andamento.

O lock coordena operações IntentFlow no mesmo filesystem com suporte a flock. A criação de propostas determinísticas e a leitura
dos arquivos enviados ao contexto da IA também usam o lock. A cópia de entrada da validação adquire lock compartilhado;
se uma aplicação estiver ativa ou interrompida, a validação recusa criar um snapshot misto. Editores e ferramentas externas precisam
respeitar o lock; o controle não impede outro processo do mesmo usuário de alterar arquivos entre uma verificação e um rename.
Leitores externos que não respeitam o lock podem observar parte do lote durante sua aplicação: **não há troca atômica simultânea
de vários caminhos**. Obter visibilidade atômica para qualquer leitor exige mudar o contrato do projeto para diretórios versionados
com um único ponteiro de geração, ou fazer os leitores cooperarem com esse lock.
Há recuperação durável e detecção de conflito. Use armazém persistente; Store(':memory:') não preserva o estado após encerrar o processo.
Não há coordenação distribuída entre hosts/filesystems independentes.

## Histórico de verificação — outubro de 2026

Os resultados abaixo são históricos, anteriores à separação das suítes Community e Enterprise. Eles não representam a contagem atual de testes de cada edição. Veja [VALIDATION_STATUS](VALIDATION_STATUS.md).

| Verificação | Resultado |
|---|---|
| Construção da imagem local | Aprovada; ID sha256:a8532fd7dbe60575587fb8bc01a8f36077c4ff7027bd3276cc4dcca78f6530d6 |
| Regressões novas, estado final | **23 aprovadas**, incluindo Docker/dbt reais e recuperação após saída abrupta |
| Suíte ampla anterior | **311 aprovados, 50 pulados, 1 falha** em 632,04 s |
| Falha daquela rodada | Teste preexistente de carga: p95 **18,45 s** (limite 15 s), sem erro de requisição; havia outras verificações rodando em paralelo |
| Teste de carga isolado anterior | **1 aprovado** em 49,02 s; p50 5,78 s, p95 **8,01 s**, máximo 8,91 s |
| Análise estática | Ruff E9/F63/F7/F82 aprovado; 71 módulos Python e JavaScript da UI com sintaxe válida; links locais conferidos |

A repetição aprovada não elimina a variação observada no teste de carga. Esses números não estabelecem uma garantia de latência em produção.
Os testes pulados permanecem não verificados. A suíte ampla foi coletada antes das últimas 9 regressões; estas estão incluídas na rodada final de 23.
O resultado amplo e a rodada direcionada se sobrepõem e não devem ser somados.

### Rodada histórica — 13/10/2026

Após o lock de dependências e as mudanças nos locks de leitura de propostas, a suíte ampla terminou com **318 aprovados, 52 pulados**,
sem falhas, em 271,59 s. Houve um aviso do cliente Snowflake sobre urllib3/chardet. A carga isolada passou em 11,70 s: p50 1,257 s,
p95 **1,806 s**, máximo 1,987 s (limite 15 s). Os 52 pulos continuam sem cobertura nesta rodada.

Código principal: [dbt_sandbox.py](../../src/intentflow/enterprise/dbt_sandbox.py), [file_transaction.py](../../src/intentflow/enterprise/file_transaction.py),
[proposals.py](../../src/intentflow/enterprise/proposals.py). Cobertura: [test_validation_recovery.py](../../tests/test_ee_validation_recovery.py).
Backups dos arquivos desta etapa e logs de verificação: /tmp/intentflow-isolation-recovery-20261013 (armazenamento temporário).

Os testes de interrupção usam os._exit no processo que altera arquivos e reabrem o armazém em outro processo.
Os testes do contêiner usam o Docker real com a imagem construída nesta máquina, arquivos sintéticos e um segredo fictício.
Nenhum serviço pago ou warehouse externo foi utilizado. O teste de rede usa um endereço reservado para documentação.
