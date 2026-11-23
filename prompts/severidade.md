### Severidade
Decida a severidade por duas perguntas, como na metodologia de risco da OWASP (probabilidade × impacto):
**quem consegue explorar** e **o que obtém**. Considere o estado final do código e os padrões da plataforma.
Por exemplo, um privilégio que o Supabase concede por padrão e nenhuma migration revoga está presente.

| Quem consegue explorar ↓ · O que obtém → | Dados de outro cliente (tenant), segredos, virar admin, apagar em massa | Dados pessoais ou de saúde em massa, escrita indevida, apagar rastros, ação com custo sem limite | Dado pouco sensível, dano limitado ou lento |
|---|---|---|---|
| Qualquer pessoa na internet (sem login, só a chave pública) | `critical` | `critical` | `medium` |
| Qualquer usuário logado, de qualquer cliente | `critical` | `high` | `medium` |
| Papel baixo (ex.: visualizador) dentro do próprio cliente | `high` | `high` | `low` |
| Só admin, só o servidor, ou exige outra falha antes | `medium` | `medium` | `low` |

Regras de calibragem:
- **Não rebaixe por exigir login.** Em um SaaS, qualquer cliente cadastrado ou funcionário já é um atacante possível.
- **Acesso entre clientes é `critical`.** Uma consulta ou RPC que aceita o id do cliente vindo do chamador e devolve
  ou altera dados de outro cliente é IDOR entre inquilinos (OWASP API1, CWE-639).
- **Dado pessoal ou de saúde em massa sobe a severidade.** Exportação, bypass de auditoria ou busca vazia/curta/coringa
  (`%`, `_`, sem limite) que entrega documentos, telefones, endereços ou dados de saúde de muitas pessoas a quem não deveria
  (como um papel restrito/visualizador) é no mínimo `high` (nunca `medium`). A LGPD trata dado de saúde como dado sensível.
- **XSS armazenado, SSRF e roubo de credencial de integração** (bucket público que aceita SVG/HTML sem restrição de
  `allowed_mime_types`, SSRF com retorno de resposta, ou redirecionamento de URL para onde o servidor envia chave) são no mínimo `high` (nunca `medium`).
- **Escalada de privilégio ou troca de cliente via `UPDATE` sem `WITH CHECK`** (policy de `UPDATE` que permite alterar o próprio
  `role`, `organization_id`/`tenant_id` ou `is_active` pelo PostgREST) é `critical`.
- **Privilégio que contorna a RLS** (TRUNCATE, função SECURITY DEFINER sem filtro, view sem `security_invoker`) ao
  alcance de `anon` ou de qualquer usuário logado é `critical`.
- `low`: robustez ou manutenção com efeito pequeno, como timeout ausente em chamada interna, ou JSON inválido
  devolvendo 500 em vez de 400.
- `info`: observação útil e sem risco.
- Se um comentário explica a intenção e a justificativa se sustenta, reduza um nível e diga o que ela deixa de cobrir.

Preencha `cwe` com o CWE principal quando houver um claro. Exemplos: CWE-639 (acesso por id controlado pelo usuário),
CWE-285 (autorização ausente), CWE-862 (checagem de papel ausente), CWE-915 (atribuição em massa), CWE-200
(exposição de dado), CWE-918 (SSRF), CWE-79 (XSS), CWE-89 (injeção SQL), CWE-798 (segredo no código), CWE-532
(dado sensível em log), CWE-770 (consumo sem limite), CWE-613 (sessão que não expira).
