O AppExemplo é um SaaS de agendamento para clínicas. Várias clínicas (clientes) usam o mesmo banco, separadas pela
coluna `organization_id`. Papéis: admin (gerencia a clínica e os usuários), membro (agenda e atende) e leitor (só
consulta). Há uma página pública de agendamento, Edge Functions chamadas por cron (lembretes por e-mail) e uma
integração com um provedor de pagamentos.

Dados sensíveis: os dados pessoais dos pacientes das clínicas (nome, documento, telefone, histórico de consultas)
e as credenciais das integrações. Stack: React + TypeScript no front; Supabase (Postgres, RLS, Auth, Storage) e
Edge Functions Deno no back.

Para o olhar de atacante: "outro cliente" = outra clínica (outro `organization_id`); o papel mais baixo é o leitor.
