# docintel

Conversor local e privado de PDF → Markdown para Obsidian. Sem API externa,
sem custo por página, roda inteiramente no seu servidor.

## Subir o serviço

```bash
cd docintel
docker compose up -d --build
```

Isso constrói a imagem (instala Python, FastAPI e o Tesseract com português/
inglês) e sobe o container `docintel` escutando na porta `8097`.

Acesse: `http://<endereço-do-seu-servidor>:8097`

Não precisa de mais nada — o `docker-compose.yml` já cria as pastas
`uploads/`, `output/` e `data/` como volumes na primeira execução.

## Uso

1. Abra a interface web, arraste um ou vários PDFs (ou clique para escolher).
2. Preencha disciplina / professor(a) / aula (nº) / tags — opcional, aplicado
   ao lote inteiro. Se "aula" ficar em branco, o número é detectado
   automaticamente do nome do arquivo (ex: `Aula_05_Constitucional.pdf` → 5).
3. Clique em "Converter". A fila processa em segundo plano — pode fechar a
   aba, o progresso continua no servidor.
4. Quando o status virar "concluído", baixe o `.md` pelo link da tabela ou
   aponte seu vault do Obsidian direto para a pasta `output/`.

## Estrutura da saída

```
output/
└── <slug-do-arquivo>-<hash>/
    ├── <slug>.md        ← frontmatter YAML + conteúdo em Markdown
    └── images/          ← imagens extraídas, já referenciadas no .md
```

Cada `.md` sai seguindo o padrão de nota do vault:

```yaml
---
disciplina: DIREITO CONSTITUCIONAL
tags:
excalidraw-plugin: parsed
data_criacao: 2026-07-31
professora: Nathalia Masson
assunto: |-
  4. Poder Legislativo
  4.6 Imunidades dos congressistas
caminho: Direito Constitucional Aula 5
---

- [[4. Poder Legislativo]]
  - [[4.6 Imunidades dos congressistas]]

## Poder Legislativo (continuação)
...
![[aula-05-constitucional-0001-07.png]]
```

**O campo `assunto`** é preenchido por uma heurística: se o PDF começa com um
bloco de linhas no formato de sumário numerado (`4.`, `4.6`, `5-`, `5.1.`...,
comum nos slides de "assuntos de hoje"), essas linhas viram o `assunto` no
frontmatter (como texto plano, sem `[[wikilink]]` -- o YAML não resolve
link) e são espelhadas logo abaixo como lista aninhada com wikilinks (ver
próximo parágrafo). Se o PDF não tiver esse padrão no início, o campo sai
vazio — sem tentar adivinhar.

**As imagens** são embutidas no formato wikilink do Obsidian (`![[nome.png]]`),
igual ao padrão nativo de colar imagem no Obsidian — não precisa de caminho
relativo, o Obsidian resolve pelo vault inteiro. Cada imagem fica dentro do
bloco da MESMA página do PDF de onde foi extraída (uma checagem de sanidade
roda no fim da conversão e avisa em log, sem falhar o job, se algum
pós-processamento tiver movido uma imagem pra página errada).

**O índice/sumário do início do documento** (título de seção + itens
numerados do "assuntos de hoje", igual aparece nos slides) vira uma lista
aninhada — item pai (seção) envolvendo os itens filhos (subitens
numerados), replicando a hierarquia visual do PDF — com cada item entre
`[[wikilinks]]`, pra virar nota própria no vault se você quiser.

**Avisos didáticos** (linhas que começam com "Obs.", "Observação",
"Atenção", "Cuidado", "Não se esqueça...") viram um callout
`> [!attention] Atenção!` envolvendo o bloco inteiro (gatilho + texto).

**Citação de artigo de lei/CF na íntegra** (blocos que abrem com
`**Art. N, CF/88:**` ou variação, inclusive os incisos/alíneas/parágrafos
que continuam a mesma citação) vira um callout `> [!quote] Texto de lei`,
separado visualmente do comentário da professora ao redor.

**Notas de rodapé de verdade** (número sobrescrito no PDF, ligado a um
texto no rodapé da página) viram a sintaxe nativa do Obsidian (`[^N]` no
corpo, `[^N]: texto` reunidas no fim do arquivo, renumeradas
sequencialmente pro documento inteiro pra não colidir quando o PDF reinicia
a numeração a cada página).

Essas quatro marcações acima (índice, avisos, citação de lei, notas de
rodapé) são heurísticas baseadas em padrão textual/posicional -- não em
entendimento do conteúdo jurídico -- então formatos atípicos podem escapar
ou, mais raramente, disparar num trecho que não deveria (ex: um parágrafo
comum que comece citando "Art. 5º" de passagem). Revise o resultado antes
de dar como definitivo.

**O que continua de fora, de propósito:** `==destaques==` e `#tags` inline
no corpo do texto, e `[[wikilinks]]` fora do índice/sumário (ex: pra outros
conceitos citados no meio do texto). Decidir o que merece destaque no meio
de um parágrafo de doutrina, ou qual conceito no corpo do texto merece
virar link pra outra nota, depende de entender o conteúdo jurídico -- não
só de reconhecer um padrão de posição/pontuação como o índice ou um "Obs.:"
no início da linha. Uma heurística aqui erraria categoria com frequência,
o que é pior que deixar em branco pra você revisar manualmente.

## Como funciona

- **Extração**: PyMuPDF4LLM faz a conversão para Markdown, preservando
  hierarquia de títulos e tabelas.
- **OCR automático, mas controlado por nós.** O pymupdf4llm ≥1.28 vem com
  um classificador automático (ONNX) que decide sozinho quando rodar OCR --
  e ele erra especificamente em páginas que misturam uma imagem grande
  (capa, logo) com bastante texto nativo: classifica a página inteira como
  "escaneada" e o OCR *substitui* o texto nativo real, apagando conteúdo em
  silêncio. Por isso o docintel desativa esse classificador
  (`pymupdf4llm.use_layout(False)`) e faz a própria checagem, simples e
  auditável: só roda OCR manualmente nas páginas onde o PyMuPDF não
  encontra texto nativo nenhum (ou seja, páginas genuinamente escaneadas).
  Páginas com texto digital nunca são tocadas por OCR, então nunca correm
  o risco de ter conteúdo apagado.
- **Progresso real de upload**: a interface mostra o envio byte a byte (não
  é só um spinner) — se a barra não andar, o problema é de rede/conexão; se
  andar até 100% e o job aparecer como "processando", o envio funcionou e
  agora é a conversão em si que está rodando.
- **Cada conversão roda em um processo isolado.** Isso tem dois efeitos:
  um PDF corrompido não derruba o servidor nem os outros jobs; e o botão
  "cancelar" mata de verdade o processo (mesmo no meio de um OCR demorado),
  em vez de só escondê-lo da fila.
- **Fila em segundo plano**: cada upload vira um job, processado por um
  pool de workers (padrão: 2, ajustável via `DOCINTEL_WORKERS` no
  `docker-compose.yml`). A interface mostra o status ao vivo (pendente →
  processando com tempo decorrido → concluído/erro/cancelado) e avisa se um
  job falhar, com a mensagem de erro visível na própria fila.
- **Deduplicação por hash**: reenviar um PDF já convertido reaproveita a
  saída na hora, sem reprocessar.
- **Resiliente a reinício**: se o container cair no meio de um lote, os jobs
  pendentes voltam pra fila automaticamente na próxima subida.
- **Estado em SQLite**: fila e histórico persistem em `data/docintel.db`.

## Baixando o resultado

Além de apontar o Obsidian direto pra pasta `output/`, cada job concluído
tem um botão "baixar .zip" que empacota o `.md` e as imagens da nota num
único arquivo. Em navegadores baseados em Chromium (Chrome, Edge), isso
abre o diálogo nativo de "salvar como", deixando você escolher a pasta; em
outros navegadores, cai no download padrão configurado no próprio navegador
— isso é uma limitação da web, não tem como um site forçar esse diálogo em
todo navegador.

As imagens dentro do zip (e também na pasta `output/`) seguem uma numeração
auditável: `<nome-do-pdf>-img<N>-pg<página>.png` — por exemplo, a 2ª imagem
extraída, que está na página 2, vira `DCA1-img2-pg2.png`. A numeração é
sequencial e global (segue a ordem em que as imagens aparecem no documento),
a página é a página real do PDF de origem.

## Ajustes

- `DOCINTEL_WORKERS` (docker-compose.yml): quantas conversões rodar em
  paralelo. OCR usa 1 core inteiro por página em picos — comece em 2 e suba
  aos poucos observando o uso de CPU do servidor.
- Para processar milhares de apostilas de uma vez, é só selecionar todas no
  seletor de arquivos (ou arrastar a pasta inteira) — a fila absorve o lote
  e processa uma a uma sem bloquear a interface.

## Escopo

Este serviço faz uma coisa: PDF → Markdown pronto para Obsidian. Não inclui
modos para RAG, geração de flashcards ou Docling — se algum dia isso vier a
ser necessário, a recomendação é tratar como um serviço separado que consome
o Markdown já limpo daqui, em vez de inflar este container.

## FASE 2 -- Markdown de Legislação

Pipeline aditivo e isolado (não altera nada do pipeline padrão acima):
converte lei/código em vez de nota de aula, reestruturando o texto em
`Art. > caput > incisos > alíneas > parágrafos` usando só sinais de
pontuação como marcador estrutural (não reconstrói espaçamento de palavras
fora dessas quebras).

- Botão **"Markdown de Legislação"**, ao lado do botão "Converter" -- usa
  os mesmos arquivos selecionados no dropzone, mas envia pra
  `/api/convert-legislacao` em vez de `/api/convert`.
- Fila, banco (`legislacao_jobs`) e worker (`worker_convert_legislacao.py`)
  totalmente separados do pipeline padrão -- mesmo padrão de subprocesso
  isolado (cancelamento real, resiliente a restart).
- Reaproveita (só chama, nunca edita) a extração segura de texto de
  `converter.py` -- OCR manual apenas em páginas sem texto nativo, reflow
  de parágrafos, remoção de rodapé -- pra não duplicar essa lógica.

Regras de segmentação:

| Elemento | Sinal estrutural |
|---|---|
| Caput | termina no primeiro `:` |
| Inciso | numeral romano + travessão (`I –`); `;` delimita entre incisos |
| Alínea | `a)`, `b)`... (aceita também o formato itálico `_a)_`) |
| Parágrafo | `§ N º` -- só conta se vier logo após `.` ou `;` (senão é remissão, ex: "nos termos do art. 5º, § 2º") |
| Parágrafo único | `Parágrafo único.` -- mesma regra de posição |

Também limpa dois ruídos comuns em compilações grandes tipo Vade Mecum:
quebra de página no meio de um artigo, e o cabeçalho corrente do livro
repetido a cada página (ex: "152 Código Civil").

Não extrai imagens (o foco é a estrutura do texto). Testado extensivamente
contra o Vade Mecum do Senado Federal real (Constituição, Código Civil,
Código Penal etc.) -- ver `converter_legislacao.py` para os detalhes da
heurística.
