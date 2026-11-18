# 📡 Radar Cidadão — Transparência Legislativa e Eleitoral (Vale do Paraíba + São Paulo)

Rede independente, apartidária e 100% automatizada de fiscalização das Câmaras Municipais e Prefeituras do **Vale do Paraíba** e da **Capital Paulista (Mandato 2025–2028)**, construída sobre a mesma arquitetura estática e determinística do projeto **Radar Taubaté**.

---

## 🌎 Cidades Monitoradas (Mandato 2025–2028)

| Cidade | Cadeiras | Prefeito(a) (2025–2028) | Rota do Portal | Fonte Oficial de Dados Legislativos |
| :--- | :---: | :--- | :--- | :--- |
| **São Paulo (Capital)** | 55 | Ricardo Nunes (MDB) | `/sp/` | APIs Abertas SPLegis & SIP CMSP (`saopaulo.sp.leg.br`) |
| **São José dos Campos** | 21 | Anderson Farias (PSD) | `/sjc/` | Sistema Câmara Sem Papel / SPL (`camarasjc.sp.gov.br`) |
| **Taubaté** | 19 | Sérgio Victor (NOVO) | `/taubate/` | Sistema Câmara Sem Papel / SPL (`camarataubate.sp.gov.br`) |
| **Caraguatatuba** | 15 | Mateus Silva (PSD) | `/caraguatatuba/` | Portal da Câmara (`caraguatatuba.sp.leg.br`) + `leisonline.org` |
| **Jacareí** | 13 | Celso Florêncio (PL) | `/jacarei/` | Transparência e Atas Eletrônicas (`jacarei.sp.leg.br`) |
| **Pindamonhangaba** | 11 | Ricardo Piorino (PL) | `/pindamonhangaba/` | TSE (2012–2026) + SAPL (`pindamonhangaba.sp.leg.br`) |
| **Guaratinguetá** | 11 | Junior Filippo (PSD) | `/guaratingueta/` | TSE (2012–2026) + Câmara (`camaraguaratingueta.sp.gov.br`) |
| **Caçapava** | 10 | Yan Lopes (PODE) | `/cacapava/` | Sistema Câmara Sem Papel / SPL (`camaracacapava.sp.gov.br`) |

Todos os portais cruzam ainda as bases oficiais do **Portal de Dados Abertos do TSE (`cdn.tse.jus.br`)** das eleições de **2012, 2014, 2016, 2018, 2020, 2022, 2024 e 2026**, exibindo receitas, despesas contratadas, custo por voto e histórico de candidaturas a outros cargos (incluindo vereadores que saíram candidatos a Deputado Estadual ou Federal em 2026 no meio do mandato municipal).

---

## ⚡ Funcionalidades em Cada Portal

1. **Barra de 3 Modos no Topo (`#modo-bar`):**
   - **⚡ Leitura Rápida (Manchetes):** resumo em 10 segundos com as 3 manchetes da Câmara, frase-resumo por vereador, alerta de candidaturas a outros cargos e 3 indicadores gigantes.
   - **📊 Versão Completa (Todos os números):** abre automaticamente todas as fichas com anel de presença, barra das 5 categorias, votações nominais, alinhamento com o Prefeito, principais parceiros de coautoria e histórico TSE.
   - **🎓 Guia Cidadão (Como cobrar):** funções reais do vereador, *Mitos × Verdades*, **✉️ Gerador Interativo de Cobrança Cidadã** (WhatsApp, e-mail oficial e link da Câmara) e guia de participação nas sessões, Tribuna Livre, Audiências Públicas e Ouvidoria.
2. **Contador de Salários ao Vivo (`#tick`):** cálculo em tempo real dos subsídios brutos pagos aos vereadores desde a posse (`01/01/2025`), citando a lei ou resolução municipal correspondente.
3. **Busca por Bairro ou Rua (`#bq`):** com filtros combináveis de **Período**, **Situação** e **Assunto** (*Iluminação, Asfalto, Trânsito, Água/esgoto, Limpeza/poda, Calçadas, Praças, Saúde, Escolas, Segurança, Ônibus*).
4. **“O problema que não some”:** detecção automática de logradouros com 3 ou mais pedidos para o mesmo problema, com modal para cobrar os vereadores autores.
5. **“Homenagem ou trabalho?”:** classificação determinística em 5 grupos de cores fixas (`b` Melhorias nos bairros, `c` Perguntas e cobranças, `h` Homenagens, `l` Propostas de lei, `o` Outros).
6. **“E o Prefeito?” (`#pref-box`):** campanha de 2024 no TSE, projetos enviados pelo Executivo, placar de vetos, cobranças sem resposta e termômetro de alinhamento de cada vereador com o governo.
7. **“Onde a Câmara rachou” (`#rachas`) & “Compare dois vereadores” (`#comp-box`):** votações nominais divididas e matriz de concordância par-a-par em plenário.

---

## 🛠️ Como Atualizar Localmente ou via GitHub Actions

```bash
# Atualizar todas as 8 cidades e o Hub central:
python3 coletar.py

# Atualizar apenas uma cidade específica:
python3 coletar.py --cidade sjc
```

A atualização diária automatizada está configurada em `.github/workflows/atualizar.yml` (executa todos os dias às `04:15` BRT / `07:15` UTC com isolamento de falhas por cidade).
