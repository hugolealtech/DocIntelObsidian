"""
Testes do pós-processador de legislação (converter_legislacao.py).

Cobrem especificamente os formatos de cabeçalho "**Art. N**" que a
extração de PDF produz na prática e que faziam a segmentação parar de
funcionar a partir de certo ponto do documento (ver correção de
2026-08-19): ponto dentro do negrito, numeração com separador de milhar,
negrito partido em pedaços com risco de ordinal, e sufixo de emenda
("-A", "-B"...) separado em parágrafo próprio.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import converter_legislacao as leg


class ArtigoRegexTests(unittest.TestCase):
    def test_reconhece_formato_simples(self):
        self.assertTrue(leg._ARTIGO_RE.match("**Art. 2**"))

    def test_reconhece_ponto_antes_de_fechar_negrito(self):
        # formato predominante no restante do livro (a partir do art. ~10)
        # -- era exatamente o que quebrava a segmentação.
        m = leg._ARTIGO_RE.match("**Art. 10.**")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "10")

    def test_reconhece_numero_com_separador_de_milhar(self):
        m = leg._ARTIGO_RE.match("**Art. 1.010.**")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "1.010")

    def test_reconhece_numero_de_4_digitos_sem_ponto(self):
        # a extração às vezes engole o separador de milhar
        m = leg._ARTIGO_RE.match("**Art. 1337.**")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "1337")

    def test_reconhece_sufixo_de_emenda_com_travessao_variado(self):
        for travessao in ("-", "\u2011", "\u2013"):
            with self.subTest(travessao=travessao):
                m = leg._ARTIGO_RE.match(f"**Art. 78{travessao}B.**")
                self.assertIsNotNone(m)
                self.assertEqual(m.group(1), "78")
                self.assertEqual(m.group(2), "B")


class NormalizarCabecalhosTests(unittest.TestCase):
    def test_mescla_negrito_partido_com_risco_de_ordinal(self):
        bruto = "**Art.** **~~8~~** **o** É livre a associação profissional."
        normalizado = leg._normalizar_cabecalhos_artigo(bruto)
        self.assertTrue(leg._ARTIGO_RE.match(normalizado))
        m = leg._ARTIGO_RE.match(normalizado)
        self.assertEqual(m.group(1), "8")

    def test_mescla_sufixo_de_emenda_separado_em_paragrafo(self):
        bruto = "**Art. 8**\n\n**~~-~~** **A.** Compete à União definir diretrizes."
        normalizado = leg._normalizar_cabecalhos_artigo(bruto)
        m = leg._ARTIGO_RE.match(normalizado)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "8")
        self.assertEqual(m.group(2), "A")

    def test_mescla_sufixo_em_negrito_unico(self):
        bruto = "**Art. 1**\n\n**-A.** Esta Lei estabelece normas gerais."
        normalizado = leg._normalizar_cabecalhos_artigo(bruto)
        m = leg._ARTIGO_RE.match(normalizado)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(2), "A")


class SegmentarLegislacaoTests(unittest.TestCase):
    def test_segmenta_artigo_com_incisos_alineas_e_paragrafos(self):
        # reprodução do art. 12 da CF, que ficava bagunçado antes da correção
        texto = (
            "**Art. 12.** São brasileiros: I – natos: _a)_ os nascidos no "
            "Brasil; _b)_ os nascidos no estrangeiro de pai brasileiro; "
            "II – naturalizados: _a)_ os que adquiram a nacionalidade "
            "brasileira na forma da lei.\n"
            "**§ 1º.** Aos portugueses com residência permanente no País "
            "serão atribuídos os direitos inerentes ao brasileiro.\n"
            "**Art. 13.** A língua portuguesa é o idioma oficial.\n"
        )
        out = leg.segmentar_legislacao(texto)
        self.assertIn("#### Art. 12", out)
        self.assertIn("#### Art. 13", out)
        self.assertIn("- **I** – natos:", out)
        self.assertIn("- **a)** os nascidos no Brasil", out)
        self.assertIn("- **II** – naturalizados:", out)
        self.assertIn("**§ 1º.**", out)
        self.assertIn("A língua portuguesa é o idioma oficial.", out)

    def test_segmenta_artigos_de_numero_alto_com_milhar(self):
        texto = (
            "**Art. 1.336.** São deveres do condômino.\n"
            "**Art. 1.337.** O condômino que não cumpre com os seus deveres "
            "poderá ser constrangido a pagar multa.\n"
        )
        out = leg.segmentar_legislacao(texto)
        self.assertIn("#### Art. 1.336", out)
        self.assertIn("#### Art. 1.337", out)

    def test_nao_confunde_remissao_com_paragrafo_genuino(self):
        texto = (
            "**Art. 15.** É vedada a cassação de direitos políticos, nos "
            "termos do art. 5º, § 2º.\n"
        )
        out = leg.segmentar_legislacao(texto)
        # a remissão interna não deve virar um bloco "**§ 2º.**" separado
        self.assertNotIn("**§ 2º.**", out)

    def test_paragrafo_solto_apos_dois_pontos_quebra_corretamente(self):
        texto = "**Art. 5.** Todos gozam de aplicação imediata.\n**§ 1º.** As normas definidoras dos direitos e garantias fundamentais têm aplicação imediata.\n"
        out = leg.segmentar_legislacao(texto)
        self.assertIn("**§ 1º.**", out)


if __name__ == "__main__":
    unittest.main()
