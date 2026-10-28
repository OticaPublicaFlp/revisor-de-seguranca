### Regras do projeto (checklist de revisão)

Cada regra tem um ID. Cite o ID no campo `regra` do achado. Aplique só as regras que cabem nos arquivos
recebidos; para as demais, nada a fazer. Em caso de conflito entre duas regras, vale a mais restritiva.

#### EF — Edge Functions (`supabase/functions/<nome>/index.ts`, `_shared/`)
- **EF-1 Chamador interno/cron.** Funções chamadas por cron ou por outras funções aceitam só a service role ou um segredo próprio enviado em header (ex.: `x-cron-secret`), comparado com um valor guardado no servidor. A chave anônima (pública, está no front-end) nunca autoriza nada.
- **EF-2 `verify_jwt = false`.** Em `supabase/config.toml`, uma função com `verify_jwt = false` fica sem checagem de JWT na borda e precisa validar o chamador no próprio código (usuário logado + papel, ou segredo interno). Se não houver validação, é achado crítico. Função nova precisa do bloco `[functions.<nome>]` no `config.toml`.
- **EF-3 Papel no servidor.** Ações administrativas e escritas conferem o papel do usuário no servidor, lendo-o do banco a partir do usuário autenticado, nunca de um campo enviado pelo cliente.
- **EF-4 Conta desativada.** Função que autentica o usuário e depois usa a service role confere se a conta continua ativa (perfil ativo, não banida).
- **EF-5 Senhas e login.** Senha nunca é lida, logada nem devolvida; troca e redefinição só pela API de Auth.
- **EF-6 Segredos.** Chaves só por `Deno.env.get(...)`; nunca no código, em log ou na resposta. Credenciais de integrações guardadas no banco ficam cifradas e só o servidor as lê.
- **EF-7 Dado pessoal em log.** `console.*` e tabelas de métrica não levam nome, documento, telefone, e-mail ou dados de saúde.
- **EF-8 Entrada e CORS.** Validar método HTTP, `req.json()` dentro de `try`, campos obrigatórios, tipos e tamanhos; erro do cliente devolve 4xx. O `OPTIONS` responde antes de qualquer lógica; `Access-Control-Allow-Origin` não é `*` em função que usa credenciais.
- **EF-9 SSRF.** URL informada pelo usuário só é acessada depois de validada: só https, sem localhost, IP privado, endereço de metadados da nuvem nem redirect automático.
- **EF-10 Erros.** O erro vira JSON com status correto e mensagem sem stack trace nem segredo.
- **EF-11 Lote e atomicidade.** Operações de cron são idempotentes (reexecutar não duplica envios nem cobranças). Escritas que precisam ser atômicas vão numa função SQL (RPC), não em várias chamadas soltas.
- **EF-12 Chamadas externas.** Toda chamada externa tem timeout; imports com versão fixa.

#### DB — Consultas e dados
- **DB-1 Paginação.** Consulta que pode passar do limite da API (no Supabase, 1000 linhas por padrão) busca todas as páginas e ordena por uma chave única no fim (ex.: `created_at, id`). Sem isso, o resultado sai incompleto sem aviso.
- **DB-2 Service role e cliente.** A service role ignora a RLS: toda consulta feita com ela filtra pelo cliente (`organization_id`) derivado do usuário autenticado, nunca do corpo da requisição.
- **DB-3 Busca.** Busca por texto exige um tamanho mínimo, escapa `%` e `_` no `LIKE` e tem limite de linhas; termo vazio nunca devolve a tabela inteira.
- **DB-4 Fonte única.** Uma regra de negócio vive num lugar só; a mesma regra reimplementada em outro arquivo tende a divergir.

#### SQL — Migrations, RLS e funções (`supabase/migrations/`)
- **SQL-1 Acesso anônimo e GRANTs.** Toda tabela com dados tem RLS ligada (`ENABLE ROW LEVEL SECURITY`). `anon` não recebe acesso a tabelas, views ou funções de negócio; policy nunca `TO public`/`TO anon` em dado privado. Função nova recebe `GRANT EXECUTE` explícito só para quem precisa. TRUNCATE ignora a RLS: se `anon` ou `authenticated` o têm numa tabela com dados (inclusive pelos privilégios padrão do Supabase, quando nenhuma migration os revoga), qualquer pessoa com a chave pública apaga a tabela inteira, o que é `critical`.
- **SQL-2 SECURITY DEFINER.** Função `SECURITY DEFINER` ignora a RLS: fixa `search_path`, confere o papel antes de escrever e filtra pelo cliente do usuário autenticado (`auth.uid()`), nunca por um id recebido como parâmetro.
- **SQL-3 Policies de escrita.** Policies de `UPDATE`/`INSERT` têm `WITH CHECK` que impede trocar o próprio papel, o próprio cliente ou o dono do registro. Usuário que edita o próprio perfil não pode virar admin.
- **SQL-4 Views.** View sobre tabela com RLS usa `security_invoker = true`; sem isso, a view roda com os privilégios do dono e ignora a RLS.
- **SQL-5 Histórico e auditoria.** Tabelas de histórico/auditoria aceitam só INSERT do app; ninguém do app altera nem apaga.
- **SQL-6 Migração destrutiva.** `DROP`, `TRUNCATE`, `DELETE`/`UPDATE` sem `WHERE` ou mudança de tipo com perda de dados exigem justificativa visível. Migration e teste não levam dado real de pessoas.
- **SQL-7 Storage.** Bucket público só para conteúdo realmente público; upload limita tipo e tamanho no próprio bucket (SVG e HTML podem carregar script); policies de storage filtram pelo dono ou pelo cliente.
- **SQL-8 Desempenho de RLS.** Policies chamam funções via `(SELECT ...)` (ex.: `(SELECT auth.uid())`) para não reavaliar por linha; colunas filtradas com frequência têm índice.

#### FE — Front-end (`src/`)
- **FE-1 Permissões.** Esconder um botão não é proteção: toda regra de acesso precisa existir também no banco (RLS) ou na função do servidor.
- **FE-2 Chave de serviço.** A chave `service_role` nunca vai para o front-end (nem em variável `VITE_*`/`NEXT_PUBLIC_*`); no navegador, só a chave anônima.
- **FE-3 Sessão.** Logout limpa a sessão local; erro de rede não deve deslogar o usuário nem apagar trabalho não salvo.
- **FE-4 Redirecionamento.** Parâmetros de URL usados para redirecionar (`?next=`, `?redirect=`) só aceitam caminhos internos.
- **FE-5 Dado sensível na URL.** Documento, e-mail ou token não vão na query string (ficam em histórico, logs e referer).
- **FE-7 Armazenamento do navegador.** `localStorage`/`sessionStorage` só com preferências; nada de dado pessoal, token de terceiros ou credencial.
- **FE-9 React.** Reporte só bugs reais: efeito sem limpeza de listener/timer, requisição disparada a cada render, `key` instável em lista.
- **FE-10 HTML injetado.** `dangerouslySetInnerHTML`/`innerHTML` só com conteúdo fixo ou sanitizado; dado vindo de usuário ou integração renderizado como HTML é XSS.

#### Fora do escopo (não reporte)
Formatação, estilo, nomes, preferências pessoais, TODOs antigos e riscos hipotéticos sem evidência no código. Em `<diff>`, também o código que o diff não tocou e problemas já existentes que a mudança não agrava. Em `<codigo>`, migrations antigas já corrigidas por migrations posteriores. Em testes e docs, examine se escondem um bug, não o estilo.
