# Revisor de segurança com IA

Um agente que **lê o código do seu projeto**, procura falhas de segurança e envia um relatório para o seu
**Telegram**. Se você aprovar com um clique, ele manda um resumo para um canal do **Slack**.

Ele **só lê**: nunca altera o código, nunca acessa o banco de dados e nunca executa nada do seu sistema.

Feito para projetos **Supabase** (Postgres, RLS, Edge Functions) **+ React/TypeScript**. A análise usa o
**Claude**, da Anthropic.

---

## Sumário

- [Em 1 minuto](#em-1-minuto)
- [Instalação](#instalação)
- [Primeiros passos](#primeiros-passos)
- [Comandos](#comandos)
- [O que você vê](#o-que-você-vê)
- [Quanto custa](#quanto-custa)
- [Segurança e privacidade](#segurança-e-privacidade)
- [Adaptando ao seu projeto (perfis)](#adaptando-ao-seu-projeto-perfis)
- [Para quem é técnico](#para-quem-é-técnico)
- [Solução de problemas](#solução-de-problemas)
- [Licença](#licença)

---

## Em 1 minuto

Pense num **auditor de segurança que recebe uma fotocópia do seu código**. Ele lê com calma, procura portas abertas
e escreve um relatório. Ele não tem caneta para escrever no original, nem a chave do prédio.

O que ele procura, por exemplo:

| Tipo de falha | Exemplo do que ele aponta |
|---|---|
| Porta aberta | Uma função que qualquer pessoa na internet consegue chamar sem login |
| Usuário com poder demais | Um usuário "só leitura" que consegue apagar registros |
| Um cliente vendo dados de outro | Em sistemas com várias empresas no mesmo banco, a empresa A lendo dados da empresa B |
| Vazamento de dados pessoais | Documento ou telefone aparecendo em logs ou exportações sem controle |
| Segredos no código | Senha ou chave de API escrita direto no código |
| Bibliotecas vulneráveis | Pacotes de terceiros com falhas já conhecidas |

Ele revisa **um pedaço do código por vez** e lembra o que já revisou. Assim, a base inteira é coberta aos poucos e
nada é revisado de novo sem necessidade.

### Como funciona

```
                     ┌──────────────────────────────┐
  GitHub ──cópia──▶  │ 1. Verificações automáticas  │  scripts, sem custo
  (só leitura)       │ 2. Revisão pela IA (Claude)  │  lê o código, confirma cada suspeita
                     │ 3. Verificação cética        │  tenta derrubar cada achado
                     └──────────────┬───────────────┘
                                    ▼
                       Telegram: relatório + botões
                                    │  (clique em "Enviar ao Slack")
                                    ▼
                            Slack: resumo do achado
```

---

## Instalação

**Você precisa de:**
- **Linux, macOS ou Windows com WSL** (no Windows, use o terminal do WSL/Ubuntu)
- **git**
- **Python 3.11 ou mais novo** (ou o [uv](https://docs.astral.sh/uv/), que instala o Python sozinho)
- Uma **chave da API da Anthropic**: [console.anthropic.com](https://console.anthropic.com) → API Keys
- Opcional: um **bot do Telegram** (criado com o [@BotFather](https://t.me/BotFather)) e um app do Slack

**Três comandos:**

```bash
git clone https://github.com/OticaPublicaFlp/revisor-de-seguranca.git
cd revisor-de-seguranca
./install.sh
```

O `install.sh` cria um ambiente Python isolado, instala as dependências e cria o atalho **`check`**, que funciona
de qualquer pasta. No final, ele oferece abrir o assistente de configuração.

---

## Primeiros passos

### 1. Configure, respondendo às perguntas

```bash
check configurar
```

O assistente pergunta uma coisa por vez e **testa cada chave na hora**, sem gastar créditos:

```
━━ 3/7 · Inteligência artificial (Anthropic) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  A chave fica em console.anthropic.com → API Keys. Dica: crie uma chave só para o revisor e defina um
  limite de gasto mensal no console. A validação abaixo é gratuita.
  Modelo
    1) Claude Opus 5.5 — o mais preciso (recomendado para segurança)
    2) Claude Sonnet 5.5 — metade do custo, um pouco menos minucioso
  Escolha [1]:
  Chave da Anthropic (começa com sk-ant-) — o que você digitar não aparece:
  testando a chave...
  ✓ chave aceita e modelo claude-opus-5-5 disponível (consulta gratuita)
```

Ele também descobre sozinho o grupo do Telegram: você adiciona o bot ao grupo, envia `/start@seu_bot` e escolhe
o grupo na lista. As chaves ficam no arquivo `.env`, que só o seu usuário consegue ler e que nunca vai para o git.

### 2. Confira se está tudo certo (grátis)

```bash
check verificar
```

```
▶ Diagnóstico (sem custo de IA)
  ✓ Python 3.12.3
  ✓ .env com permissão 0o600
  ✓ perfil 'meu-projeto' (MeuProjeto) carregado
  ✓ repositório acessível, branch 'main' encontrada
  ✓ chave aceita e modelo claude-opus-5-5 disponível (consulta gratuita)
  ✓ Telegram: bot @meu_revisor_bot
  ✓ Telegram: bot no chat -1001234567890
  · Slack não configurado (o botão do Slack não aparece no Telegram)
  ✓ osv.dev acessível (checagem de dependências)
  ✓ atalho `check` instalado

Tudo certo. Próximo passo: `check simular`.
```

### 3. Faça uma revisão de teste (só na tela)

```bash
check simular
```

Revisa as próximas partes do código e mostra tudo na tela. **Não envia nada e não grava nada.** Tem custo de IA.

### 4. Use de verdade

```bash
check            # revisa e envia o relatório ao Telegram
check run        # modo automático: revisa todo dia no horário configurado
```

---

## Comandos

| Comando | O que faz | Custo de IA |
|---|---|---|
| `check configurar` | Assistente de configuração | não |
| `check verificar` | Confere configuração e conexões | não |
| `check pistas` | Mostra o que as verificações automáticas encontraram e as bibliotecas vulneráveis | não |
| `check units` | Lista as partes do código, o estado de cada uma e quais serão revisadas a seguir | não |
| `check simular` | Revisão completa, só na tela (sem enviar, sem gravar) | sim |
| `check` | Revisão real: envia o relatório ao Telegram e marca as partes como revisadas | sim |
| `check run` | Fica rodando: revisa no horário agendado e responde aos botões do Telegram | sim |
| `check bench` | Mede quantas falhas reais (já corrigidas) o agente encontra | sim |
| `check --debug ...` | Qualquer comando, mostrando o detalhe técnico se der erro | — |

---

## O que você vê

### No terminal, enquanto ele trabalha

```
▶ Revisando o código: supabase/functions/export-records — 1 arquivo(s) (8.214 caracteres)
  2 pista(s) dos scripts automáticos para conferir
  🔎 read_file(path='supabase/config.toml')
     ↳ 41 linhas, 1032 caracteres
  💭 The function has verify_jwt disabled; I should check whether the handler validates the caller...
  🔎 search_repo(pattern='getUser', path='supabase/functions/export-records')
     ↳ nenhuma ocorrência
  [revisão · volta 3] 14s · tokens: entrada=3 (cache=9.812) saída=1.204
  Resumo:
    Li o index.ts inteiro e o config.toml. A função não valida o chamador e usa service role...

▶ Verificando 1 achado(s)
  ✓ confirmado: Função pública exporta clientes de qualquer clínica — index.ts:14 cria o cliente com...
  ■ CRÍTICO [EF-2] Função pública exporta clientes de qualquer clínica
    supabase/functions/export-records/index.ts:14

  Resumo: 6 chamadas ao modelo em 71s · tokens: entrada=18 (cache=58.140) saída=6.022 · custo estimado ≈ US$ 0,43
```

O 💭 é um resumo do raciocínio do modelo, gerado pela API (vem em inglês).

### No Telegram

```
Revisão #12 — AppExemplo
main@3f9c2a1

🔴 1 Crítico · 🟡 2 Médio

🔴 Função pública exporta clientes de qualquer clínica
supabase/functions/export-records/index.ts:14
🟡 Relatório mensal lê só as primeiras 1000 consultas
supabase/functions/monthly-report/index.ts:31
...

Cobertura: 37/145 unidades. Revisadas agora: supabase/functions/export-records, ...

Enviar o resumo ao Slack?
[ ✅ Enviar ao Slack ]  [ ❌ Descartar ]
```

Junto vem um arquivo `.md` com o relatório completo: descrição, por que importa e como corrigir cada achado.
Os botões só aparecem se o Slack estiver configurado.

### Um achado completo (do arquivo anexo)

> **🔴 [Crítico] `EF-2` Função pública exporta clientes de qualquer clínica**
> `supabase/functions/export-records/index.ts:14`
>
> A função tem verify_jwt=false no config.toml e não valida usuário, papel nem segredo interno. Como usa service
> role (ignora a RLS) e o organization_id vem do corpo da requisição, qualquer pessoa na internet lista os
> pacientes de qualquer clínica.
>
> **Sugestão:** Validar o usuário com auth.getUser(token), ler o papel e o organization_id do perfil dele no banco
> e só então consultar, filtrando por esse organization_id.

---

## Quanto custa

O custo é o da API da Anthropic, pago por uso. Cada "parte" revisada custa aproximadamente:

| Modelo | Com segunda revisão (atacante) | Sem segunda revisão |
|---|---|---|
| Claude Opus 5.5 (padrão) | ~US$ 0,80 | ~US$ 0,45 |
| Claude Sonnet 5.5 | ~US$ 0,40 | ~US$ 0,22 |

Exemplo: 5 partes por dia com o Opus e a segunda revisão dão ~US$ 4 por dia. Depois que a base inteira foi
revisada uma vez, o custo cai bastante, porque ele só revisa de novo o que mudou (ou o que passou de 30 dias).

O **valor real** aparece no fim de cada execução (`custo estimado ≈ US$ ...`). Para gastar menos: `MAX_UNITS` menor,
`DUAL_REVIEW=off`, `EFFORT=low` ou o Sonnet. Dica: defina um **limite de gasto mensal** no console da Anthropic.

As verificações automáticas, `check pistas`, `check units` e `check verificar` não gastam nada.

---

## Segurança e privacidade

**O que ele pode fazer**
- Baixar uma cópia do código (`git clone`/`fetch`) e ler essa cópia.
- Enviar trechos do código para a API da Anthropic, para análise.
- Enviar nome e versão das bibliotecas para o [osv.dev](https://osv.dev) (desligue com `DEPS_CHECK=false`).
- Enviar o relatório ao Telegram e, com aprovação, ao Slack.

**O que ele não pode fazer**
- **Alterar o código.** O programa só usa comandos de leitura do git. Não existe `push`, `commit` nem `reset` no código.
- **Acessar o banco de dados.** Ele não recebe nenhuma credencial do banco. As migrations são lidas como texto.
- **Executar ou atacar o seu sistema.** A IA só tem três "botões", todos de leitura: ler um arquivo, procurar
  um texto e listar uma pasta. Ela não roda comandos, não acessa a internet e não envia nada. O "revisor com
  olhar de atacante" só *imagina* ataques ao ler o código.
- **Ler as próprias chaves.** A leitura bloqueia `.env`, chaves privadas e a pasta `.git`.

**Cuidados que dependem de você**
- **Credencial só de leitura.** Prefira um token do GitHub *fine-grained* com permissão **Contents: Read-only**, ou
  uma *deploy key* sem "Allow write access". Usar a sua credencial pessoal funciona, mas dá ao programa mais
  acesso do que ele precisa.
- **Quem lê o relatório.** Ele mostra onde o sistema é vulnerável. Mantenha no grupo do Telegram só pessoas de
  confiança. Por padrão, o Slack recebe só título e arquivo, sem a descrição.
- **Seu perfil é privado.** Ele descreve o funcionamento interno do seu sistema e fica fora do git
  (veja [perfis](#adaptando-ao-seu-projeto-perfis)).
- **Texto malicioso no código** pode tentar enganar a IA ("ignore as instruções..."). No pior caso, o relatório
  sai errado. A IA continua sem conseguir alterar nada, e o revisor é instruído a reportar a tentativa.

---

## Adaptando ao seu projeto (perfis)

A qualidade da revisão depende de a IA entender o seu sistema. Isso fica num **perfil**, uma pasta em
`profiles/<nome>/`:

| Arquivo | O que é | Obrigatório |
|---|---|---|
| `perfil.toml` | Nome do projeto e os nomes que as verificações automáticas procuram | sim |
| `contexto.md` | O que o sistema faz, quem usa, quais são os papéis, o que é dado sensível | sim |
| `regras.md` | O checklist: as regras de segurança do seu projeto, com IDs | sim |
| `exemplos.md` | Exemplos de achados no formato esperado (ajuda a calibrar o rigor) | não |
| `casos.json` | Falhas reais já corrigidas, para o benchmark | não |

O perfil `exemplo` vem pronto, com um SaaS fictício e um checklist genérico de Supabase + React. Para o seu projeto:

```bash
check configurar     # escolha "Criar um perfil novo" — ele copia o exemplo para profiles/<seu-projeto>/
```

Depois, edite dois arquivos:

**`contexto.md`**: escreva como explicaria o sistema a um consultor de segurança:
```markdown
O MeuSistema é um ERP para escolas. Várias escolas usam o mesmo banco, separadas por `school_id`.
Papéis: diretor (tudo), secretaria (cadastros), professor (só as próprias turmas), responsável (só o filho).
Dados sensíveis: dados de alunos menores de idade, notas, laudos médicos.
Para o olhar de atacante: "outro cliente" = outra escola; o papel mais baixo é o responsável.
```

**`regras.md`**: o checklist, em seções `#### PREFIXO — título`. Os prefixos decidem quais regras cada parte do código
recebe: `EF` (Edge Functions), `DB` (consultas), `SQL` (migrations), `FE` (front-end), e também `REL`, `AI` e `CX`
para Edge Functions. Bons lugares para tirar regras: os `AGENTS.md`/`CLAUDE.md` do projeto, os post-mortems e as
correções de segurança que vocês já fizeram.

**Os perfis próprios nunca vão para o git** (o `.gitignore` só deixa passar o `exemplo`). Guarde uma cópia em
local seguro, por exemplo um repositório privado.

---

## Para quem é técnico

### Arquitetura

```
reviewer/
├── main.py       CLI, pipeline (modo code/diff), daemon (agendador + polling do Telegram), callbacks
├── units.py      divide a branch em unidades (função, lote de migrations, pasta do front), fingerprint por blob
├── prechecks.py  pistas determinísticas: estado final das migrations, Edge Functions, segredos, PII, XSS
├── themes.py     4 revisões transversais sobre inventários gerados por script
├── deps.py       dependências vulneráveis via OSV (package-lock.json + deno.lock)
├── llm.py        laço agêntico com streaming, ferramentas só de leitura, verificação, custo
├── tools.py      read_file / search_repo / list_dir sobre objetos do git (sem working tree)
├── profile.py    carregamento dos perfis
├── wizard.py     check configurar / check verificar
├── errors.py     tradução de erros para português + ocultação de segredos
└── ui.py, report.py, telegram.py, slack.py, gitrepo.py, db.py (SQLite), bench.py, config.py
prompts/          modelos genéricos (review, attack, verify) com {{PROJETO}}, {{CONTEXTO}}, {{EXEMPLOS}}
profiles/         exemplo/ (público) e os seus perfis (ignorados pelo git)
```

### Camadas de verificação

| Camada | O que faz |
|---|---|
| **Pistas (scripts)** | Reduz as migrations ao **estado final** (última definição de cada função/policy/GRANT, quem executa cada função, inclusive `REVOKE` por SQL dinâmico em blocos `DO`). Aponta: `verify_jwt=false` sem autenticação; service role sem filtro de tenant; `SECURITY DEFINER` executável pelo app que escreve sem conferir papel ou sem `search_path`; policies/GRANTs para `anon`/`public`; `fetch` em URL não validada; segredos (chaves conhecidas e JWT com `role=service_role`, sem nunca mostrar o valor); PII em log; `dangerouslySetInnerHTML`. Viram pistas que o revisor confirma ou descarta. |
| **Dependências** | OSV `querybatch`. Dependência só de dev vale no máximo `low`; pacote instalado fora do registro npm vira `info` com ressalva (o OSV só conhece versões do npm). |
| **Revisão por unidade** | Cada unidade recebe só as seções do checklist do seu tipo e as pistas dos seus arquivos. |
| **Temas transversais** | `entradas`, `acesso-publico`, `dados-sensiveis`, `funcoes-privilegiadas`. Reavaliados quando o inventário muda. |
| **Revisão dupla** | `DUAL_REVIEW=critical`: Edge Functions, migrations e temas também passam pelo prompt de atacante (perfis: anônimo, papel mais baixo, outro tenant, usuário desativado, quem controla a entrada). |
| **Verificação** | Um terceiro prompt tenta refutar cada candidato, une duplicados e ajusta a severidade. |

Seleção das unidades por execução (`MAX_UNITS`): primeiro as **alteradas**, depois as **nunca revisadas** (temas e
Edge Functions primeiro), depois as que passaram de `RECHECK_DAYS`. O estado fica em `data/state.db`.

### Configuração completa (`.env`)

| Variável | Padrão | Descrição |
|---|---|---|
| `PROFILE` | `exemplo` | Pasta em `profiles/` (ou um caminho) |
| `REPO_URL` / `REPO_BRANCH` | — / `main` | Repositório revisado |
| `REPO_TOKEN` | vazio | Token só de leitura (enviado por `GIT_CONFIG_*`, fora do `argv` e do `.git/config`) |
| `REPO_SSH_KEY` | vazio | Chave da deploy key (ignora o agente SSH e o `~/.ssh/config`) |
| `REVIEW_MODE` | `code` | `code` (código atual, por unidades) ou `diff` (só o que mudou) |
| `MAX_UNITS` / `RECHECK_DAYS` | `5` / `30` | Unidades por execução / revisão periódica |
| `ANTHROPIC_API_KEY` / `REVIEW_MODEL` | — / `claude-opus-5-5` | Chave e modelo |
| `EFFORT` | `medium` | `low` · `medium` · `high` · `xhigh` · `max` |
| `DUAL_REVIEW` | `critical` | `off` · `critical` · `all` |
| `VERIFY` | `true` | Etapa de verificação cética |
| `DEPS_CHECK` | `true` | Consulta ao OSV |
| `MAX_TURNS` / `MAX_OUTPUT_TOKENS` | `14` / `32000` | Limite de voltas por etapa / de saída por chamada (inclui o thinking) |
| `CHUNK_CHARS` / `MAX_CHUNKS` | `60000` / `6` | Tamanho de unidade / blocos de diff |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | — | Bot e conversa (grupo = id negativo) |
| `TELEGRAM_APPROVER_IDS` | vazio | Quem pode aprovar o envio ao Slack (vazio = qualquer membro) |
| `SLACK_BOT_TOKEN` / `SLACK_CHANNEL_ID` | vazio | App com `chat:write` e ID do canal |
| `SLACK_INCLUDE_DETAILS` | `false` | Mandar a descrição ao Slack |
| `CRON_EXPR` / `TZ` | `0 8 * * *` / `America/Sao_Paulo` | Agenda do `check run` |

### Modelo e API

Claude via `client.beta.messages.stream`:
- `thinking: {type: "adaptive", display: "summarized"}` e `output_config.effort`;
- cache do system prompt e do histórico;
- `fallbacks: "default"` (beta `server-side-fallback-2026-07-01`) para recusas.

Particularidades do Opus 5.5 tratadas no código:
- **Sem `tool_choice` forçado** (retorna 400). A entrega é pedida por instrução, e leituras depois de `MAX_TURNS`
  são recusadas até o modelo entregar.
- **Histórico só cresce.** Editar o histórico invalida os blocos de thinking.
- **Nada de pedir raciocínio na resposta.** É recusado como `reasoning_extraction`. O schema pede um `resumo`.
- **Recusas** viram `ModelRefusal`, com mensagem explicada.
- **Conexão que cai no meio do streaming** refaz a volta (3 tentativas).

### Benchmark

`profiles/<perfil>/casos.json` lista falhas reais já corrigidas no histórico do seu repositório:

```json
{"cases": [{
  "id": "funcao-cron-aberta", "title": "Cron aceitava a chave anônima",
  "fix_commit": "<sha da correção>", "prefix_commit": "<sha anterior à correção>",
  "files": ["supabase/functions/send-reminders/index.ts", "supabase/config.toml"],
  "rule_groups": ["EF", "DB"], "expected": "O que um bom revisor deveria apontar",
  "match_any": ["chave anônima", "sem autenticação", "x-cron-secret"], "match_file": "send-reminders",
  "severity_min": "high"
}]}
```

```bash
check bench --limite 3           # primeiros 3 casos
check bench --inicio 4           # continua do caso 4
check bench --sem-regras         # sem o checklist: mede se ele acha a falha sozinho
```

O agente revisa o código **anterior** à correção (pelos objetos do git, sem checkout). Os resultados ficam em
`data/bench/`. O casamento é por palavra-chave, então confira os casos "parcial" e "perdeu". Se o seu checklist foi
escrito a partir dessas mesmas correções, o resultado com regras mede *aplicação* das regras; `--sem-regras` mede
*generalização*.

### Desenvolvimento

```bash
.venv/bin/python -m unittest discover -s tests     # ~100 testes, sem rede e sem custo
```

Os testes usam `tests/fixtures/perfil/`, repositórios git temporários e um cliente da API simulado (que também confere
os parâmetros enviados ao modelo).

### Limitações

- Revisa **código**. Não vê configurações do painel do Supabase, segredos do Vault nem o estado real do banco em
  produção.
- Pode deixar passar falhas (falso negativo) e apontar coisas que não são falhas (a verificação reduz, mas não
  zera). Use como uma camada a mais, não como garantia.
- As pistas de SQL são heurísticas (regex sobre migrations), não um parser completo do Postgres.
- O `check run` precisa ficar rodando num terminal (ou num serviço). Com o computador desligado, ele roda a revisão
  perdida quando voltar.

---

## Solução de problemas

| Mensagem | O que fazer |
|---|---|
| `Configuração incompleta: falta ...` | Rode `check configurar` |
| `Chave da Anthropic recusada (401)` | Gere outra chave no console e rode `check configurar` |
| `Saldo de créditos da Anthropic insuficiente` | Adicione créditos em console.anthropic.com → Plans & Billing |
| `Sem acesso ao GitHub (problema de rede)` | Teste `curl -4 -I https://github.com`. No WSL, rode `wsl --shutdown` no PowerShell |
| `O GitHub recusou o acesso ao repositório` | Confira a URL e o token (precisa de leitura no repositório) |
| `Falha de rede: expirou o tempo de resposta` | A internet oscilou. O agente já tentou 3 vezes; rode de novo |
| `O Telegram recusou o envio` (bot fora do grupo) | Adicione o bot ao grupo e rode `check configurar` |
| `O modelo recusou analisar este trecho` | Os filtros de segurança da Anthropic às vezes barram código de segurança; a parte é tentada de novo depois |
| `check: comando não encontrado` | Rode `./install.sh` e, se ele pedir, adicione `~/.local/bin` ao PATH |

Todo erro mostra a causa provável e o que fazer. O detalhe técnico fica em `data/last-error.log`, com os segredos
ocultados. Use `check --debug ...` para vê-lo na tela.

---

## Licença

[MIT](LICENSE). Use, modifique e distribua à vontade, mantendo o aviso de autoria.
