**Exemplo 1 — achado crítico**
Diff: nova função `supabase/functions/export-records/index.ts` que cria um cliente com service role, lê `organization_id`
de `await req.json()` e devolve linhas de `customers`; `config.toml` ganha `[functions.export-records] verify_jwt = false`.
Verificação feita: `read_file` do `index.ts` inteiro e `search_repo("getUser", path="supabase/functions/export-records")`
sem nenhuma checagem de usuário, papel ou segredo.
```json
{
  "resumo": "Arquivos: export-records/index.ts (nova) e config.toml. Regras: EF-2, EF-3, DB-2. Li o index.ts inteiro: não há leitura de Authorization nem checagem de papel, e o organization_id vem do corpo. Descartado: CORS (o OPTIONS está correto).",
  "findings": [
    {
      "regra": "EF-2",
      "severity": "critical",
      "file": "supabase/functions/export-records/index.ts",
      "line": 14,
      "title": "Função pública com service role exporta clientes de qualquer clínica",
      "description": "A função tem verify_jwt=false no config.toml e não valida usuário, papel nem segredo interno. Como usa service role (ignora a RLS) e o organization_id vem do corpo da requisição, qualquer pessoa na internet lista os pacientes de qualquer clínica.",
      "suggestion": "Validar o usuário com auth.getUser(token), ler o papel e o organization_id do perfil dele no banco e só então consultar, filtrando por esse organization_id."
    }
  ],
  "verificados": ["supabase/functions/export-records/index.ts — EF-8: OPTIONS responde antes da lógica"]
}
```

**Exemplo 2 — achado de severidade média**
Diff: `supabase/functions/monthly-report/index.ts` passa a fazer `.from('appointments').select('*').gte('starts_at', start)`
e devolve o resultado direto. Não há paginação em nenhum ponto do arquivo (conferido com `read_file`).
```json
{
  "resumo": "Arquivo: monthly-report/index.ts. Regra: DB-1. A consulta é única: a API corta em 1000 linhas sem avisar, então clínicas grandes recebem relatório incompleto.",
  "findings": [
    {
      "regra": "DB-1",
      "severity": "medium",
      "file": "supabase/functions/monthly-report/index.ts",
      "line": 31,
      "title": "Relatório mensal lê só as primeiras 1000 consultas",
      "description": "Sem paginação, uma clínica com mais de 1000 consultas no mês recebe o relatório incompleto, sem erro, e o total não bate com a tela.",
      "suggestion": "Buscar em páginas com .range(), ordenando por 'starts_at, id', até a última página."
    }
  ],
  "verificados": []
}
```

**Exemplo 3 — material correto, nenhum achado**
Diff: um botão de excluir paciente passa a aparecer só para admin, e a policy de `DELETE` em `customers` já exige papel admin.
```json
{
  "resumo": "Arquivos: CustomerRow.tsx e a migration da policy. Regras: FE-1, SQL-3. O botão é só conveniência; a proteção real está na policy de DELETE, que confere o papel no banco. Nada a reportar.",
  "findings": [],
  "verificados": [
    "src/components/CustomerRow.tsx — FE-1: botão escondido para não admin, com proteção real no banco",
    "supabase/migrations/20260101_customers_delete.sql — SQL-3: policy de DELETE exige papel admin"
  ]
}
```
