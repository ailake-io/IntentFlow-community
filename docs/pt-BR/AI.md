# IA para sugerir correções (LLM) e decisão com Jev

[English](../AI.md)

As sugestões de IA vêm no pacote principal (antes Enterprise), sem licença. Instalação: [INSTALLATION](../INSTALLATION.md).

Dois papéis diferentes, de propósito:

| | **LLM** (Anthropic, OpenAI, Gemini, modelo local) | **Jev** (TypeSafe, modelo de decisão) |
|---|---|---|
| Faz | **Escreve** a edição do SQL | **Decide**: devolve escolha/pontuação/sim-não com probabilidade, nunca texto |
| Quando | Só quando a correção exige raciocínio | Triagem do achado, roteamento e porta de risco |
| Custo/latência | Segundos a minutos, caro | Rápido e barato (alegação do fornecedor; não medida aqui) |
| Quem manda | Pessoa: aprova e valida | Código Python lê o número e decide a política |

Nada é aplicado sem **aprovação humana + validação em sandbox**; a correção da IA entra no mesmo funil das correções por regra
(proposta → aprovar → validar → aplicar → reverter), com o diff à vista. A validação é vinculada ao hash da edição; uma política que exige
`build` não aceita `parse`. Revisão extra exige aprovador diferente do autor, inclusive se só houver um usuário cadastrado.
A validação usa Docker sem rede, ambiente mínimo, perfil DuckDB temporário e limites de recursos. Não usa credenciais originais nem
comprova compatibilidade de produção com outros warehouses. Preparação e recuperação: [VALIDATION_RECOVERY](VALIDATION_RECOVERY.md).

## Ligar (UI: Configuração → "IA para sugerir correções")

| Provedor | Chave (variável de ambiente) | Modelo | URL base |
|---|---|---|---|
| Anthropic (Claude) | `ANTHROPIC_API_KEY` | à sua escolha (sem padrão) | `https://api.anthropic.com` |
| OpenAI | `OPENAI_API_KEY` | à sua escolha (sem padrão) | `https://api.openai.com/v1` |
| Google Gemini | `GEMINI_API_KEY` | à sua escolha (sem padrão) | `https://generativelanguage.googleapis.com/v1beta` |
| Modelo local (Ollama, LM Studio, vLLM, llama.cpp, Docker Model Runner) | opcional | o que você tiver | `http://127.0.0.1:11434/v1` (Ollama) |

- **Chave:** variável de ambiente do servidor (recomendado) **ou** campo na tela, que guarda **cifrada** no armazém (exige
  `INTENTFLOW_SECRET_KEY`, ou SQLite em arquivo com o arquivo de chave). A chave **nunca** é devolvida pela API, nem gravada na auditoria.
- **Auditoria:** cada chamada grava `llm_call` com provedor, modelo, tokens e duração na trilha de auditoria; prompts, respostas e chaves nunca são gravados.
- **Modelo:** não há padrão; nada é chamado até um modelo ser definido. **Listar modelos** (ou `intentflow llm models`) pergunta ao provedor quais modelos a conta pode usar.
- **Listar modelos / Testar conexão:** botões na própria tela (usam os valores digitados, antes de salvar).
- **CLI:** `intentflow llm set --provider local --model qwen2.5:3b --base-url http://127.0.0.1:11434/v1`, `intentflow llm set-key`
  (pede a chave sem exibir), `intentflow llm test`, `intentflow llm models`, `intentflow fix-ai <id-do-achado>`.

Os nomes de modelos acima refletem os defaults do código, não comprovam disponibilidade atual nas contas dos provedores. Esses endpoints pagos não foram exercitados com chave real; use listar modelos/testar conexão com sua conta. Os resultados Ollama abaixo pertencem a uma rodada anterior. Evidências recentes: [VALIDATION_STATUS](VALIDATION_STATUS.md).

### Jev
Configuração → "Decisão com Jev". Dois acessos, conforme a documentação pública do Jev:

| Acesso | Chave | Chamada |
|---|---|---|
| `openrouter` | `OPENROUTER_API_KEY` | `POST https://openrouter.ai/api/alpha/decisions`, modelo `typesafe/jev-1.13` |
| `typesafe` | `TYPESAFE_API_KEY` | SDK `typesafe-sdk` (`pip install typesafe-sdk`), `client.system_one(state, questions)` |

Perguntas que o IntentFlow faz ao Jev sobre cada achado: **é um problema real?** (sim/não), **é provável falso positivo?** (sim/não),
**urgência** (pode esperar → hoje), **caminho de correção** (renomeação mecânica, expandir `SELECT *`, edição com LLM, metadado/teste,
investigar dados, nenhuma ação). Sobre cada edição gerada: **muda a semântica?**, **risco** (baixo/médio/alto), **toca lógica não
relacionada?**. Botão **Triar** nos Achados; chips "falso positivo provável 82%", "urgência: hoje", "caminho: …".

O que o código faz com o número (determinístico, em `jev.policy_for_risk`): risco ≥ 1,2 **ou** muda semântica ≥ 70% **ou** lógica não
relacionada ≥ 50% ⇒ validação em modo `build` e "revisão extra" destacada. O Jev nunca aprova, aplica ou silencia nada.

**Fluxo "Sugerir com IA"** (REFLEX): com Jev ligado, se o caminho indicado é mecânico e há correção por regra, **o LLM nem é chamado**;
se o Jev diz "nenhuma ação"/"investigar dados" com ≥ 80% de confiança, o IntentFlow avisa e só prossegue com "forçar".

## Privacidade: o que sai do servidor
SQL do modelo afetado (até 3 arquivos, 14 mil caracteres cada), nomes e tipos de colunas dos modelos relacionados, nomes dos modelos a
jusante, texto do achado e sua evidência. Os campos `sample`, `rows`, `observed`, `examples` são removidos no nível superior;
isso **não é anonimização**: evidências aninhadas, texto livre e literais SQL ainda podem conter dados sensíveis.
Um endpoint local evita o envio ao provedor remoto apenas se o servidor do modelo também operar localmente, sem encaminhamento externo.
Revise o conteúdo e a política da sua empresa antes de usar um provedor externo. Na revisão de 13/10/2026 nenhum provedor externo foi acionado.

## Como a edição é aceita (segurança)
O modelo devolve `edits` (`file`, `find`, `replace`), não o arquivo inteiro. Cada `find` precisa aparecer **exatamente uma vez**; só
arquivos do achado são aceitos; no máximo 8 edições; arquivos truncados no envio não são editados. Travas determinísticas:
**rejeita** remover `ref()`/`source()` (quebraria a linhagem) e **rejeita** adicionar `env_var()`, `run_query()`, `pre_hook`/`post_hook`, `on-run-*` ou `{% do/call/statement %}` (uma edição de IA nunca os introduz); **avisa e exige revisão extra** ao remover Jinja/macros ou alterar
mais de 24 linhas. Saída que não é JSON, edição ambígua ou fora do escopo vira erro claro; nada é criado. O escopo de escrita da pessoa (padrões por pasta/modelo) vale para todos os arquivos da proposta, na criação e a cada aprovar/validar/aplicar/reverter.

## O que observei com um modelo local real (qwen2.5:3b, Ollama)
O pipeline funcionou ponta a ponta (≈ 67 s). A qualidade **não** foi boa: para um `SELECT *` o modelo trocou três chamadas de macro
(`cents_to_dollars`) por `cast(... as decimal)`, que não tinha relação com o achado, inventou uma explicação e declarou 95% de confiança.
Por isso: (1) a "confiança" do modelo aparece como *autodeclarada, não calibrada*; (2) a remoção de macros dispara a trava e a revisão
extra; (3) modelos pequenos servem para testar o fluxo, **não** para confiar. Use modelos fortes e leia o diff.

## Limites
- **Jev:** verificado só contra servidor simulado com o formato documentado; a API está em acesso antecipado e pode mudar
  (o caminho `/api/alpha/decisions` é "alpha"). Pesos abertos (Open-Jev-2B, autotrust/JEV) existem na comunidade, mas **não** foram integrados.
- **Anthropic, OpenAI, Gemini:** formato de requisição/resposta verificado contra servidor local; nunca contra os serviços reais com chave real.
- **Local:** verificado contra Ollama real (listar modelos, chat, pipeline completo).
- Só edita arquivos `.sql` de modelos; correções em YAML (descrição, testes) ainda não. Dono (DBT003) e `unique_key` (DBT002) têm regra própria, sem LLM: a decisão é humana.
- Na UI a sugestão roda em segundo plano (`/ai-status`); com modelo local o limite padrão é 600 s. O resultado aparece com o diff e a pergunta "aplicar?"; erros ficam visíveis no diálogo.
- Sem custo/limite de gasto por chamada; use limites do próprio provedor.
