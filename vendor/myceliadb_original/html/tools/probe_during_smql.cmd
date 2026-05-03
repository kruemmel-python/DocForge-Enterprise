@echo off
cd /d C:\web_sicherheit\SMQL-Embedding-Adapter
C:\web_sicherheit\SMQL-Embedding-Adapter\.venv\Scripts\python.exe -m smql_embedding_adapter.cli --mycelia-url http://127.0.0.1:9999 --mycelia-token-file C:\web_sicherheit\html\keys\local_transport.token --lmstudio-url http://127.0.0.1:1234 --embedding-model text-embedding-nomic-embed-text-v2-moe --collection demo --search-backend mycelia smql "FIND ASSOCIATED WITH TEXT 'Was ist SMQL?' LIMIT 3"
