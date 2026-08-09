-- Cria os bancos de dados separados para n8n e Evolution API
-- Executado automaticamente na primeira inicialização do PostgreSQL

SELECT 'CREATE DATABASE n8n'
  WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'n8n')\gexec

SELECT 'CREATE DATABASE evolution'
  WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'evolution')\gexec
