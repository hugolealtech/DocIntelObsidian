"""
Pós-processador de legislação -- FASE 2 do docintel.

Módulo isolado e aditivo: não importa nem altera nada do pipeline padrão
além de REUTILIZAR (só chamar, nunca editar) as funções já existentes em
`converter.py` que fazem a extração segura de texto (OCR manual só em
páginas sem texto nativo, remoção de rodapé, reflow de parágrafos). Isso
evita duplicar aquela lógica e garante que os dois pipelines usam a mesma
extração de base.

O que este módulo faz de diferente do pipeline padrão: em vez de gerar uma
nota de aula, reestrutura o texto de uma lei/código em hierarquia
Art. -> caput -> incisos -> alíneas -> parágrafos, usando só sinais de
pontuação como marcador estrutural (não tenta reconstruir espaçamento de
palavras fora dessas quebras).

Acionado exclusivamente pelo botão "Markdown de Legislação" -- o botão e o
fluxo de conversão padrão continuam 100% independentes.
"""

import os
import re

import pymupdf
import pymupdf4llm

import converter as _base  # reaproveita (só chama) a extração segura já existente

# ---------------------------------------------------------------------------
# Padrões estruturais
# ---------------------------------------------------------------------------

# Número do artigo: 1 a 3 dígitos, com separador de milhar opcional
# ("1.000", "1.010" -- comuns em Código Civil, Código Penal, CLT etc a
# partir do art. ~1000), OU uma sequência de 4+ dígitos corrida, sem
# pontuação, para quando a extração do PDF engoliu o separador de milhar
# (ex: "1337" em vez de "1.337"). Sem isso, "\d+" parava no primeiro "."
# e cortava o número ao meio.
_ARTIGO_NUM_RE = r"\d{1,3}(?:\.\d{3})*|\d{4,}"

# Variantes de travessão usadas para o sufixo de emenda ("-A", "-B"...):
# hífen comum, hífen não separável, traço de número, en-dash e em-dash --
# a extração do PDF não é consistente sobre qual usar.
_TRAVESSAO_RE = r"[\-\u2010\u2011\u2012\u2013\u2014]"

# "**Art. 23**", "**Art. 24-A**", "**Art. 1.010**", às vezes com o "º"
# saindo separado em negrito próprio ("**Art. 5** **o**") -- essa parte é
# tolerada e descartada. O ponto final do artigo pode vir DENTRO do
# negrito ("**Art. 10.**", formato predominante no restante do livro a
# partir de ~art. 10) ou fora dele ("**Art. 10**.") -- os dois são aceitos.
_ARTIGO_RE = re.compile(
    r"\*\*Art\.\s*(" + _ARTIGO_NUM_RE + r")(?:" + _TRAVESSAO_RE + r"([A-Z]))?\s*\.?\s*\*\*"
    r"(?:\s*\.?\s*\*{0,2}\s*[oº]\s*\.?\s*\*{0,2})?"
    r"\s*\.?\s*"
)

# Variante em que a extração do PDF separa "Art." do número em negritos
# distintos, com o número ainda envolto em risco (artefato de ordinal
# sobrescrito), ex: "**Art.** **~~1~~** **o**" -- observado nos artigos de
# número baixo (1 a 9) de praticamente toda lei do livro. Normalizado para
# a forma canônica "**Art. N**" antes da extração propriamente dita.
_ARTIGO_NEGRITO_QUEBRADO_RE = re.compile(
    r"\*\*Art\.\*\*\s*\*\*(?:~~)?(" + _ARTIGO_NUM_RE + r")(?:~~)?\*\*"
    r"\s*\*\*[oº]\*\*\.?\s*"
)

# Variante em que o sufixo de emenda ("-A", "-B"...) sai separado do
# número do artigo, em parágrafo próprio, ex: "**Art. 8**" seguido de
# "**-A.**" ou de "**~~-~~** **A.**". Também normalizada para a forma
# canônica ("**Art. 8-A.**") antes da extração.
_ARTIGO_SUFIXO_QUEBRADO_RE = re.compile(
    r"\*\*Art\.\s*(" + _ARTIGO_NUM_RE + r")\.?\s*\*\*"
    r"\s*\n*\s*"
    r"\*\*(?:~~" + _TRAVESSAO_RE + r"~~\*\*\s*\*\*|" + _TRAVESSAO_RE + r")\s*([A-Z])\.?\s*\*\*"
)

# Marca início de inciso: numeral romano + travessão ("I – ", "II – ").
_INCISO_RE = re.compile(r"\b([IVXLCDM]+)\s*[\u2013\u2014-]\s*")

# Marca início de alínea: letra minúscula + parêntese ("a)", "b)").
# Exige que a letra esteja isolada (não parte de outra palavra) pra não
# confundir com texto comum.
_ALINEA_RE = re.compile(r"(?:^|[\s;,])_?([a-z])\)_?\s*")

# Número do parágrafo, tolerando o "º" saindo como "o" solto e o ruído de
# tachado "~~N~~" (artefato de extração em números sobrescritos).
_PARAGRAFO_NUM_RE = re.compile(r"§\s*(?:~~)?(\d+)(?:~~)?\s*[oº]?\.?\s*")
_PARAGRAFO_UNICO_RE = re.compile(r"_?Par[aá]grafo\s+[uú]nico_?\s*\.\s*")

_PONTUACAO_FECHA = (".", ";", ":")

# Marcador de quebra de página inserido por `converter.process_pages` --
# com grupo de captura no número, usado por `_limpar_e_rotular_paginas`
# pra saber a página de cada trecho.
_PAGE_BREAK_RE = re.compile(r"\n\n---\n\*p\. (\d+)\*\n\n---\n\n")

# Cabeçalho corrente que livros como o Vade Mecum repetem no topo de cada
# página (número da página + nome da lei/código em vigor naquele trecho,
# ex: "152 Código Civil" ou "Constituição da República Federativa do
# Brasil 23") -- só conta como cabeçalho (e é removido) se tiver um número
# de página junto; um título sem número é preservado, por segurança.
_RUNNING_HEADER_RE = re.compile(
    r"^(\d{1,4}\s+)?([A-ZÀ-Ú][A-Za-zÀ-ÿ ]{1,60})(\s+\d{1,4})?(?=\s|$)"
)

# Rodapé típico de um livro de legislação compilada (ex: Vade Mecum): só o
# número da página, sozinho numa linha, sem nenhum domínio/URL na linha
# seguinte -- diferente do rodapé de apostila (`_FOOTER_RE`, em
# converter.py), que exige uma URL logo depois e por isso NUNCA reconhece
# esse formato. Sem remover isso ANTES do reflow de parágrafos (que junta
# linhas soltas do mesmo parágrafo), esse número sobra solto no meio do
# texto de uma página e o reflow cola ele no meio da palavra/frase
# seguinte -- ex: "República Fe8derativa" em vez de "República
# Federativa" (bug real, reproduzido contra o Vade Mecum do Senado
# Federal: o pymupdf4llm às vezes intercala o rodapé de uma página bem no
# meio de uma palavra hifenizada na quebra de linha).
_RODAPE_NUMERO_RE = re.compile(r"\n{2,}(\d{1,4})\n{2,}")

# Cabeçalho de abertura de cada lei, no formato usado por livros de
# legislação compilada tipo Vade Mecum: um heading com o nome da lei,
# seguido (uma linha em branco depois) por um heading com a citação legal
# ("Lei n° ...", "Decreto-lei n° ...", "Emenda Constitucional n° ...",
# "Lei Complementar n° ..."). Normalmente aparece só uma vez, na abertura
# de cada lei do compilado -- mas às vezes (mesma classe de bug do rodapé
# solto -- extração da própria pymupdf4llm) esse par de headings vaza de
# novo, no meio do corpo de um artigo, como se fosse cabeçalho de página
# repetido. Usado tanto pra detectar troca de legislação (documento com
# mais de uma lei) quanto pra limpar essa repetição espúria.
_LEI_TITULO_RE = re.compile(
    r"#{2,6}\s+\*{0,2}([A-ZÀ-Ú][^\n*]{2,80}?)\*{0,2}\s*\n\s*\n"
    r"#{2,6}\s+\*{0,2}(?:Lei\s+Complementar|Lei|Decreto-lei|Emenda\s+Constitucional)\b[^\n]{0,80}\n?"
)


def _remover_rodape_numeros_pagina(md_text: str) -> str:
    """Ver docstring de `_RODAPE_NUMERO_RE`. Roda ANTES de
    `_base.process_pages` fazer o reflow de parágrafos, que é o passo que
    efetivamente cola o número solto no meio da palavra seguinte. Remove
    qualquer número isolado (linha em branco dos dois lados) no texto --
    não tenta validar contra o número de página "esperado" (índice do
    PDF + 1): um livro de legislação compilada como o Vade Mecum tem
    várias páginas de capa/sumário antes do início da numeração impressa,
    então o índice do PDF quase nunca bate com o número impresso no
    rodapé. Aplica direto sobre o texto inteiro (sem separar por página)
    -- o padrão (dígito sozinho, cercado de linha em branco dos dois
    lados) nunca casa com o marcador `--- end of page=N ---` em si (o "N"
    ali vem colado a "page=" e " ---", nunca cercado de linha em branco),
    então não há risco de interferir na quebra de página que
    `_base.process_pages` faz depois.

    Troca por DUAS quebras de linha (não por um espaço) -- na maioria das
    vezes o número de rodapé aparece bem no fim do conteúdo da página,
    logo antes do marcador `--- end of page=N ---` que o pymupdf4llm
    insere; comendo as quebras de linha ali (trocando por um espaço) tira
    do marcador seguinte a quebra de linha que `_base.process_pages`
    precisa pra reconhecê-lo (regride pra um bug pior: o marcador inteiro
    vaza como texto). Trocar por "\\n\\n" evita isso e, pro reflow de
    parágrafos que roda logo depois, dá no mesmo resultado de um espaço
    simples quando o número cai no meio do texto de verdade (o reflow
    ignora linha em branco como sinal de quebra -- só o conteúdo de cada
    linha decide onde um bloco novo começa). Cogitou-se remover sem
    deixar espaço nenhum quando o número cai entre duas letras minúsculas
    (caso de uma palavra hifenizada que a extração já tinha rejuntado
    antes do rodapé se intrometer, ex: "Fe" + rodapé + "derativa" ->
    "Federativa") -- mas medindo contra o Vade Mecum real isso erra bem
    mais vezes do que acerta (a maioria dos casos é só uma quebra de
    linha comum entre DUAS PALAVRAS DIFERENTES, ex: "que" + rodapé +
    "possam", que precisa do espaço pra não virar "quepossam"), então
    fica de fora -- mais seguro mesmo custando o raro caso de uma palavra
    hifenizada sair com um espaço a mais no meio."""
    return _RODAPE_NUMERO_RE.sub("\n\n", md_text)


def _detectar_e_remover_titulo_lei(trecho: str, lei_atual):
    """Ver docstring de `_LEI_TITULO_RE`. Devolve
    (trecho_sem_o_cabecalho, nome_da_lei_atualizado) -- se não achar
    nada, devolve o trecho como está e mantém `lei_atual`."""
    m = _LEI_TITULO_RE.search(trecho)
    if not m:
        return trecho, lei_atual
    nova_lei = m.group(1).strip()
    trecho_limpo = trecho[: m.start()] + " " + trecho[m.end() :]
    return trecho_limpo, nova_lei


def _limpar_e_rotular_paginas(texto: str, titulo_padrao):
    """Remove os marcadores de quebra de página e o cabeçalho corrente do
    livro que aparece logo em seguida -- sem isso, esses artefatos
    interrompem incisos/parágrafos no meio quando um artigo atravessa uma
    quebra de página. Ao mesmo tempo, detecta troca de legislação (ver
    `_LEI_TITULO_RE`) e devolve, junto com o texto limpo, a lista de
    eventos de página na ordem em que apareceram -- cada evento é
    `(quantidade_de_artigos_vistos_até_ali, nome_da_lei, numero_da_pagina)`
    -- usada por `segmentar_legislacao` pra decidir ANTES de qual artigo
    (nunca no meio de um) inserir o marcador "Nome da lei -- pg N": a
    quantidade de artigos já vistos naquele ponto é contada sobre o MESMO
    trecho, já normalizado (`_normalizar_cabecalhos_artigo`), então bate
    com a ordem dos marcadores que `segmentar_legislacao` encontra depois
    na segunda passada (mesmo o texto completo sendo renormalizado de
    novo lá -- normalizar não muda quantos cabeçalhos de artigo existem,
    só reescreve a forma deles)."""
    partes = _PAGE_BREAK_RE.split(texto)
    saida = []
    eventos = []
    lei_atual = titulo_padrao
    artigos_vistos = 0

    for i in range(0, len(partes) - 1, 2):
        trecho = partes[i]
        pagina_num = partes[i + 1]
        if i > 0:
            m = _RUNNING_HEADER_RE.match(trecho)
            if m and (m.group(1) or m.group(3)):
                trecho = trecho[m.end() :]
            trecho = " " + trecho.lstrip()
        trecho, lei_atual = _detectar_e_remover_titulo_lei(trecho, lei_atual)
        artigos_vistos += len(_ARTIGO_RE.findall(_normalizar_cabecalhos_artigo(trecho)))
        saida.append(trecho)
        eventos.append((artigos_vistos, lei_atual, pagina_num))
    if len(partes) % 2 == 1:
        saida.append(partes[-1])

    # só mantém o primeiro evento (topo do documento) e os pontos em que a
    # legislação em vigor realmente muda -- não repete o marcador em toda
    # página, só "no topo do documento (ou de cada nova lei)".
    eventos_relevantes = []
    lei_anterior = object()  # sentinela -- garante que o primeiro evento sempre entra
    for evento in eventos:
        if evento[1] != lei_anterior:
            eventos_relevantes.append(evento)
        lei_anterior = evento[1]

    return "".join(saida), eventos_relevantes


def _normalizar_cabecalhos_artigo(texto: str) -> str:
    """Reescreve as variantes "quebradas" de cabeçalho de artigo (negrito
    partido em pedaços, sufixo de emenda separado em parágrafo próprio)
    para a forma canônica "**Art. N**" / "**Art. N-X**", que é o que
    `_ARTIGO_RE` sabe reconhecer. Roda antes de qualquer outra coisa --
    sem isso, essas variantes nunca são detectadas como início de artigo
    e o conteúdo inteiro sai sem segmentação nenhuma."""
    texto = _ARTIGO_NEGRITO_QUEBRADO_RE.sub(lambda m: f"**Art. {m.group(1)}** ", texto)
    texto = _ARTIGO_SUFIXO_QUEBRADO_RE.sub(
        lambda m: f"**Art. {m.group(1)}-{m.group(2)}.** ", texto
    )
    return texto


def _e_marcador_paragrafo_genuino(texto: str, pos: int) -> bool:
    """Um "§"/"Parágrafo único" só conta como abertura de parágrafo novo se
    o caractere não-espaço imediatamente anterior fechar uma frase (. ou ;)
    -- ou se estiver bem no início do trecho analisado. Do contrário, é uma
    remissão dentro do próprio texto (ex: "nos termos do art. 5º, § 2º"),
    que não deve quebrar nada."""
    i = pos - 1
    while i >= 0 and texto[i].isspace():
        i -= 1
    if i < 0:
        return True  # início do trecho
    return texto[i] in _PONTUACAO_FECHA


def _encontrar_marcadores_paragrafo(texto: str):
    """Devolve lista de (inicio, fim_do_marcador, rotulo) para cada
    parágrafo genuíno encontrado em `texto`, na ordem em que aparecem."""
    candidatos = []
    for m in _PARAGRAFO_NUM_RE.finditer(texto):
        candidatos.append((m.start(), m.end(), f"§ {m.group(1)}º"))
    for m in _PARAGRAFO_UNICO_RE.finditer(texto):
        candidatos.append((m.start(), m.end(), "Parágrafo único"))
    candidatos.sort(key=lambda c: c[0])

    genuinos = [c for c in candidatos if _e_marcador_paragrafo_genuino(texto, c[0])]
    return genuinos


def _segmentar_alineas(texto_inciso: str) -> list:
    """Quebra o texto de um inciso em alíneas, se houver. Cada alínea
    termina em ';' (mesma regra dos incisos)."""
    marcadores = list(_ALINEA_RE.finditer(texto_inciso))
    if not marcadores:
        return []

    alineas = []
    for idx, m in enumerate(marcadores):
        letra = m.group(1)
        inicio = m.end()
        fim = marcadores[idx + 1].start() if idx + 1 < len(marcadores) else len(texto_inciso)
        corpo = texto_inciso[inicio:fim].strip().rstrip(";").strip()
        corpo = re.sub(r"^[\u2013\u2014\-]\s*", "", corpo)
        if corpo:
            alineas.append((letra, corpo))
    return alineas


def _segmentar_incisos(texto_corpo: str) -> list:
    """Quebra o corpo (texto após o caput) em incisos, usando ';' como
    delimitador entre eles, conforme especificado. Cada item devolvido é
    (numeral_romano, texto_do_inciso, alineas)."""
    marcadores = list(_INCISO_RE.finditer(texto_corpo))
    if not marcadores:
        return []

    incisos = []
    for idx, m in enumerate(marcadores):
        numeral = m.group(1)
        inicio = m.end()
        fim = marcadores[idx + 1].start() if idx + 1 < len(marcadores) else len(texto_corpo)
        bruto = texto_corpo[inicio:fim].strip()
        # remove o ';' final (delimitador) mas preserva o texto
        bruto = bruto.rstrip(";").strip()
        alineas = _segmentar_alineas(bruto)
        if alineas:
            # remove o trecho das alíneas do texto "solto" do inciso --
            # sobra só a introdução do inciso antes da primeira alínea
            primeiro_marcador = _ALINEA_RE.search(texto_corpo[inicio:fim])
            intro = texto_corpo[inicio:inicio + primeiro_marcador.start()].strip().rstrip(":;").strip()
            incisos.append((numeral, intro, alineas))
        else:
            incisos.append((numeral, bruto, []))
    return incisos


def segmentar_artigo(texto_artigo: str) -> str:
    """Recebe o texto de UM artigo (já sem o cabeçalho '**Art. N**') e
    devolve a versão estruturada em Markdown: caput -> incisos -> alíneas
    -> parágrafos."""
    texto_artigo = texto_artigo.strip()
    if not texto_artigo:
        return ""

    marcadores_par = _encontrar_marcadores_paragrafo(texto_artigo)

    # tudo antes do primeiro parágrafo genuíno é "caput + incisos"; o resto
    # (se houver) são os parágrafos.
    fim_corpo_principal = marcadores_par[0][0] if marcadores_par else len(texto_artigo)
    corpo_principal = texto_artigo[:fim_corpo_principal].strip()
    trecho_paragrafos = texto_artigo[fim_corpo_principal:]

    # CAPUT: termina no primeiro ':' que aparece antes de qualquer inciso.
    primeiro_inciso = _INCISO_RE.search(corpo_principal)
    pos_dois_pontos = corpo_principal.find(":")

    if pos_dois_pontos != -1:
        caput = corpo_principal[: pos_dois_pontos + 1].strip()
        resto = corpo_principal[pos_dois_pontos + 1 :].strip()
        incisos = _segmentar_incisos(resto)
    else:
        caput = corpo_principal.strip()
        incisos = []

    # PARÁGRAFOS: cada trecho entre um marcador genuíno e o próximo.
    paragrafos = []
    if marcadores_par:
        # marcadores_par tem posições relativas a texto_artigo; recalcula
        # relativo a trecho_paragrafos (que começa em fim_corpo_principal)
        offsets = [(ini - fim_corpo_principal, fim - fim_corpo_principal, rotulo) for ini, fim, rotulo in marcadores_par]
        for idx, (ini, fim, rotulo) in enumerate(offsets):
            prox_ini = offsets[idx + 1][0] if idx + 1 < len(offsets) else len(trecho_paragrafos)
            corpo = trecho_paragrafos[fim:prox_ini].strip()
            if corpo:
                paragrafos.append((rotulo, corpo))

    # ---- monta a saída em Markdown ----
    linhas = []
    if caput:
        linhas.append(caput)

    if incisos:
        linhas.append("")
        for numeral, intro, alineas in incisos:
            if alineas:
                linhas.append(f"- **{numeral}** – {intro}:" if intro else f"- **{numeral}** –")
                for letra, corpo_alinea in alineas:
                    linhas.append(f"  - **{letra})** {corpo_alinea}")
            else:
                linhas.append(f"- **{numeral}** – {intro}")

    if paragrafos:
        linhas.append("")
        for rotulo, corpo in paragrafos:
            linhas.append(f"**{rotulo}.** {corpo}")
            linhas.append("")

    return "\n".join(linhas).strip()


def _bloco_marcador_pagina(lei, pagina_num: str) -> str:
    """Marcador isolado de página/legislação: '---' (linha horizontal, no
    padrão que o Obsidian reconhece como separador, com espaço em branco
    -- linha em branco -- antes e depois), o nome da legislação e o
    número da página em linha própria, e outro '---' fechando. Nunca
    entra no meio do texto de um artigo -- ver `segmentar_legislacao`."""
    rotulo = f"{lei} -- pg {pagina_num}" if lei else f"pg {pagina_num}"
    return f"\n\n---\n*{rotulo}*\n\n---\n"


def segmentar_legislacao(texto: str, titulo_padrao: str = "") -> str:
    """Recebe o Markdown já extraído (texto corrido, com os marcadores
    '**Art. N**' preservados) e devolve a versão reestruturada. Trechos que
    não fazem parte de nenhum artigo (capa, sumário, cabeçalhos de
    capítulo/seção) são preservados como estão -- só o CONTEÚDO de cada
    artigo é reestruturado.

    `titulo_padrao` é o nome da legislação a mostrar no marcador do topo
    do documento (ver `_bloco_marcador_pagina`) quando o próprio PDF não
    tiver um cabeçalho de abertura de lei reconhecível (`_LEI_TITULO_RE`)
    -- normalmente o título/nome do arquivo, vindo de
    `converter_pdf_legislacao`. Opcional: os testes deste módulo chamam
    `segmentar_legislacao` direto, sem simular quebra de página nenhuma,
    e nesse caso nenhum marcador é inserido."""
    texto, eventos = _limpar_e_rotular_paginas(texto, titulo_padrao)
    texto = _normalizar_cabecalhos_artigo(texto)
    # une um bullet solto ("\n- texto") que às vezes sobra no meio de um
    # inciso/parágrafo (artefato do reflow de linha do pipeline padrão) --
    # sem isso, esse pedaço perde a numeração do inciso a que pertence.
    texto = re.sub(r"\n\n?-\s*", " ", texto)

    marcadores = list(_ARTIGO_RE.finditer(texto))

    # agrupa os eventos de página pelo índice do artigo em questão (ver
    # `_limpar_e_rotular_paginas`) -- índice 0 = antes do primeiro
    # artigo, índice N = depois do artigo de índice N-1. Nunca insere no
    # meio de um artigo: sempre num limite entre dois artigos (ou antes
    # do primeiro / depois do último).
    marcadores_por_indice = {}
    for indice_artigos, lei, pagina_num in eventos:
        idx = min(indice_artigos, len(marcadores))
        marcadores_por_indice.setdefault(idx, []).append((lei, pagina_num))

    if not marcadores:
        prefixo = "".join(
            _bloco_marcador_pagina(lei, pagina_num)
            for lei, pagina_num in marcadores_por_indice.get(0, [])
        )
        return f"{prefixo}\n{texto}" if prefixo else texto

    partes = [texto[: marcadores[0].start()]]
    for lei, pagina_num in marcadores_por_indice.get(0, []):
        partes.append(_bloco_marcador_pagina(lei, pagina_num))

    for idx, m in enumerate(marcadores):
        numero = m.group(1) + (f"-{m.group(2)}" if m.group(2) else "")
        fim_artigo = marcadores[idx + 1].start() if idx + 1 < len(marcadores) else len(texto)
        corpo_bruto = texto[m.end() : fim_artigo]

        estruturado = segmentar_artigo(corpo_bruto)
        partes.append(f"\n\n#### Art. {numero}\n\n{estruturado}\n")

        for lei, pagina_num in marcadores_por_indice.get(idx + 1, []):
            partes.append(_bloco_marcador_pagina(lei, pagina_num))

    return "".join(partes)


def converter_pdf_legislacao(input_path: str, job_dir: str, meta: dict) -> dict:
    """Equivalente a `converter.convert_pdf`, mas para legislação: mesma
    extração segura de base (reaproveitada de `converter.py`), com a
    reestruturação de artigos aplicada por cima em vez da nota de aula
    padrão. Não extrai imagens -- o foco aqui é a estrutura do texto."""
    os.makedirs(job_dir, exist_ok=True)

    doc = pymupdf.open(input_path)
    try:
        ocr_por_pagina = _base._paginas_sem_texto_nativo(doc)
    finally:
        doc.close()

    raw_md = pymupdf4llm.to_markdown(input_path, write_images=False, page_separators=True)
    # remove o número de página solto no rodapé (ver `_RODAPE_NUMERO_RE`)
    # ANTES do reflow de parágrafos -- que é o passo (dentro de
    # `_base.process_pages`, chamado logo abaixo) que colaria esse número
    # no meio da palavra/frase seguinte se ele não fosse removido antes.
    raw_md = _remover_rodape_numeros_pagina(raw_md)
    raw_md = _base._aplicar_ocr_manual(raw_md, ocr_por_pagina)
    paginado = _base.process_pages(raw_md)
    limpo = _base.normalize_markdown_text(paginado)

    nome = meta.get("display_name") or _base.slugify(meta.get("original_filename", "legislacao"))
    titulo = meta.get("titulo") or nome

    estruturado = segmentar_legislacao(limpo, titulo)
    n_artigos = len(_ARTIGO_RE.findall(limpo))

    cabecalho = (
        f"# {titulo}\n\n"
        "> Markdown de legislação gerado pelo docintel -- estrutura "
        "Art. > caput > incisos > alíneas > parágrafos derivada por sinais "
        "de pontuação.\n"
    )

    final_md = f"{cabecalho}\n{estruturado}\n"

    md_path = os.path.join(job_dir, f"{nome}.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(final_md)

    return {"md_path": md_path, "n_artigos": n_artigos}
