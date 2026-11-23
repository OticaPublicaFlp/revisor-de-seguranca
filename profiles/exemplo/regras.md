### Regras do projeto (checklist de revisão)

Cada regra tem um ID. Cite o ID no campo `regra` do achado. Entre parênteses, a referência de mercado
(OWASP API Security Top 10 2023, OWASP ASVS 5.0, CWE ou Advisors do Supabase), útil para o campo `cwe`. Aplique só as regras que cabem nos arquivos
recebidos; para as demais, nada a fazer. Em caso de conflito entre duas regras, vale a mais restritiva.

#### EF — Edge Functions (`supabase/functions/<nome>/index.ts`, `_shared/`)
- **EF-1 Chamador interno/cron.** Funções chamadas por cron ou por outras funções aceitam só a service role ou um segredo próprio enviado em header (ex.: `x-cron-secret`), comparado com um valor guardado no servidor. A chave anônima (pública, está no front-end) nunca autoriza nada. (API5 · CWE-306)
- **EF-2 `verify_jwt = false`.** Em `supabase/config.toml`, uma função com `verify_jwt = false` fica sem checagem de JWT na borda e precisa validar o chamador no próprio código (usuário logado + papel, ou segredo interno). Se não houver validação, é achado crítico. Função nova precisa do bloco `[functions.<nome>]` no `config.toml`. (API2 · CWE-306)
- **EF-3 Papel no servidor.** Ações administrativas e escritas conferem o papel do usuário no servidor, lendo-o do banco a partir do usuário autenticado, nunca de um campo enviado pelo cliente. (API5 · ASVS 8.3.1 · CWE-862)
- **EF-4 Conta desativada.** Função que autentica o usuário e depois usa a service role confere se a conta continua ativa (perfil ativo, não banida). Desativar uma conta precisa encerrar as sessões dela (banir no Auth ou revogar os tokens): só marcar o perfil como inativo deixa o token valendo até expirar. (ASVS 7.4.2 · CWE-613)
- **EF-5 Senhas e login.** Senha nunca é lida, logada nem devolvida; troca e redefinição só pela API de Auth.
- **EF-6 Segredos.** Chaves só por `Deno.env.get(...)`; nunca no código, em log ou na resposta. Credenciais de integrações guardadas no banco ficam cifradas e só o servidor as lê. (ASVS 13.3.1 · CWE-798)
- **EF-7 Dado pessoal em log.** `console.*` e tabelas de métrica não levam nome, documento, telefone, e-mail ou dados de saúde. (ASVS 16.2.5 · CWE-532)
- **EF-8 Entrada e CORS.** Validar método HTTP, `req.json()` dentro de `try`, campos obrigatórios, tipos e tamanhos; erro do cliente devolve 4xx. O `OPTIONS` responde antes de qualquer lógica; `Access-Control-Allow-Origin` não é `*` em função que usa credenciais. (API8 · CWE-20)
- **EF-9 SSRF.** URL informada pelo usuário só é acessada depois de validada: só https, sem localhost, IP privado, endereço de metadados da nuvem nem redirect automático. (API7 · ASVS 1.3.6, 15.3.2 · CWE-918)
- **EF-10 Erros.** O erro vira JSON com status correto e mensagem sem stack trace nem segredo. (API8 · ASVS 16.5.1 · CWE-209)
- **EF-11 Lote e atomicidade.** Operações de cron são idempotentes (reexecutar não duplica envios nem cobranças). Escritas que precisam ser atômicas vão numa função SQL (RPC), não em várias chamadas soltas.
- **EF-12 Chamadas externas.** Toda chamada externa tem timeout; imports com versão fixa. A resposta de uma API de terceiros é dado não confiável: validar antes de gravar ou mostrar. (API10 · CWE-20)
- **EF-13 Tokens.** O usuário vem do JWT validado pela API de Auth (`auth.getUser()` ou `auth.getClaims()`); nunca decodificar o token com `atob`/`JSON.parse` e confiar no conteúdo sem verificar assinatura e expiração. (API2 · ASVS 9.1.1, 9.2.1 · CWE-347)
- **EF-14 Custo e fluxos em massa.** Ação que gera custo ou alcança muita gente (WhatsApp, e-mail, SMS, IA, exportação completa, convites) tem limite por usuário e por cliente, e só os papéis autorizados a disparam. (API4, API6 · CWE-770)

#### DB — Consultas e dados
- **DB-1 Paginação.** Consulta que pode passar do limite da API (no Supabase, 1000 linhas por padrão) busca todas as páginas e ordena por uma chave única no fim (ex.: `created_at, id`). Sem isso, o resultado sai incompleto sem aviso.
- **DB-2 Service role e cliente.** A service role ignora a RLS: toda consulta feita com ela filtra pelo cliente (`organization_id`) derivado do usuário autenticado, nunca do corpo da requisição. (API1 · ASVS 8.4.1 · CWE-639)
- **DB-3 Busca.** Busca por texto exige um tamanho mínimo, escapa `%` e `_` no `LIKE` e tem limite de linhas; termo vazio nunca devolve a tabela inteira. (API4 · CWE-770)
- **DB-4 Fonte única.** Uma regra de negócio vive num lugar só; a mesma regra reimplementada em outro arquivo tende a divergir.
- **DB-5 Campos expostos e atribuição em massa.** A resposta leva só as colunas necessárias: `select('*')` ou RPC que devolve a linha inteira não pode entregar ao cliente colunas secretas (chave de integração, token, hash). Escrita não repassa o corpo inteiro (`.update(body)`): use uma lista de campos permitidos para que o cliente não envie `role`, `organization_id` ou `status`. Revogar o privilégio de uma coluna só funciona depois de revogar o da tabela. (API3 · ASVS 8.2.3, 15.3.1, 15.3.3 · CWE-213, CWE-915)

#### SQL — Migrations, RLS e funções (`supabase/migrations/`)
- **SQL-1 Acesso anônimo, RLS e GRANTs.** Toda tabela de `public` tem RLS ligada (`ENABLE ROW LEVEL SECURITY`). O Supabase concede por padrão SELECT, INSERT, UPDATE, DELETE e TRUNCATE a `anon` e `authenticated` em tabela nova; adicionar policies não remove esses GRANTs. Por isso as migrations revogam tudo de `anon` (por tabela, em massa ou por `ALTER DEFAULT PRIVILEGES`) e concedem a `authenticated` só o necessário. TRUNCATE ignora a RLS: se `anon` ou `authenticated` o têm numa tabela com dados, qualquer pessoa com a chave pública apaga a tabela inteira, o que é `critical`. Policy nunca `TO public`/`TO anon` em dado privado. (API1, API8 · ASVS 8.2.2 · CWE-284; advisor rls_disabled_in_public)
- **SQL-2 SECURITY DEFINER.** Função `SECURITY DEFINER` ignora a RLS: fixa `search_path` (de preferência `''`, com nomes qualificados), confere o papel antes de escrever e filtra pelo cliente do usuário autenticado (`auth.uid()`), nunca por um id recebido como parâmetro. O Postgres dá EXECUTE a PUBLIC em função nova: revogue de `public`/`anon` e conceda só a quem precisa. GRANT EXECUTE não limita as linhas devolvidas. (API1, API5 · CWE-639, CWE-862; advisors function_search_path_mutable, anon_security_definer_function_executable)
- **SQL-3 Policies de escrita.** Policies de `UPDATE`/`INSERT` têm `WITH CHECK` que impede trocar o próprio papel, o próprio cliente ou o dono do registro; usuário que edita o próprio perfil não pode virar admin. Prefira uma policy por operação com `TO authenticated` explícito: `FOR ALL` e policy sem `TO` escondem o que cada regra libera. (API3, API5 · CWE-269)
- **SQL-4 Histórico e auditoria.** Tabelas de histórico/auditoria aceitam só INSERT do app; ninguém do app altera nem apaga. (ASVS 16 · CWE-778)
- **SQL-5 Migração destrutiva.** `DROP`, `TRUNCATE`, `DELETE`/`UPDATE` sem `WHERE` ou mudança de tipo com perda de dados exigem justificativa visível. Migration e teste não levam dado real de pessoas.
- **SQL-6 Desempenho de RLS.** Policies chamam funções via `(SELECT ...)` (ex.: `(SELECT auth.uid())`) para não reavaliar por linha; colunas filtradas com frequência têm índice.

Verificações dos Database Advisors do Supabase (os scripts também as procuram):
- **SQL-10 Metadados do usuário.** Policy ou função que decide acesso por `user_metadata` / `raw_user_meta_data` confia num dado que o próprio usuário altera com `auth.updateUser()`. Papel e cliente vêm de `app_metadata` ou de uma tabela própria. (CWE-639; advisor rls_references_user_metadata)
- **SQL-11 Policy sempre verdadeira.** Policy de INSERT/UPDATE/DELETE/ALL com `USING (true)` ou `WITH CHECK (true)` para `public`, `anon` ou `authenticated` libera a escrita para todos. "Só a Edge Function usa" não vale: a service role já ignora a RLS e não precisa de policy. (CWE-284; advisor rls_policy_always_true)
- **SQL-12 Views.** View em schema exposto usa `security_invoker = true`; sem isso, ela roda com os privilégios do dono e ignora a RLS das tabelas. (CWE-284; advisor security_definer_view)
- **SQL-13 Storage.** Bucket público serve qualquer arquivo a quem tiver a URL: só para conteúdo realmente público; documentos privados vão em bucket privado com URL assinada. O próprio bucket limita tipo (`allowed_mime_types`) e tamanho (`file_size_limit`); SVG e HTML carregam script (XSS armazenado). Policies de `storage.objects` filtram por `bucket_id` e pela pasta do dono ou do cliente, e policy de SELECT ampla em bucket público permite listar todos os arquivos. (API8 · ASVS 5.2.1, 5.2.2, 5.3.1 · CWE-434; advisor public_bucket_allows_listing)

#### FE — Front-end (`src/`)
- **FE-1 Permissões.** Esconder um botão não é proteção: toda regra de acesso precisa existir também no banco (RLS) ou na função do servidor. (ASVS 8.3.1 · CWE-602)
- **FE-2 Chave de serviço.** A chave `service_role` nunca vai para o front-end (nem em variável `VITE_*`/`NEXT_PUBLIC_*`); no navegador, só a chave anônima. (CWE-798)
- **FE-3 Sessão.** Logout limpa a sessão local; erro de rede não deve deslogar o usuário nem apagar trabalho não salvo.
- **FE-4 Redirecionamento.** Parâmetros de URL usados para redirecionar (`?next=`, `?redirect=`) só aceitam caminhos internos. (CWE-601)
- **FE-5 Dado sensível na URL.** Documento, e-mail ou token não vão na query string (ficam em histórico, logs e referer). (ASVS 14.2.1 · CWE-598)
- **FE-7 Armazenamento do navegador.** `localStorage`/`sessionStorage` só com preferências; nada de dado pessoal, token de terceiros ou credencial. (ASVS 14.3.3 · CWE-922)
- **FE-9 React.** Reporte só bugs reais: efeito sem limpeza de listener/timer, requisição disparada a cada render, `key` instável em lista.
- **FE-10 HTML injetado.** `dangerouslySetInnerHTML`/`innerHTML` só com conteúdo fixo ou sanitizado; dado vindo de usuário ou integração renderizado como HTML é XSS. (ASVS 1.2.1, 1.3.1 · CWE-79)
- **FE-11 Realtime.** Canal com dado de cliente é privado (`supabase.channel(nome, { config: { private: true } })`) e tem policies em `realtime.messages`; canal público entrega as mensagens a qualquer um que saiba o nome. (API1 · CWE-284)

#### Fora do escopo (não reporte)
Formatação, estilo, nomes, preferências pessoais, TODOs antigos e riscos hipotéticos sem evidência no código. Em `<diff>`, também o código que o diff não tocou e problemas já existentes que a mudança não agrava. Em `<codigo>`, migrations antigas já corrigidas por migrations posteriores. Em testes e docs, examine se escondem um bug, não o estilo.
