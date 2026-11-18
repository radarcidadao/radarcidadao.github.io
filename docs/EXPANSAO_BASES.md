# Avaliação Técnica para Expansão de Bases Públicas

Este documento avalia a viabilidade de integração futura de novas bases oficiais no **Radar Cidadão**, sem publicar filtros vazios ou dados fictícios antes que haja fonte verificada e cobertura consistente.

---

## 1. Contratos e Licitações
- **Fontes oficiais:**
  - **PNCP (Portal Nacional de Contratações Públicas)** — API aberta REST (`pncp.gov.br/api/consulta/v1/`) filtrável por CNPJ do órgão municipal (Prefeitura e Câmara) e ano;
  - **TCE-SP (Audesp / Painel de Contratos)** e **Portais da Transparência municipais** (ex.: São Paulo `transparencia.prefeitura.sp.gov.br` / API e-NegóciosCidadeSP; SJC `transparencia.sjc.sp.gov.br`).
- **Campos-alvo:** Fornecedor/CNPJ, objeto, secretaria/órgão, modalidade, valor contratado, vigência e termos aditivos.
- **Chave de relacionamento:** CNPJ do órgão contratante + número do processo administrativo/licitação + CPF/CNPJ do fornecedor.
- **Limitações atuais:** Nem todas as Prefeituras mantêm API aberta padronizada para aditivos e anexos em formato estruturado; o PNCP cobre contratações sob a Lei nº 14.133/2021 com maior consistência a partir de 2024.

## 2. Obras Públicas
- **Fontes oficiais:**
  - **TCE-SP — Sistema Audesp / Painel de Obras Paralisadas**;
  - **SICONV / Transferegov** (obras com repasse federal);
  - **Portais municipais de Obras** (ex.: GeoSampa / Portal da Transparência de SP).
- **Campos-alvo:** Localização (endereço/bairro/coordenadas quando fornecidos pela fonte), empresa contratada, prazo contratual, situação, avanço físico (%) e valores contratado e pago.
- **Chave de relacionamento:** Código da obra no TCE-SP/Transferegov ou número do contrato vinculado no PNCP/Portal da Transparência.
- **Limitações atuais:** A maioria dos municípios do Vale do Paraíba não publica percentual de avanço físico ou geolocalização estruturada em API aberta, o que impede o cruzamento automático confiável com logradouros de Indicações legislativas sem suposição.

## 3. Despesas Orçamentárias (Empenhado, Liquidado e Pago)
- **Fontes oficiais:**
  - **TCE-SP — Portal da Transparência Municipal (Despesas)** e **SICONFI (Tesouro Nacional)**;
  - **APIs municipais de execução orçamentária** (SP Capital via Portal da Transparência / Dados Abertos).
- **Campos-alvo:** Favorecido (nome e CPF/CNPJ mascarado ou público conforme LGPD/LAI), finalidade/elemento de despesa, órgão/secretaria, período e valores empenhados, liquidados e pagos.
- **Chave de relacionamento:** CNPJ do favorecido + número do empenho + código do órgão/unidade gestora.
- **Limitações atuais:** Padronização heterogênea entre sistemas contábeis municipais e atualização mensal/bimestral no TCE-SP.

## 4. Emendas Parlamentares / Impositivas
- **Fontes oficiais:**
  - **Sistemas SPL das Câmaras** (anexos PDF da LOA/LDO e aba `Emendas Impositivas` já capturada em Taubaté quando disponível);
  - **Portais de Execução Orçamentária das Prefeituras** (quadro de execução de emendas individuais).
- **Campos-alvo:** Parlamentar autor, beneficiário/entidade, finalidade, valor alocado e estágio de execução (empenhado/liquidado/pago).
- **Chave de relacionamento:** Nome/ID do parlamentar + número da emenda na LOA + CNPJ da entidade beneficiária ou dotação orçamentária.
- **Limitações atuais:** Na maioria das Câmaras monitoradas, as emendas impositivas são publicadas apenas em PDFs não tabulares sem identificador único ligado ao pagamento pela Prefeitura.

## 5. Doações e Fornecedores Eleitorais (TSE Detalhado)
- **Fontes oficiais:**
  - **Portal de Dados Abertos do TSE** (`cdn.tse.jus.br/estatistica/sead/odsele/`): arquivos `receitas_candidatos_{ANO}_SP.csv` e `despesas_contratadas_candidatos_{ANO}_SP.csv` / `despesas_pagas_candidatos_{ANO}_SP.csv` (disponíveis de 2018 a 2026).
- **Campos-alvo:** Nome do doador ou fornecedor, CPF/CNPJ público permitido pelo TSE, categoria/origem do recurso (Fundo Eleitoral, Fundo Partidário, Recursos Próprios, Pessoa Física) e valores contratado vs. pago.
- **Chave de relacionamento:** `SQ_CANDIDATO` + `NR_CPF_CANDIDATO` do candidato em cada eleição.
- **Regra de integridade:** A coincidência de nomes ou eventuais relações financeiras entre fornecedores de campanha e contratados públicos **nunca** deve ser sinalizada automaticamente como irregularidade pela interface.

---

## Regras Obrigatórias Antes de Qualquer Nova Integração
1. Identificar a fonte oficial verificável, formato, cobertura temporal e periodicidade de atualização.
2. Documentar exatamente as chaves de cruzamento e limitações conhecidas.
3. Nunca publicar dados fictícios em produção nem filtros vazios com aparência de funcionalidade ativa.
4. Manter separação absoluta entre dinheiro de campanha eleitoral, subsídio/salário de agente público, verba de gabinete e orçamento da Prefeitura.
