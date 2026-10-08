# docintel

Conversor local e privado de PDF → Markdown para Obsidian. Sem API externa,
sem custo por página, roda inteiramente no seu servidor.

## Subir o serviço

```bash
cd docintel
docker compose up -d --build
```

Isso constrói a imagem (instala Python, FastAPI, Pillow e o Tesseract com
português/inglês) e sobe o container `docintel` escutando na porta `8097`.

Acesse: `http://<endereço-do-seu-servidor>:8097`

Não precisa de mais nada — o `docker-compose.yml` já cria as pastas
`uploads/`, `output/` e `data/` como volumes na primeira execução.

Para executar o conversor diretamente no macOS, fora do container, instale o
Tesseract e os dados de idioma português/inglês com `brew install tesseract
tesseract-lang`. O conversor interrompe a execução com erro explícito se um PDF
precisar de OCR e o Tesseract não estiver disponível.

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
    ├── original.pdf     ← PDF enviado, preservado para o ZIP
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

# 4. Poder Legislativo
#### 4.6 Imunidades dos congressistas

## Poder Legislativo (continuação)
...
![[aula-05-constitucional-0001-07.png]]
```

**O campo `assunto`** é preenchido por uma heurística: se o PDF começa com um
bloco de linhas no formato de sumário numerado (`4.`, `4.6`, `5-`, `5.1.`...,
comum nos slides de "assuntos de hoje"), essas linhas viram texto simples no
frontmatter e são espelhadas no corpo como headings Markdown. O título
principal usa `#`, os filhos `####` e os netos `#####`; títulos em negrito
não são convertidos em wikilinks. Se o PDF não tiver esse padrão no início,
o campo sai vazio — sem tentar adivinhar.

**As imagens** são embutidas no formato wikilink do Obsidian (`![[nome.png]]`),
igual ao padrão nativo de colar imagem no Obsidian — não precisa de caminho
relativo, o Obsidian resolve pelo vault inteiro. Em todos os PDFs de aula,
texto, tabelas nativas e figuras são ordenados pelas coordenadas da página.
Tiras adjacentes de mesma largura são empilhadas numa figura quando a distância
entre bordas é de até 3 unidades da página. Os arquivos seguem o padrão
`<slug>-img<N>-pg<P>.png`, com índice global e número físico da página.

Cada figura continua sendo a fonte visual principal. Como o projeto não usa
um modelo de visão, o Tesseract gera apenas texto aproximado dentro de um
callout recolhido (`> [!note]- Texto OCR (aproximado)`), para busca; esse OCR
não é inserido como texto normal do enunciado. A ordem do Markdown intercala
headings, texto nativo e figuras conforme sua posição no PDF.

**Títulos e subtítulos** viram headings Markdown a partir de nível 4
(`####`–`######`), para não interferir nos níveis superiores do outline do
Obsidian. A hierarquia é inferida por numeração, estilo tipográfico e posição;
negrito parcial permanece ênfase no corpo. O sumário mantém sua posição no
fluxo do PDF, e seus itens reconhecidos também são registrados em `assunto`
no front matter.

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
no corpo do texto, além de wikilinks conceituais automáticos. Decidir o que merece destaque no meio
de um parágrafo de doutrina, ou qual conceito no corpo do texto merece
virar link pra outra nota, depende de entender o conteúdo jurídico -- não
só de reconhecer um padrão de posição/pontuação como o índice ou um "Obs.:"
no início da linha. Uma heurística aqui erraria categoria com frequência,
o que é pior que deixar em branco pra você revisar manualmente.

## Como funciona

- **Extração posicional**: PyMuPDF fornece as linhas de texto, tabelas e
  imagens com suas coordenadas. Tabelas nativas são escritas em Markdown;
  quadros visuais e clusters vetoriais preenchidos são renderizados como
  figuras. Para evitar transformar o desenho da página inteira em figura, os
  clusters vetoriais consideram apenas preenchimentos coloridos e molduras
  grandes menores que a página. Linhas OCR/nativas sobrepostas a uma figura
  não são repetidas no corpo.
- **OCR seletivo**: só páginas com menos de 30 caracteres nativos recebem OCR
  de página inteira (`por+eng`, 300 DPI). O OCR usa a mesma ordem geométrica;
  o texto reconhecido dentro de figuras fica em callout recolhido, não como
  texto principal. Em páginas nativas, o OCR pode ser aplicado ao recorte da
  figura para busca e para a opção de corte de marca d'água.
- `pymupdf4llm.use_layout(False)` permanece ativo para o pipeline de
  legislação, evitando que seu classificador ONNX substitua texto nativo.
- **Símbolos lógicos**: variantes ASCII/OCR são normalizadas para Unicode
  somente em contexto de fórmula (por exemplo, `P v Q` → `P ∨ Q`, `P > Q` →
  `P ⇒ Q`, `P = Q` → `P ⇔ Q`). Letras `v`, `A` e símbolos semelhantes na
  prosa não são substituídos fora desse contexto.
- **Rodapés**: a faixa inferior (7% da página) é ignorada pela posição;
  URLs/domínios reconhecidos são removidos inclusive quando têm espaços ou
  erros comuns de OCR. Links legítimos no corpo são preservados.
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
tem um botão "baixar .zip" que empacota o `.md`, as imagens e o PDF original
num único arquivo. O seletor acima da fila permite escolher a compressão do
PDF: **Menor**, **Média** (padrão) ou **Maior**. Em navegadores baseados em
Chromium (Chrome, Edge), isso
abre o diálogo nativo de "salvar como", deixando você escolher a pasta; em
outros navegadores, cai no download padrão configurado no próprio navegador
— isso é uma limitação da web, não tem como um site forçar esse diálogo em
todo navegador.

As imagens dentro do zip (e também na pasta `output/`) usam o nome do PDF
enviado. Para páginas visuais, o padrão é
`<nome-do-pdf>-pg<página>-fig<N>.png`; no pipeline textual legado, as imagens
continuam usando `<nome-do-pdf>-img<N>-pg<página>.png`.

## Ajustes

- `DOCINTEL_WORKERS` (docker-compose.yml): quantas conversões rodar em
  paralelo. OCR usa 1 core inteiro por página em picos — comece em 2 e suba
  aos poucos observando o uso de CPU do servidor.
- O recorte de marca d'água (`--crop-watermark`) é opcional e fica desligado
  por padrão. Para uma execução manual do worker, acrescente a flag ao final:
  `python worker_convert.py <pdf> <diretorio-de-saida> <meta.json> <resultado.json> --crop-watermark`.
  A interface web não ativa esse recorte.
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
