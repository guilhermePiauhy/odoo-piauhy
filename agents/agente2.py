import base64
import csv
import json
import logging
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from queue import Queue
from threading import Thread
from typing import Optional

import debugpy
import pytesseract
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from pdf2image import convert_from_bytes
from tqdm import tqdm

sys.path.append('/app')

from libs.ai.ai import IA
from libs.healthcheck.healthcheck import HealthcheckServer
from libs.odoo.odoofuncoes import Odoo


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

load_dotenv()


def _env_bool(nome: str, padrao: str = "false") -> bool:
    return (os.getenv(nome, padrao) or "").strip().lower() in {"1", "true", "sim", "yes", "y", "on"}


def attach_debugger():
    if os.getenv("DEBUG_MODE", "false").lower() == "true":
        debugpy.listen(("0.0.0.0", 5678))
        logger.info("Aguardando debugger...")
        debugpy.wait_for_client()
        logger.info("Debugger conectado.")


attach_debugger()


USER_ODOO = "chat.teste"
PW_ODOO = '@parada2024'
ODOO_DB = os.getenv("ODOO_DB", "mmp.intelligenti.com.br")
ODOO_URL = os.getenv("ODOO_URL", "https://mmp.intelligenti.com.br")
MAX_WORKERS = max(1, int(os.getenv("MAX_WORKERS", "10")))
BATCH_SIZE = max(1, int(os.getenv("BATCH_SIZE", "500")))
DATA_LIMITE_DIAS = max(1, int(os.getenv("DATA_LIMITE_DIAS", "45")))
IGNORAR_FILTRO_TOTAL = True
LIMITE_CASOS_TESTE = 100
LIMITE_TOKENS_SENTENCA = max(1000, int(os.getenv("LIMITE_TOKENS_SENTENCA", "30000")))
LIMITE_TOKENS_ACORDAO = max(1000, int(os.getenv("LIMITE_TOKENS_ACORDAO", "20000")))
PLANILHA_CSV = os.getenv("CSV_FILE", "resultados_resultado_decisorio_final.csv")
PROJETO_ALVO = (os.getenv("PROJETO_ALVO") or "").strip()
GRUPO_ALVO = (os.getenv("GRUPO_ALVO") or "").strip()

PDF_MIME = "application/pdf"
HTML_MIMES = {"text/html", "application/xhtml+xml"}

CAMPOS_CSV = [
    "timestamp",
    "dossie_id",
    "dossie",
    "processo",
    "projeto",
    "data_sentenca",
    "data_acordao",
    "classificacao_final",
    "gera_obrigacao_de_pagar",
    "gera_apenas_obrigacao_de_fazer_ou_nao_fazer",
    "gera_obrigacao_hibrida",
    "gera_somente_declaracao_sem_condenacao_material",
    "grau_de_confianca_classificacao",
    "motivo_de_inseguranca",
    "agente_1_resultado_final",
    "agente_1_condenacao_material",
    "agente_1_resumo_tecnico",
    "agente_1_json",
    "agente_2_json",
    "erro",
]

AGENTE_1_TEMPLATE = """
Você é um advogado sênior especialista em análise de sentenças e acórdãos no contencioso cível de massa.

Sua função é reconstruir, com precisão técnica, o resultado jurisdicional final do caso com base exclusivamente nos documentos fornecidos.

Os documentos poderão ser:
- apenas sentença;
- apenas acórdão;
- sentença e acórdão no mesmo caso.

Seu trabalho NÃO é redigir peça processual.
Seu trabalho NÃO é interpretar fatos fora dos autos.
Seu trabalho NÃO é presumir conteúdo ausente.

Seu papel é identificar, a partir do conteúdo decisório, qual é o resultado final vigente do caso e quais comandos efetivamente foram impostos ao final.

REGRAS GERAIS
1. Trabalhe apenas com o conteúdo efetivamente constante dos documentos.
2. Não invente trechos, fundamentos, pedidos ou condenações.
3. Se houver sentença e acórdão, considere como resultado final prevalente o que foi decidido no acórdão.
4. Se o acórdão apenas mantiver a sentença, registre isso expressamente.
5. Se o acórdão reformar total ou parcialmente a sentença, registre exatamente o que foi alterado.
6. Honorários sucumbenciais, custas e despesas processuais devem ser tratados como efeitos acessórios, e não como obrigação material principal do pedido, salvo se o próprio documento indicar de forma excepcional que a obrigação principal do caso se resume a essa verba.
7. Diferencie obrigação principal do mérito de condenações acessórias de sucumbência.
8. Se não houver clareza suficiente no documento, registre a limitação de forma objetiva, sem presumir.

O QUE VOCÊ DEVE IDENTIFICAR
Você deve identificar, ao final da leitura:

A. Qual é o documento final prevalente
- sentença
- acórdão
- sentença mantida por acórdão
- acórdão reformando sentença

B. Qual foi o resultado final do caso
- procedência
- improcedência
- procedência parcial
- extinção sem resolução de mérito
- não foi possível identificar com segurança

C. Se houve condenação material principal
- sim
- não
- parcialmente
- não foi possível identificar com segurança

D. Quais capítulos materiais resultaram do julgamento final
Exemplos:
- obrigação de fazer
- obrigação de não fazer
- obrigação de pagar quantia
- restituição de valores
- indenização por danos morais
- indenização por danos materiais
- declaração sem conteúdo condenatório material
- improcedência sem condenação material principal

E. Se existe comando material final impondo:
- obrigação de fazer
- obrigação de não fazer
- obrigação de pagar
- obrigação meramente declaratória
- nenhuma obrigação material principal

F. Se houve modificação entre sentença e acórdão
- mantido integralmente
- reformado parcialmente
- reformado integralmente
- prejudicado / não aplicável

CRITÉRIOS DE LEITURA
1. Priorize o dispositivo e a conclusão decisória.
2. Use a fundamentação apenas para esclarecer o alcance do dispositivo quando necessário.
3. Se a sentença condena e o acórdão exclui a condenação, prevalece o acórdão.
4. Se a sentença improcede e o acórdão reforma para condenar, prevalece o acórdão.
5. Se houver apenas obrigação declaratória sem imposição executiva material, isso deve ser registrado como declaração sem obrigação material principal.
6. Se houver tutela específica consistente em excluir registro, restabelecer serviço, cancelar contrato, emitir documento, fazer reparo, entregar produto ou providência semelhante, trate como obrigação de fazer ou não fazer, conforme o caso.
7. Se houver condenação em danos morais