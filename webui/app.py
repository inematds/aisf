import io
import os
import re
import sys
import uuid
import subprocess
import threading
import queue
import time
import json
import glob
import zipfile
from pathlib import Path
from flask import Flask, render_template, request, jsonify, Response, send_file
from werkzeug.utils import secure_filename

# Paths
PROJECT_ROOT = Path(__file__).parent.parent
RESULT_DIR = PROJECT_ROOT / "result"
UPLOAD_DIR = PROJECT_ROOT / "uploads"
VENV_PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"

UPLOAD_DIR.mkdir(exist_ok=True)
QUEUES_FILE = UPLOAD_DIR / "queues.json"

PROJECTS_DIR = PROJECT_ROOT / "projetos"
PROJECTS_DIR.mkdir(exist_ok=True)

GLOBAL_CONFIG_FILE   = UPLOAD_DIR / "global_config.json"
SYSTEM_PROMPT_FILE   = UPLOAD_DIR / "system_prompt_episode.txt"

DEFAULT_SYSTEM_PROMPT = """\
Você é um assistente de produção de série animada com IA (SkyReels V3). A partir da descrição do episódio e dos recursos do projeto, gere um array JSON com TODAS as cenas necessárias para cobrir a história completa.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DESCRIÇÃO DO EPISÓDIO:
{description}

{resources}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PARÂMETROS TÉCNICOS:
- Task type   : {task_type}
- Resolução   : {resolution}
- Duração/cena: entre 5 e 8s (MÁXIMO 10s — nunca ultrapassar)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
REFERÊNCIA DAS TASKS DO SKYREELS V3:

• reference_to_video — Gera vídeo a partir de 1–4 imagens de referência + prompt de texto (modelo 14B).
  O campo "ref_imgs" é OBRIGATÓRIO (paths das imagens do personagem/ambiente da cena).
  O "prompt" descreve o movimento e ação que ocorre no vídeo.

• single_shot_extension — Estende um vídeo existente por 5–30s (modelo 14B).
  Usado quando a cena anterior precisa continuar sem corte.

• shot_switching_extension — Estende com transição cinemática de câmera, máx. 5s (modelo 14B).
  Usado para mudança de ângulo ou ambiente com transição suave.

• talking_avatar — Gera avatar falante a partir de retrato + áudio, até 200s (modelo 19B).
  Requer "input_image" (portrait) e "input_audio" (arquivo de áudio).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
REGRAS OBRIGATÓRIAS — preencha TODOS os campos:

1. PROMPT DE VÍDEO (campo "prompt"):
   - Prompt CURTO em INGLÊS (máximo 2 frases) — foque em MOVIMENTO e CÂMERA, não em narrativa
   - O modelo SkyReels anima as imagens de referência — o prompt apenas guia a movimentação
   - Estrutura ideal: "[tipo de plano], [ação principal], [movimento de câmera], anime style"
   - ⚠ PROIBIDO: descrições longas, narrativas, contexto histórico ou explicações no prompt
   - ⚠ O prompt NÃO precisa repetir o que as imagens já mostram (cenário, personagens, cores)
   - Bons exemplos:
     "Medium shot, girl stands up from desk gesturing excitedly, camera slowly pushes in, anime style"
     "Wide shot, characters walk through corridor, camera dollies forward, warm lighting"
     "Close-up, boy looks at holographic screen with curious expression, soft camera pan right"
   - Mau exemplo (NUNCA faça):
     "Wide establishing shot of a futuristic holographic classroom in 2030. Four teenagers sit at interactive desks as holographic projections of ancient Greek maps illuminate the room in blue and gold light. A small robot floats near the teacher's desk."

2. PROMPT DE IMAGEM (campo "image_prompt"):
   - Prompt em INGLÊS para geração de imagem estática via fal.ai / Flux
   - Descreva: personagens presentes, ambiente, cores dominantes, estilo artístico, iluminação, ângulo
   - Mantenha estilo visual consistente com os personagens do projeto
   - ⚠ ESCALA FÍSICA REAL: animais devem aparecer em tamanho real — hamster é do tamanho de uma mão,
     gato do tamanho de um colo, robô companheiro menor que os estudantes. NUNCA exagere o tamanho
     de animais — especifique sempre: "small hamster on Maya's palm", "tiny robot at knee height"
   - Exemplo: "anime style illustration, 2030 futuristic school corridor, teenage girl with purple hair and confident expression, warm morning light, small brown hamster on her shoulder, vibrant colors"

3. TEXTO DE ÁUDIO (campo "audio_text"):
   - Narração ou diálogos em PORTUGUÊS BRASILEIRO para geração via ElevenLabs
   - Inclua APENAS o que será falado/narrado nesta cena
   - ⚠ NUNCA inclua o nome do personagem como prefixo — escreva DIRETO a fala ou narração.
     ERRADO: "Valen: Você também vai para a turma do Professor Dex?"
     CORRETO: "Você também vai para a turma do Professor Dex?"
     ERRADO: "[Lumi] Claro, vamos juntas!"
     CORRETO: "Claro, vamos juntas!"
   - Se a cena for silenciosa ou só musical, use string vazia: ""
   - Mantenha tom e personalidade dos personagens conforme os documentos do projeto
   - ⚠ REGRA DE PROPORÇÃO ÁUDIO/VÍDEO (CRÍTICA):
     O áudio gerado DEVE caber na duração do vídeo. Referência de tempo:
     · 5s de vídeo  → máximo 1-2 frases curtas (~15-20 palavras)
     · 8s de vídeo  → máximo 2-3 frases curtas (~25-35 palavras)
     · 10s de vídeo → máximo 3-4 frases curtas (~40-50 palavras)
     Texto longo demais gera áudio maior que o clip e fica cortado ou dessincronizado.
     EXCEÇÃO: se a descrição do episódio EXPLICITAMENTE pedir narração longa, monólogo
     ou sequência de imagens estáticas, então pode usar texto mais longo e ajustar a
     duração do vídeo proporcionalmente.

4. IMAGENS DE REFERÊNCIA (campo "ref_imgs"):
   - Use os paths EXATOS das imagens listadas nos recursos acima
   - MÁXIMO 4 imagens por cena — NUNCA coloque mais de 4 (limite técnico do pipeline)
   - REGRA DE CONSISTÊNCIA DE AMBIENTE: SEMPRE inclua a imagem do local/ambiente em TODA
     cena que acontece naquele ambiente — inclusive closes e planos de diálogo.
     Sem a imagem do ambiente, o modelo gera um fundo aleatório e inconsistente.
   - Composição ideal por cena:
     · Cena de estabelecimento (wide/panorâmica): ambiente + 1-2 personagens presentes
     · Close/diálogo: personagem que fala + ambiente onde a cena ocorre
     · Grupo: até 2 personagens principais + ambiente (máx 3 refs, reserve 1 slot para variação)
   - Exemplo correto (close em corredor): ["projetos/X/imagens/valen.png", "projetos/X/imagens/escola.png"]
   - Exemplo ERRADO (close sem ambiente): ["projetos/X/imagens/valen.png"]  ← fundo inconsistente!
   - ⚠ PROIBIDO: colocar duas imagens diferentes que mostrem o MESMO personagem — isso
     DUPLICA o personagem na cena (duas cópias do mesmo personagem aparecem lado a lado).
     Use NO MÁXIMO uma imagem por personagem.
   - ⚠ USE A IMAGEM CORRETA para o tipo de personagem: se o personagem é um ANIMAL
     (ratinho, cachorro, robô, etc.), use a imagem desse animal — NUNCA substitua por uma
     imagem de personagem humano para representá-lo.

5. VOZ DO PERSONAGEM (campo "voice_id"):
   - Extraia o voice_id do ElevenLabs diretamente dos documentos do projeto (tabela de casting)
   - Coloque o voice_id do personagem principal que fala ou narra a cena
   - Se a cena for silenciosa ou a voz for genérica/narradora, use string vazia: ""
   - NÃO invente voice_ids — use SOMENTE os que estão explicitamente nos docs do projeto
   - Exemplo: "FIEA0c5UHH9JnvWaQrXS" (Valen), "vibfi5nlk3hs8Mtvf9Oy" (Lumi), etc.

6. TRILHA DE FUNDO (campo "audio_bg"):
   - Path para arquivo de música/trilha do projeto (pasta audios/ do projeto)
   - Mixada a 28% do volume sobre a narração/diálogo (ou sozinha se audio_text vazio)
   - Use para: cenas épicas, panorâmicas, momentos de tensão, transições sem diálogo
   - Se não há trilha disponível ou a cena não precisa, use string vazia: ""
   - Exemplo: "projetos/INETUSX/audios/tema_principal.mp3"

7. CONSISTÊNCIA NARRATIVA:
   - Use os documentos do projeto como base de conhecimento absoluta para personagens, universo e vozes
   - Use EXATAMENTE os nomes dos personagens conforme os documentos do projeto — NUNCA invente
     nomes alternativos ou genéricos (ex: se o personagem chama "Ratinho", não use "Rato" ou "ratinho")
   - ⚠ COERÊNCIA AÇÃO/DIREÇÃO: o audio_text e o prompt de vídeo DEVEM descrever a MESMA
     ação na MESMA direção. Se a narração diz "subindo a escada", o prompt DEVE dizer
     "walking UP the stairs" — jamais "walking down". Contradições entre áudio e vídeo
     destroem completamente a coerência narrativa.
   - Distribua imagens de referência coerentemente (personagem correto em cada cena)
   - Adapte duração conforme intensidade narrativa (~{duration}s como base)
   - Cubra TODA a história descrita — não pule cenas importantes

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Retorne SOMENTE um array JSON válido, sem texto antes ou depois, começando com [ e terminando com ].
Cada cena deve seguir EXATAMENTE este formato:
{json_template}

APENAS o JSON. Nada mais.\
"""


def _load_system_prompt():
    if SYSTEM_PROMPT_FILE.exists():
        return SYSTEM_PROMPT_FILE.read_text(encoding="utf-8")
    return DEFAULT_SYSTEM_PROMPT


# ── Templates dos system prompts por projeto ──────────────────────────────
DEFAULT_PHASE1_TEMPLATE = """\
Você é um supervisor de produção de série animada.
A partir da descrição do episódio e dos recursos visuais do projeto, faça:

1. MAPEAMENTO DE AMBIENTES: Identifique todos os locais/ambientes onde as cenas acontecem
2. ELEMENTOS NOVOS: Identifique elementos visuais que NÃO têm imagem de referência disponível na lista abaixo

Imagens de referência disponíveis no projeto:
{images_list}{docs_str}

Descrição do episódio:
{description}

Retorne SOMENTE um JSON válido com este formato exato:
{{
  "environments": [
    {{
      "name": "nome curto do ambiente",
      "description": "descrição visual detalhada do ambiente",
      "existing_ref": "projetos/X/imagens/Y.png ou null se não existe"
    }}
  ],
  "new_elements": [
    {{
      "name": "nome do elemento",
      "type": "environment|character|object",
      "image_prompt": "prompt detalhado em inglês para gerar imagem via fal.ai (anime style, 16:9, 2030 futuristic)"
    }}
  ]
}}

REGRAS OBRIGATÓRIAS:
- environments.existing_ref: use o path EXATO de uma imagem da lista acima (copie exatamente como está), ou null
- new_elements: SOMENTE elementos que NÃO têm nenhuma imagem correspondente na lista acima
- Máximo 5 novos elementos — priorize ambientes e personagens novos mais importantes
- Se todos os ambientes já têm referência visual: new_elements = []

APENAS o JSON.\
"""

DEFAULT_IMAGE_GUIDE = """\
# Guia de Geração de Imagens

Este arquivo define o estilo e regras para geração de imagens via fal.ai (Flux/Gemini).
As regras aqui são usadas pela IA ao gerar o campo `image_prompt` de cada cena.

## Estilo visual padrão
- anime style illustration
- 2030 futuristic setting
- vibrant colors, detailed backgrounds
- 16:9 aspect ratio

## Regras obrigatórias
- Descreva: personagens presentes, ambiente, cores dominantes, estilo artístico, iluminação, ângulo
- Mantenha estilo visual consistente entre todas as cenas e episódios
- NUNCA inclua elementos que contradizem o universo da série

## Regras de escala física (OBRIGATÓRIO)
- Todos os personagens e animais devem aparecer em proporção física REAL
- Hamster/rato: tamanho de uma mão humana — pequeno, coadjuvante visual
- Gato/cachorro pequeno: tamanho de um lap (colo)
- Estudantes: altura humana normal (1,6m–1,8m)
- Robô companheiro: altura de criança ou menor (não maior que os estudantes)
- NUNCA exagere o tamanho de animais — eles são coadjuvantes, NÃO protagonistas visuais
- Exceção SOMENTE se a descrição da cena explicitamente pedir tamanho diferente
- Para especificar escala no prompt: "small hamster sitting on Maya's palm, hand-sized pet"

## Exemplo de image_prompt
"Anime style illustration, 2030 futuristic school corridor, teenage girl with purple hair
and confident expression, warm morning light, detailed background, vibrant colors, 16:9,
small brown hamster resting on her shoulder, realistic proportions"
\
"""

DEFAULT_AUDIO_GUIDE = """\
# Guia de Geração de Áudio (ElevenLabs TTS)

Este arquivo define as regras para geração de áudio via ElevenLabs TTS.
O campo `audio_text` de cada cena usa estas diretrizes.

## Idioma
- PORTUGUÊS BRASILEIRO exclusivamente

## Tom e estilo
- Narração: terceira pessoa, tempo presente, tom cinemático e envolvente
- Diálogos: primeira pessoa, tom natural e expressivo para cada personagem
- Mantenha a personalidade definida nos documentos do projeto

## Regras
- Inclua APENAS o que será falado ou narrado — sem descrições de cena
- Se a cena for silenciosa ou apenas musical, use string vazia: ""

## ⚠ REGRA CRÍTICA: Proporção áudio/vídeo
O áudio DEVE caber na duração do clip de vídeo. Referência:
- 5s de vídeo  → máximo 1-2 frases curtas (~15-20 palavras)
- 8s de vídeo  → máximo 2-3 frases curtas (~25-35 palavras)
- 10s de vídeo → máximo 3-4 frases curtas (~40-50 palavras)

Texto longo demais gera áudio maior que o clip → dessincronização.

EXCEÇÃO: se a descrição do episódio pedir EXPLICITAMENTE narração longa,
monólogo ou sequência de imagens, pode usar texto mais longo e ajustar
a duração do vídeo proporcionalmente.

## Casting de vozes
Definido na tabela de personagens nos documentos do projeto (campo voice_id).
\
"""

DEFAULT_VIDEO_GUIDE = """\
# Guia de Geração de Vídeo (SkyReels V3)

Este arquivo define as regras para geração de vídeos via SkyReels V3.
O campo `prompt` de cada cena usa estas diretrizes.

## Como o SkyReels funciona
O modelo ANIMA as imagens de referência (ref_imgs). O prompt apenas guia:
- Que AÇÃO/MOVIMENTO ocorre (personagem anda, vira, gesticula)
- Que MOVIMENTO DE CÂMERA usar (pan, dolly, push in, zoom)
- O prompt NÃO precisa descrever o que já está nas imagens (cenário, cores, roupas)

## Regras do prompt de vídeo
- Escreva em INGLÊS — MÁXIMO 2 frases curtas
- Foque em: tipo de plano + ação + movimento de câmera
- ⚠ PROIBIDO: descrições longas, narrativas, contexto ou explicações
- Seja específico sobre direções (up/down/left/right)
- ⚠ COERÊNCIA: o prompt DEVE descrever a mesma ação/direção que o audio_text

## Bons exemplos
"Medium shot, girl stands up gesturing excitedly, camera slowly pushes in, anime style"
"Wide shot, group walks through corridor, camera dollies forward, warm lighting"
"Close-up, boy looks at screen with curiosity, soft camera pan right, anime style"

## Maus exemplos (NUNCA faça)
"Wide establishing shot of a futuristic holographic classroom in 2030. Four teenagers
sit at interactive desks as holographic projections of ancient Greek maps and the
Parthenon illuminate the room in blue and gold light." ← MUITO LONGO, descritivo demais

## Referências visuais (ref_imgs)
- MÁXIMO 4 imagens por cena
- Use SEMPRE a imagem do ambiente + imagem do personagem que aparece
- NUNCA duas imagens do mesmo personagem (duplica o personagem na cena)
\
"""


def _ensure_project_prompts(proj_dir: Path):
    """Cria arquivos de system prompt em docs/ do projeto se não existirem."""
    docs_dir = proj_dir / "docs"
    docs_dir.mkdir(exist_ok=True)
    files = {
        "_sys_episodio.md": DEFAULT_SYSTEM_PROMPT,
        "_sys_fase1.md":    DEFAULT_PHASE1_TEMPLATE,
        "_sys_imagem.md":   DEFAULT_IMAGE_GUIDE,
        "_sys_audio.md":    DEFAULT_AUDIO_GUIDE,
        "_sys_video.md":    DEFAULT_VIDEO_GUIDE,
    }
    for fname, content in files.items():
        fp = docs_dir / fname
        if not fp.exists():
            fp.write_text(content, encoding="utf-8")


def _load_project_prompt(proj_dir: Path, filename: str, default: str) -> str:
    """Carrega prompt do projeto; usa default se não existir."""
    fp = proj_dir / "docs" / filename
    if fp.exists():
        return fp.read_text(encoding="utf-8")
    return default


def _load_global_config():
    if GLOBAL_CONFIG_FILE.exists():
        try:
            return json.loads(GLOBAL_CONFIG_FILE.read_text())
        except Exception:
            pass
    return {}


def _load_effective_cfg(proj_name: str | None = None, nq_image_style: str | None = None) -> dict:
    """Global config merged with project-level then NQ-level overrides (image_style → kie_image_style)."""
    cfg = _load_global_config()
    if proj_name:
        proj_cfg_file = PROJECTS_DIR / proj_name / "config.json"
        if proj_cfg_file.exists():
            try:
                proj_cfg = json.loads(proj_cfg_file.read_text())
                if proj_cfg.get("image_style"):
                    cfg = dict(cfg)
                    cfg["kie_image_style"] = proj_cfg["image_style"]
            except Exception:
                pass
    if nq_image_style:
        cfg = dict(cfg)
        cfg["kie_image_style"] = nq_image_style
    return cfg


app = Flask(__name__)

# ---- Global generation state ----
generation_state = {
    "running": False,
    "log": [],
    "progress": 0,
    "total": 8,
    "status": "idle",  # idle | running | done | error
    "last_video": None,
    "current_job_id": None,
    "current_nq_id": None,
    "current_nq_name": None,
    "current_nq_scene": None,
    "proc": None,
}
log_queue = queue.Queue()

# ---- Job Queue ----
job_queue = []          # list of job dicts (all statuses)
job_queue_lock = threading.Lock()
_job_id_counter = 0

# ---- Episode AI Generation (background) ----
# {job_id: {status: pending|done|error, jobs: [], saved_doc: str, error: str, project: str}}
_ep_gen_state: dict = {}
_ep_gen_by_project: dict = {}   # {proj_name: job_id}  — último job por projeto
_ep_gen_lock = threading.Lock()

# ---- Named Queues ----
named_queues = []
nq_lock = threading.Lock()
_nq_id_counter = 0

# ---- Background episode generation ----
_ep_gen_state: dict = {}       # job_id -> {status, jobs, saved_doc, ep_title, error, raw}
_ep_gen_by_project: dict = {}  # project_name -> job_id (latest)
_ep_gen_lock = threading.Lock()

# ---- Background bulk image generation (environments/elements) ----
_bulk_img_state: dict = {}     # bulk_job_id -> {status, done, total, errors}


def _next_nq_id():
    global _nq_id_counter
    _nq_id_counter += 1
    return _nq_id_counter


def _save_queues():
    """Persist named_queues to disk (called after every mutation)."""
    try:
        with nq_lock:
            data = json.loads(json.dumps(named_queues))  # deep copy via JSON
        # Reset transient states before saving
        for nq in data:
            if nq["status"] == "running":
                nq["status"] = "idle"
            for j in nq["jobs"]:
                if j["status"] in ("running", "pending"):
                    j["status"] = "idle"
        QUEUES_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    except Exception as e:
        print(f"[queues] save error: {e}")


def _load_queues():
    """Load named_queues from disk on startup."""
    global _nq_id_counter, _job_id_counter
    if not QUEUES_FILE.exists():
        return
    try:
        data = json.loads(QUEUES_FILE.read_text())
        for nq in data:
            # Reset any in-flight states from previous run
            if nq.get("status") in ("running", "pending"):
                nq["status"] = "idle"
            for j in nq.get("jobs", []):
                if j.get("status") in ("running", "pending"):
                    j["status"] = "idle"
            named_queues.append(nq)
        # Atribuir ep_code a episódios existentes que não têm código
        _proj_counters: dict = {}
        for nq in named_queues:
            proj = nq.get("project", "")
            if proj and not nq.get("ep_code"):
                _proj_counters[proj] = _proj_counters.get(proj, 0) + 1
                nq["ep_code"] = f"EP{_proj_counters[proj]:03d}"
        # Restore counters to avoid ID collisions
        if named_queues:
            _nq_id_counter = max(nq["id"] for nq in named_queues)
            all_job_ids = [j["id"] for nq in named_queues for j in nq.get("jobs", [])]
            if all_job_ids:
                _job_id_counter = max(all_job_ids)
        print(f"[queues] loaded {len(named_queues)} queue(s) from {QUEUES_FILE}")
    except Exception as e:
        print(f"[queues] load error: {e}")


def _next_job_id():
    global _job_id_counter
    _job_id_counter += 1
    return _job_id_counter


def _parse_project_voices(proj_name: str) -> dict:
    """Lê os docs do projeto e extrai mapa {nome_personagem: voice_id} de tabelas Markdown.
    Procura pela coluna 'Voice ID' (3ª coluna) em tabelas pipe-separadas.
    Padrão: | Personagem | Voz | voice_id | ...
    """
    voices: dict = {}
    docs_dir = PROJECTS_DIR / proj_name / "docs"
    if not docs_dir.exists():
        return voices
    # ElevenLabs voice IDs: alphanum, 15–25 chars
    # Captura: | Nome | qualquer_coisa | VOICE_ID |
    row_re = re.compile(r'^\|\s*([^|]+?)\s*\|[^|]*\|\s*([A-Za-z0-9]{15,25})\s*\|')
    skip_names = {"personagem", "character", "nome", "voz", "voice", "perfil"}
    for f in sorted(docs_dir.glob("*.md")):
        for line in f.read_text(errors="ignore").splitlines():
            m = row_re.match(line.strip())
            if not m:
                continue
            name = m.group(1).strip()
            vid  = m.group(2).strip()
            if name.lower() in skip_names:
                continue
            voices[name] = vid
    return voices


def _match_voice(voices: dict, label: str, fallback: str) -> str:
    """Retorna o voice_id do personagem cujo nome aparece MAIS CEDO no label da cena.
    Evita falso match quando múltiplos personagens estão no título (ex: 'Lumi Chama Valen').
    """
    label_lower = label.lower()
    best_pos = len(label_lower) + 1
    best_vid = fallback
    for name, vid in voices.items():
        first = name.split()[0].lower()
        idx = label_lower.find(first)
        if idx != -1 and idx < best_pos:
            best_pos = idx
            best_vid = vid
    return best_vid


def _next_ep_code(proj_name: str) -> str:
    """Gera o próximo código sequencial de episódio para um projeto (EP001, EP002, ...)."""
    existing = [
        q.get("ep_code", "")
        for q in named_queues
        if q.get("project") == proj_name and q.get("ep_code", "").startswith("EP")
    ]
    nums = []
    for code in existing:
        try:
            nums.append(int(code[2:]))
        except ValueError:
            pass
    n = max(nums, default=0) + 1
    return f"EP{n:03d}"


# Patterns for named-queue reference resolution
_RE_PREV     = re.compile(r'^\{\{prev\}\}$', re.IGNORECASE)
_RE_JOB_IDX  = re.compile(r'^\{\{job:(\d+)\}\}$', re.IGNORECASE)
_RE_SEED_TS  = re.compile(r'result/[^/]+/(\d+)_<timestamp>\.mp4', re.IGNORECASE)


def _resolve_nq_refs(job, nq):
    """Return a shallow copy of job with forward-reference fields resolved.

    Supported syntaxes (in input_video / input_image / input_audio):
      {{prev}}       – output_video of the immediately previous job in the queue
      {{job:N}}      – output_video of the job at 0-based index N
      result/<task>/<seed>_<timestamp>.mp4  – resolved by matching seed (legacy compat)
    """
    if not nq:
        return job

    jobs = nq.get("jobs", [])
    idx  = job.get("nq_job_index", 0)

    def resolve(value):
        if not value or not isinstance(value, str):
            return value

        # {{prev}}
        if _RE_PREV.match(value):
            if idx > 0:
                out = jobs[idx - 1].get("output_video", "")
                if out:
                    print(f"[ref] {{{{prev}}}} → {out}")
                    return out
            print(f"[ref] warning: {{{{prev}}}} could not resolve (idx={idx})")
            return value

        # {{job:N}}
        m = _RE_JOB_IDX.match(value)
        if m:
            ref_idx = int(m.group(1))
            if 0 <= ref_idx < len(jobs):
                out = jobs[ref_idx].get("output_video", "")
                if out:
                    print(f"[ref] {{{{job:{ref_idx}}}}} → {out}")
                    return out
            print(f"[ref] warning: {{{{job:{m.group(1)}}}}} could not resolve")
            return value

        # result/<task>/<seed>_<timestamp>.mp4
        m = _RE_SEED_TS.search(value)
        if m:
            seed = m.group(1)
            for j in jobs:
                if str(j.get("seed", "")) == seed and j.get("output_video"):
                    print(f"[ref] <timestamp> seed={seed} → {j['output_video']}")
                    return j["output_video"]
            print(f"[ref] warning: <timestamp> seed={seed} not found in queue jobs")
            return value

        return value

    resolved = dict(job)
    for field in ("input_video", "input_image", "input_audio"):
        if field in resolved:
            resolved[field] = resolve(resolved[field])
    return resolved


def build_cmd_from_job(job):
    """Build generate_video.py command + env + metadata from a job dict."""
    task_type = job.get("task_type", "reference_to_video")
    prompt    = job.get("prompt", "")
    resolution = job.get("resolution", "540P")
    duration  = str(min(int(job.get("duration", 5)), 10))  # cap at 10s
    seed      = str(job.get("seed", 42))
    offload   = bool(job.get("offload", True))
    low_vram  = bool(job.get("low_vram", False))
    num_inference_steps = int(job.get("num_inference_steps", 4))

    # talking_avatar only supports 480P / 720P
    if task_type == "talking_avatar" and resolution == "540P":
        resolution = "480P"

    cmd = [
        str(VENV_PYTHON), str(PROJECT_ROOT / "generate_video.py"),
        "--task_type", task_type,
        "--prompt", prompt,
        "--resolution", resolution,
        "--duration", duration,
        "--seed", seed,
        "--num_inference_steps", str(num_inference_steps),
    ]

    if low_vram:
        cmd.append("--low_vram")
    elif offload:
        cmd.append("--offload")

    # Task-specific params
    if task_type == "reference_to_video":
        ref_imgs = job.get("ref_imgs", [])
        if isinstance(ref_imgs, str):
            ref_imgs = [r.strip() for r in ref_imgs.split(",") if r.strip()]
        # Filtra refs que não existem em disco — evita crash no generate_video.py
        ref_imgs = [r for r in ref_imgs if r and Path(r).exists()]
        ref_imgs = ref_imgs[:4]  # pipeline limita a 4 imagens (MAX_ALLOWED_REF_IMG_LENGTH)
        if ref_imgs:
            cmd += ["--ref_imgs", ",".join(ref_imgs)]

    if task_type in ("single_shot_extension", "shot_switching_extension"):
        input_video = job.get("input_video", "")
        if input_video:
            cmd += ["--input_video", input_video]

    if task_type == "talking_avatar":
        input_image = job.get("input_image", "")
        input_audio = job.get("input_audio", "")
        if input_image:
            cmd += ["--input_image", input_image]
        if input_audio:
            cmd += ["--input_audio", input_audio]

    env_extra = {}
    if low_vram:
        env_extra["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"

    # Metadata for JSON sidecar
    metadata = {
        "task_type": task_type,
        "prompt": prompt or "(sem prompt)",
        "resolution": resolution,
        "seed": seed,
        "offload": low_vram or offload,
        "low_vram": low_vram,
    }
    if task_type == "reference_to_video":
        ref_imgs = job.get("ref_imgs", [])
        if isinstance(ref_imgs, str):
            ref_imgs = [r.strip() for r in ref_imgs.split(",") if r.strip()]
        metadata["ref_imgs"] = ref_imgs
        metadata["duration"] = duration + "s"
    if task_type in ("single_shot_extension", "shot_switching_extension"):
        metadata["input_video"] = job.get("input_video", "")
        metadata["duration"] = duration + "s"
    if task_type == "talking_avatar":
        metadata["input_image"] = job.get("input_image", "")
        metadata["input_audio"] = job.get("input_audio", "")
        metadata["duration"] = "determinado pelo áudio"

    return cmd, env_extra, metadata


def start_next_queued_job():
    """Called when a generation finishes. Picks the next pending job."""
    if generation_state["running"]:
        return
    with job_queue_lock:
        for job in job_queue:
            if job["status"] == "pending":
                job["status"] = "running"
                job["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                # Resolve named-queue references ({{prev}}, {{job:N}}, <timestamp>)
                nq = None
                if job.get("nq_id") is not None:
                    with nq_lock:
                        nq = next((q for q in named_queues if q["id"] == job["nq_id"]), None)
                effective_job = _resolve_nq_refs(job, nq)
                # Resolve refs quebradas de episódio usando generated_ref do NQ
                if nq and effective_job.get("task_type") == "reference_to_video":
                    raw = effective_job.get("ref_imgs") or []
                    if isinstance(raw, str):
                        raw = [r.strip() for r in raw.split(",") if r.strip()]
                    ep_code = nq.get("ep_code", "")
                    proj_name = nq.get("project", "")
                    ep_dir = PROJECTS_DIR / proj_name / "episodios" / ep_code if proj_name and ep_code else None
                    import unicodedata as _ud
                    ep_ref_map = {}
                    for it in nq.get("environments", []) + nq.get("new_elements", []):
                        g = it.get("generated_ref")
                        if g and Path(g).exists():
                            ep_ref_map[Path(g).stem.lower()] = g
                    def _res(r):
                        if not r or Path(r).exists(): return r
                        if "episodios" not in r: return r
                        def _norm(s):
                            s = _ud.normalize("NFD", s.lower())
                            s = "".join(c for c in s if _ud.category(c) != "Mn")
                            return set(t for t in re.sub(r"[^a-z0-9]", " ", s).split() if len(t) >= 3)
                        toks = _norm(Path(r).stem)
                        best, bs = None, 0
                        for stem, path in ep_ref_map.items():
                            sc = len(toks & _norm(stem))
                            if sc > bs: bs = sc; best = path
                        if best and bs >= 1:
                            print(f"[run-resolve] '{Path(r).name}' → '{Path(best).name}'")
                            return best
                        return r if ep_dir is None else _resolve_ep_ref(r, ep_dir)
                    effective_job = dict(effective_job)
                    effective_job["ref_imgs"] = [_res(r) for r in raw]
                cmd, env_extra, metadata = build_cmd_from_job(effective_job)
                thread = threading.Thread(
                    target=run_generation,
                    args=(cmd, env_extra, metadata, job),
                    daemon=True,
                )
                thread.start()
                return


def _nq_job_done_hook(job):
    """Called after each job finishes. Stops the queue on error; marks done when all finish."""
    nq_id = job.get("nq_id")
    if nq_id is None:
        return
    with nq_lock:
        nq = next((q for q in named_queues if q["id"] == nq_id), None)
        if nq is None:
            return
        # If this job failed, cancel all remaining pending jobs for this queue
        if job.get("status") == "error":
            with job_queue_lock:
                for j in nq["jobs"]:
                    if j["status"] == "pending":
                        j["status"] = "idle"
                        # Remove from global job_queue so they won't start
                        if j in job_queue:
                            job_queue.remove(j)
            nq["status"] = "error"
        else:
            still_active = any(j["status"] in ("pending", "running") for j in nq["jobs"])
            all_finished = all(j["status"] in ("done", "error") for j in nq["jobs"])
            if not still_active:
                if all_finished:
                    any_error = any(j["status"] == "error" for j in nq["jobs"])
                    nq["status"] = "error" if any_error else "done"
                else:
                    nq["status"] = "idle"  # some jobs still idle
    _save_queues()


def run_named_queue(nq_id):
    """Schedule all idle jobs of a named queue for sequential execution."""
    with nq_lock:
        nq = next((q for q in named_queues if q["id"] == nq_id), None)
        if nq is None or nq["status"] == "running":
            return False
        pending_jobs = [j for j in nq["jobs"] if j["status"] == "idle"]
        if not pending_jobs:
            return False
        nq["status"] = "running"
        for j in pending_jobs:
            j["status"] = "pending"

    with job_queue_lock:
        for j in pending_jobs:
            if not any(ex["id"] == j["id"] for ex in job_queue):
                job_queue.append(j)

    if not generation_state["running"]:
        start_next_queued_job()
    return True


def run_single_nq_job_fn(nq_id, job_id):
    """Schedule a single named queue job for execution."""
    with nq_lock:
        nq = next((q for q in named_queues if q["id"] == nq_id), None)
        if nq is None:
            return False
        job = next((j for j in nq["jobs"] if j["id"] == job_id), None)
        if job is None or job["status"] in ("running", "pending"):
            return False
        job["status"] = "pending"
        if nq["status"] == "idle":
            nq["status"] = "running"

    with job_queue_lock:
        if not any(j["id"] == job_id for j in job_queue):
            job_queue.append(job)

    if not generation_state["running"]:
        start_next_queued_job()
    return True


def run_generation(cmd, env_extra=None, metadata=None, job=None):
    generation_state["running"] = True
    generation_state["log"] = []
    generation_state["progress"] = 0
    generation_state["status"] = "running"
    generation_state["last_video"] = None
    generation_state["current_job_id"] = job["id"] if job else None

    # Named queue progress info
    nq_id = job.get("nq_id") if job else None
    if nq_id is not None:
        with nq_lock:
            nq = next((q for q in named_queues if q["id"] == nq_id), None)
            if nq:
                idx = next((i + 1 for i, j in enumerate(nq["jobs"]) if j["id"] == job["id"]), 1)
                total = len(nq["jobs"])
                generation_state["current_nq_id"]   = nq_id
                generation_state["current_nq_name"] = nq["name"]
                generation_state["current_nq_scene"] = f"Cena {idx}/{total} — {job.get('label', '')}"
    else:
        generation_state["current_nq_id"]   = None
        generation_state["current_nq_name"] = None
        generation_state["current_nq_scene"] = None

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    if env_extra:
        env.update(env_extra)

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=str(PROJECT_ROOT),
            env=env,
        )
        generation_state["proc"] = proc

        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue

            generation_state["log"].append(line)
            log_queue.put(line)

            # Parse progress from tqdm output e.g. " 25%|██▌  | 2/8 ["
            _m = re.match(r'^\s*(\d+)%\|', line)
            if _m:
                try:
                    pct = int(_m.group(1))
                    if 0 <= pct <= 100:
                        generation_state["progress"] = pct
                except Exception:
                    pass

        proc.wait()

        if proc.returncode == 0:
            generation_state["status"] = "done"
            if job:
                job["status"] = "done"
                job["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            # Find latest video
            videos = sorted(RESULT_DIR.rglob("*.mp4"), key=lambda p: p.stat().st_mtime)
            if videos:
                last_video = videos[-1]
                generation_state["last_video"] = str(last_video.relative_to(PROJECT_ROOT))
                if job:
                    job["output_video"] = generation_state["last_video"]
                    _save_queues()  # persist "done" + output_video before audio mixing
                    # Auto-mix audio (reference_to_video/extension geram vídeo silencioso)
                    if job.get("task_type") != "talking_avatar":
                        sp_str = job.get("input_audio", "")
                        bg_str = job.get("audio_bg", "")
                        sp = (PROJECT_ROOT / sp_str) if sp_str else None
                        bg = (PROJECT_ROOT / bg_str) if bg_str else None
                        sp = sp if (sp and sp.exists()) else None
                        bg = bg if (bg and bg.exists()) else None
                        if sp or bg:
                            bg_vol = float(job.get("audio_bg_volume", 0.28))
                            _mix_audio_scene(last_video, speech_path=sp, bg_path=bg, bg_volume=bg_vol)
                # Save metadata JSON alongside the video
                if metadata:
                    metadata["generated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    meta_path = last_video.with_suffix(".json")
                    meta_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2))
        else:
            generation_state["status"] = "error"
            if job:
                job["status"] = "error"
                job["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    except Exception as e:
        generation_state["log"].append(f"ERROR: {e}")
        generation_state["status"] = "error"
        if job:
            job["status"] = "error"
            job["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    finally:
        generation_state["running"] = False
        generation_state["current_job_id"] = None
        generation_state["proc"] = None
        log_queue.put("__DONE__")
        # Update named queue status
        if job:
            _nq_job_done_hook(job)
        generation_state["current_nq_id"]    = None
        generation_state["current_nq_name"]  = None
        generation_state["current_nq_scene"] = None
        # Auto-start next pending job
        start_next_queued_job()


# ---- Parse Markdown queue format ----
def parse_md_queue(md_text):
    """Parse a queue from Markdown format. Returns list of job dicts."""
    jobs = []
    current = {}
    for line in md_text.splitlines():
        stripped = line.strip()
        # New job block on ## heading
        if stripped.startswith("##") or (stripped.startswith("#") and not stripped.startswith("##")):
            if current and "task_type" in current:
                jobs.append(current)
            current = {}
        elif stripped.startswith("- "):
            parts = stripped[2:].split(":", 1)
            if len(parts) == 2:
                key = parts[0].strip()
                val = parts[1].strip()
                # Type coercion
                if key in ("duration", "seed"):
                    try:
                        val = int(val)
                    except ValueError:
                        pass
                elif key == "ref_imgs":
                    val = [v.strip() for v in val.split(",") if v.strip()]
                elif key in ("offload", "low_vram"):
                    val = val.lower() in ("true", "yes", "1", "sim")
                current[key] = val
    if current and "task_type" in current:
        jobs.append(current)
    return jobs


# ============================================================
# Routes
# ============================================================

@app.route("/")
def index():
    videos = []
    if RESULT_DIR.exists():
        for v in sorted(RESULT_DIR.rglob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True):
            size_mb = v.stat().st_size / 1024 / 1024
            videos.append({
                "path": str(v.relative_to(PROJECT_ROOT)),
                "name": v.name,
                "task": v.parent.name,
                "size": f"{size_mb:.1f} MB",
                "has_meta": v.with_suffix(".json").exists(),
            })
    return render_template("index.html", videos=videos)


@app.route("/generate", methods=["POST"])
def generate():
    data = request.form

    task_type  = data.get("task_type", "reference_to_video")
    prompt     = data.get("prompt", "")
    resolution = data.get("resolution", "540P")
    duration   = int(data.get("duration", 5))
    seed       = int(data.get("seed", 42))
    offload    = data.get("offload", "true") == "true"
    low_vram   = data.get("low_vram", "false") == "true"

    job = {
        "id": _next_job_id(),
        "task_type": task_type,
        "prompt": prompt,
        "resolution": resolution,
        "duration": duration,
        "seed": seed,
        "offload": offload,
        "low_vram": low_vram,
        "status": "pending",
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "label": f"{task_type} — seed {seed}",
    }

    # Handle file uploads for reference_to_video
    if task_type == "reference_to_video":
        ref_imgs = []
        files = request.files.getlist("ref_imgs")
        for f in files:
            if f and f.filename:
                safe_name = re.sub(r'[^\w\-.]', '_', f.filename)
                save_path = UPLOAD_DIR / safe_name
                f.save(str(save_path))
                ref_imgs.append(str(save_path))
        manual = data.get("ref_imgs_path", "").strip()
        if manual:
            ref_imgs += [p.strip() for p in manual.split(",") if p.strip()]
        if not ref_imgs:
            return jsonify({"error": "Informe ao menos uma imagem de referência"}), 400
        job["ref_imgs"] = ref_imgs

    # Handle input_video for extension tasks
    if task_type in ("single_shot_extension", "shot_switching_extension"):
        input_video = data.get("input_video", "").strip()
        if not input_video:
            return jsonify({"error": "Informe o vídeo de entrada"}), 400
        job["input_video"] = input_video

    # Handle talking_avatar inputs
    if task_type == "talking_avatar":
        input_image_file = request.files.get("input_image_file")
        if input_image_file and input_image_file.filename:
            safe_name = re.sub(r'[^\w\-.]', '_', input_image_file.filename)
            save_path = UPLOAD_DIR / safe_name
            input_image_file.save(str(save_path))
            job["input_image"] = str(save_path)
        else:
            input_image = data.get("input_image", "").strip()
            if not input_image:
                return jsonify({"error": "Informe a imagem do retrato"}), 400
            job["input_image"] = input_image

        input_audio_file = request.files.get("input_audio_file")
        if input_audio_file and input_audio_file.filename:
            safe_name = re.sub(r'[^\w\-.]', '_', input_audio_file.filename)
            save_path = UPLOAD_DIR / safe_name
            input_audio_file.save(str(save_path))
            job["input_audio"] = str(save_path)
        else:
            input_audio = data.get("input_audio", "").strip()
            if not input_audio:
                return jsonify({"error": "Informe o arquivo de áudio"}), 400
            job["input_audio"] = input_audio

    # Enqueue and auto-start if idle
    with job_queue_lock:
        job_queue.append(job)

    if not generation_state["running"]:
        start_next_queued_job()

    return jsonify({"ok": True, "job_id": job["id"]})


@app.route("/stream")
def stream():
    def event_gen():
        for line in generation_state["log"]:
            yield f"data: {json.dumps({'log': line})}\n\n"

        while True:
            try:
                line = log_queue.get(timeout=1)
                if line == "__DONE__":
                    payload = {
                        "status": generation_state["status"],
                        "progress": generation_state["progress"],
                        "video": generation_state.get("last_video"),
                    }
                    yield f"data: {json.dumps({'done': payload})}\n\n"
                    break
                yield f"data: {json.dumps({'log': line, 'progress': generation_state['progress']})}\n\n"
            except queue.Empty:
                if not generation_state["running"]:
                    break
                yield f"data: {json.dumps({'ping': True, 'progress': generation_state['progress'], 'nq_scene': generation_state.get('current_nq_scene')})}\n\n"

    return Response(event_gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/status")
def status():
    return jsonify({k: v for k, v in generation_state.items() if k != "proc"})


@app.route("/cancel", methods=["POST"])
def cancel_generation():
    """Cancela a geração em andamento (SIGTERM no subprocess)."""
    proc = generation_state.get("proc")
    if not generation_state.get("running") or proc is None:
        return jsonify({"error": "Nenhuma geração em andamento"}), 400
    proc.terminate()
    return jsonify({"ok": True})



@app.route("/uploads/list")
def list_uploads():
    """Lista todos os arquivos na pasta uploads/ (nível raiz, sem subpastas).
    Retorna 'files' (imagens) e 'docs' (textos/outros) separados.
    """
    IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
    SKIP       = {"queues.json", "global_config.json"}
    images, docs = [], []
    for f in sorted(UPLOAD_DIR.iterdir()):
        if not f.is_file() or f.name in SKIP:
            continue
        entry = {
            "name": f.name,
            "size": f.stat().st_size,
            "path": str(f.relative_to(PROJECT_ROOT)),
        }
        if f.suffix.lower() in IMAGE_EXTS:
            images.append(entry)
        else:
            docs.append(entry)
    return jsonify({"files": images, "docs": docs})


@app.route("/file/<path:filepath>")
def serve_file(filepath):
    """Serve any project file inline (for image thumbnails, etc.)."""
    full = PROJECT_ROOT / filepath
    try:
        full = full.resolve()
        if not str(full).startswith(str(PROJECT_ROOT.resolve())):
            return "Forbidden", 403
    except Exception:
        return "Not found", 404
    if not full.exists() or not full.is_file():
        return "Not found", 404
    return send_file(str(full))


@app.route("/video/<path:filepath>")
def serve_video(filepath):
    full = PROJECT_ROOT / filepath
    if not full.exists():
        return "Not found", 404
    return send_file(str(full), mimetype="video/mp4")


@app.route("/video-meta/<path:filepath>")
def video_meta(filepath):
    full = PROJECT_ROOT / filepath
    meta = full.with_suffix(".json")
    if not meta.exists():
        return jsonify({}), 404
    return jsonify(json.loads(meta.read_text()))


@app.route("/download/<path:filepath>")
def download_file(filepath):
    full = PROJECT_ROOT / filepath
    try:
        full = full.resolve()
        PROJECT_ROOT.resolve()
    except Exception:
        return "Not found", 404
    if not str(full).startswith(str(PROJECT_ROOT.resolve())):
        return "Forbidden", 403
    if not full.exists() or not full.is_file():
        return "Not found", 404
    return send_file(str(full), as_attachment=True, download_name=full.name)


@app.route("/nqueues/<int:nq_id>/export-json")
def export_nq_json(nq_id):
    with nq_lock:
        nq = next((q for q in named_queues if q["id"] == nq_id), None)
        if nq is None:
            return "Fila não encontrada", 404
        data = json.dumps(nq, indent=2, ensure_ascii=False).encode("utf-8")
        safe = "".join(c if c.isalnum() or c in "-_ " else "_" for c in nq["name"]).strip()
    buf = io.BytesIO(data)
    return send_file(buf, as_attachment=True, download_name=f"{safe}.json", mimetype="application/json")


@app.route("/nqueues/<int:nq_id>/download-zip")
def download_nq_zip(nq_id):
    include_sources = request.args.get("include_sources", "0") == "1"
    with nq_lock:
        nq = next((q for q in named_queues if q["id"] == nq_id), None)
        if nq is None:
            return "Fila não encontrada", 404
        jobs_snap = [dict(j) for j in nq["jobs"]]
        safe = "".join(c if c.isalnum() or c in "-_ " else "_" for c in nq["name"]).strip()
    buf = io.BytesIO()
    added = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for job in jobs_snap:
            if not job.get("output_video"):
                continue
            vp = PROJECT_ROOT / job["output_video"]
            if vp.exists() and str(vp) not in added:
                zf.write(str(vp), f"videos/{vp.name}")
                added.add(str(vp))
            if include_sources:
                for ref in job.get("ref_imgs") or []:
                    p = PROJECT_ROOT / ref
                    if p.exists() and str(p) not in added:
                        zf.write(str(p), f"sources/{p.name}")
                        added.add(str(p))
                for key in ("input_video", "input_image", "input_audio"):
                    val = job.get(key)
                    if val:
                        p = PROJECT_ROOT / val
                        if p.exists() and str(p) not in added:
                            zf.write(str(p), f"sources/{p.name}")
                            added.add(str(p))
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=f"{safe}.zip", mimetype="application/zip")


@app.route("/videos")
def list_videos():
    videos = []
    if RESULT_DIR.exists():
        for v in sorted(RESULT_DIR.rglob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True):
            size_mb = v.stat().st_size / 1024 / 1024
            videos.append({
                "path": str(v.relative_to(PROJECT_ROOT)),
                "name": v.name,
                "task": v.parent.name,
                "size": f"{size_mb:.1f} MB",
                "has_meta": v.with_suffix(".json").exists(),
            })
    return jsonify(videos)


# ---- Queue endpoints ----

@app.route("/queue", methods=["GET"])
def get_queue():
    with job_queue_lock:
        return jsonify(list(job_queue))


@app.route("/queue/add", methods=["POST"])
def queue_add():
    data = request.get_json(force=True, silent=True)
    if not data or not isinstance(data, dict):
        return jsonify({"error": "JSON inválido"}), 400
    if not data.get("task_type"):
        return jsonify({"error": "task_type obrigatório"}), 400

    job = {
        "id": _next_job_id(),
        "status": "pending",
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "label": data.get("label") or f"{data['task_type']} — seed {data.get('seed', 42)}",
        **data,
    }
    with job_queue_lock:
        job_queue.append(job)

    if not generation_state["running"]:
        start_next_queued_job()

    return jsonify({"ok": True, "id": job["id"]})


@app.route("/queue/clear", methods=["POST"])
def queue_clear():
    with job_queue_lock:
        removed = sum(1 for j in job_queue if j["status"] == "pending")
        job_queue[:] = [j for j in job_queue if j["status"] != "pending"]
    return jsonify({"ok": True, "removed": removed})


@app.route("/queue/remove/<int:job_id>", methods=["POST"])
def queue_remove(job_id):
    with job_queue_lock:
        for i, job in enumerate(job_queue):
            if job["id"] == job_id and job["status"] == "pending":
                job_queue.pop(i)
                return jsonify({"ok": True})
    return jsonify({"error": "Job não encontrado ou não está pendente"}), 404


@app.route("/queue/import", methods=["POST"])
def queue_import():
    content = request.data.decode("utf-8").strip()
    if not content:
        return jsonify({"error": "Conteúdo vazio"}), 400

    try:
        # Auto-detect format
        if content.startswith("[") or content.startswith("{"):
            raw = json.loads(content)
            if isinstance(raw, dict):
                raw = [raw]
            jobs_data = raw
        else:
            jobs_data = parse_md_queue(content)

        added = 0
        with job_queue_lock:
            for data in jobs_data:
                if not data.get("task_type"):
                    continue
                job = {
                    "id": _next_job_id(),
                    "status": "pending",
                    "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "label": data.get("label") or f"{data['task_type']} — seed {data.get('seed', 42)}",
                    **data,
                }
                job_queue.append(job)
                added += 1

        if added > 0 and not generation_state["running"]:
            start_next_queued_job()

        return jsonify({"ok": True, "added": added})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/doc/<path:filename>")
def serve_doc(filename):
    doc_dir = PROJECT_ROOT / "doc"
    full = doc_dir / filename
    if not full.exists() or not full.is_file():
        return "Not found", 404
    return send_file(str(full), mimetype="text/plain; charset=utf-8")


@app.route("/doc/download/<path:filename>")
def download_doc(filename):
    doc_dir = PROJECT_ROOT / "doc"
    full = doc_dir / filename
    if not full.exists() or not full.is_file():
        return "Not found", 404
    return send_file(str(full), as_attachment=True, download_name=filename)


@app.route("/help")
def help_page():
    return render_template("help.html")


# ---- Named Queue endpoints ----

def _estimate_job_minutes(job):
    """Estimated generation time in minutes for a single job."""
    t   = job.get("task_type", "")
    res = job.get("resolution", "720P")
    dur = job.get("duration", 5) or 5
    if t == "reference_to_video":
        base = {"480P": 8, "540P": 12, "720P": 18}.get(res, 14)
        return base + dur * 0.5
    if t == "talking_avatar":
        return {"480P": 15, "720P": 22}.get(res, 15)
    if t == "single_shot_extension":
        return 8 + dur * 0.4
    if t == "shot_switching_extension":
        return 5 + dur * 0.3
    return 10

@app.route("/nqueues", methods=["GET"])
def get_named_queues():
    with nq_lock:
        result = [{
            "id": nq["id"],
            "name": nq["name"],
            "project": nq.get("project", ""),
            "ep_code": nq.get("ep_code", ""),
            "status": nq["status"],
            "job_count": len(nq["jobs"]),
            "done_count": sum(1 for j in nq["jobs"] if j["status"] == "done"),
            "error_count": sum(1 for j in nq["jobs"] if j["status"] == "error"),
            "created_at": nq["created_at"],
            "estimated_minutes": round(sum(
                _estimate_job_minutes(j) for j in nq["jobs"]
            )),
            "remaining_minutes": round(sum(
                _estimate_job_minutes(j) for j in nq["jobs"]
                if j["status"] not in ("done",)
            )),
        } for nq in named_queues]
    return jsonify(result)


@app.route("/nqueues", methods=["POST"])
def create_named_queue():
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"error": "JSON inválido"}), 400
    name = data.get("name") or f"Fila {len(named_queues) + 1}"
    jobs_data = data.get("jobs", [])
    if isinstance(jobs_data, dict):
        jobs_data = [jobs_data]

    nq_id = _next_nq_id()
    jobs = []
    for i, jd in enumerate(jobs_data):
        if not jd.get("task_type"):
            continue
        jobs.append({
            "id": _next_job_id(),
            "nq_id": nq_id,
            "nq_job_index": i,
            "status": "idle",
            "label": jd.get("label") or f"{jd['task_type']} — seed {jd.get('seed', 42)}",
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "output_video": "",
            **{k: v for k, v in jd.items() if k not in ("id", "nq_id", "status")},
        })

    proj = data.get("project", "")
    nq = {
        "id": nq_id,
        "name": name,
        "project": proj,
        "status": "idle",
        "jobs": jobs,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if proj:
        # Aceitar ep_code pré-calculado (gerado pelo generate-episode) ou gerar novo
        nq["ep_code"] = data.get("ep_code") or _next_ep_code(proj)
    # Persistir environments e new_elements gerados pela IA (prompts para geração de imagens)
    if data.get("environments"):
        nq["environments"] = data["environments"]
    if data.get("new_elements"):
        nq["new_elements"] = data["new_elements"]
    # Persistir descrição/sinopse do episódio
    if data.get("description"):
        nq["description"] = data["description"]
    with nq_lock:
        named_queues.append(nq)
    _save_queues()
    return jsonify({"ok": True, "id": nq_id, "ep_code": nq.get("ep_code", "")})


@app.route("/nqueues/import", methods=["POST"])
def import_nq_route():
    content = request.data.decode("utf-8").strip()
    name = request.args.get("name") or f"Fila {len(named_queues) + 1}"
    project = request.args.get("project", "")
    if not content:
        return jsonify({"error": "Conteúdo vazio"}), 400
    try:
        if content.startswith("[") or content.startswith("{"):
            raw = json.loads(content)
            if isinstance(raw, dict):
                raw = [raw]
            jobs_data = raw
        else:
            jobs_data = parse_md_queue(content)

        nq_id = _next_nq_id()
        jobs = []
        for i, jd in enumerate(jobs_data):
            if not jd.get("task_type"):
                continue
            jobs.append({
                "id": _next_job_id(),
                "nq_id": nq_id,
                "nq_job_index": i,
                "status": "idle",
                "label": jd.get("label") or f"{jd['task_type']} — seed {jd.get('seed', 42)}",
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "output_video": "",
                **{k: v for k, v in jd.items() if k not in ("id", "nq_id", "status")},
            })

        nq = {
            "id": nq_id,
            "name": name,
            "project": project,
            "status": "idle",
            "jobs": jobs,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        with nq_lock:
            named_queues.append(nq)
        _save_queues()
        return jsonify({"ok": True, "id": nq_id, "name": name, "job_count": len(jobs)})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/nqueues/<int:nq_id>", methods=["GET"])
def get_named_queue_detail(nq_id):
    with nq_lock:
        nq = next((q for q in named_queues if q["id"] == nq_id), None)
    if nq is None:
        return jsonify({"error": "Fila não encontrada"}), 404
    return jsonify(nq)


@app.route("/nqueues/<int:nq_id>/jobs", methods=["POST"])
def add_nq_job(nq_id):
    data = request.get_json(force=True)
    if not data.get("task_type"):
        return jsonify({"error": "task_type obrigatório"}), 400
    with nq_lock:
        nq = next((q for q in named_queues if q["id"] == nq_id), None)
        if nq is None:
            return jsonify({"error": "Fila não encontrada"}), 404
        job_id = _next_job_id()
        job = {
            "id": job_id,
            "nq_id": nq_id,
            "nq_job_index": len(nq["jobs"]),
            "status": "idle",
            "label": data.get("label") or f"{data['task_type']} — seed {data.get('seed', 42)}",
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "output_video": "",
            **{k: v for k, v in data.items() if k not in ("id", "nq_id", "status", "created_at", "output_video")},
        }
        nq["jobs"].append(job)
    _save_queues()
    return jsonify({"ok": True, "id": job_id})


@app.route("/nqueues/<int:nq_id>/jobs/<int:job_id>", methods=["DELETE"])
def delete_nq_job(nq_id, job_id):
    with nq_lock:
        nq = next((q for q in named_queues if q["id"] == nq_id), None)
        if nq is None:
            return jsonify({"error": "Fila não encontrada"}), 404
        job = next((j for j in nq["jobs"] if j["id"] == job_id), None)
        if job is None:
            return jsonify({"error": "Cena não encontrada"}), 404
        if job["status"] == "running":
            return jsonify({"error": "Cena em execução não pode ser removida"}), 400
        nq["jobs"] = [j for j in nq["jobs"] if j["id"] != job_id]
    _save_queues()
    return jsonify({"ok": True})


@app.route("/nqueues/<int:nq_id>/jobs/<int:job_id>", methods=["PATCH"])
def patch_nq_job(nq_id, job_id):
    data = request.get_json(force=True)
    PROTECTED = {"id", "nq_id", "nq_job_index", "status", "created_at",
                 "output_video", "started_at", "finished_at", "task_type"}
    with nq_lock:
        nq = next((q for q in named_queues if q["id"] == nq_id), None)
        if nq is None:
            return jsonify({"error": "Fila não encontrada"}), 404
        job = next((j for j in nq["jobs"] if j["id"] == job_id), None)
        if job is None:
            return jsonify({"error": "Cena não encontrada"}), 404
        if job["status"] == "running":
            return jsonify({"error": "Cena não pode ser editada enquanto está rodando"}), 400
        was_done_or_error = job["status"] in ("done", "error")
        for k, v in data.items():
            if k not in PROTECTED:
                job[k] = v
        # Campos de áudio/voz não invalidam o vídeo gerado — não resetar o job
        AUDIO_ONLY = {"audio_bg", "audio_text", "voice_id", "input_audio"}
        edits = set(k for k in data if k not in PROTECTED)
        audio_only_edit = bool(edits) and edits.issubset(AUDIO_ONLY)
        if was_done_or_error and not audio_only_edit:
            job["status"] = "idle"
            job["output_video"] = ""
            job["error"] = ""
            job["started_at"] = ""
            job["finished_at"] = ""
    _save_queues()
    return jsonify({"ok": True, "reset": was_done_or_error})


@app.route("/nqueues/<int:nq_id>", methods=["DELETE"])
def delete_named_queue_route(nq_id):
    force = request.args.get("force") == "true"
    with nq_lock:
        nq = next((q for q in named_queues if q["id"] == nq_id), None)
        if nq is None:
            return jsonify({"error": "Fila não encontrada"}), 404
        if nq["status"] == "running":
            return jsonify({"error": "Não é possível excluir uma fila em execução"}), 400
        if nq.get("project") and not force:
            # Episódio vinculado a projeto sem force: só limpa jobs e reseta status
            nq["jobs"] = []
            nq["status"] = "idle"
            nq["current_job"] = 0
        else:
            named_queues.remove(nq)
    _save_queues()
    return jsonify({"ok": True})


@app.route("/nqueues/<int:nq_id>/gallery")
def nq_gallery(nq_id):
    """Lista os assets gerados para o episódio: imagens, áudios e vídeos."""
    nq = next((q for q in named_queues if q["id"] == nq_id), None)
    if nq is None:
        return jsonify({"error": "Fila não encontrada"}), 404

    proj_name = nq.get("project", "")
    ep_code   = nq.get("ep_code", "")

    result: dict = {"images": [], "audios": [], "trilhas": [], "videos": [], "docs": []}

    if proj_name and ep_code:
        ep_dir = PROJECTS_DIR / proj_name / "episodios" / ep_code
        for subdir in ("imagens", "ambiente", "elementos"):
            sub = ep_dir / subdir
            if sub.exists():
                for f in sorted(sub.glob("*")):
                    if f.is_file():
                        result["images"].append(str(f.relative_to(PROJECT_ROOT)))
        for f in sorted((ep_dir / "audios").glob("*")) if (ep_dir / "audios").exists() else []:
            if f.is_file():
                result["audios"].append(str(f.relative_to(PROJECT_ROOT)))
        for f in sorted((ep_dir / "trilha").glob("*")) if (ep_dir / "trilha").exists() else []:
            if f.suffix.lower() in (".mp3", ".wav", ".ogg", ".m4a"):
                result["trilhas"].append(str(f.relative_to(PROJECT_ROOT)))

    # Vídeos gerados (output_video de cada job) — inclui metadados para a galeria
    for j in nq.get("jobs", []):
        vid = j.get("output_video", "")
        if vid and (PROJECT_ROOT / vid).exists():
            result["videos"].append({
                "path": vid,
                "job_id": j.get("id"),
                "label": j.get("label", ""),
                "refs": j.get("ref_imgs", []),
            })

    return jsonify(result)


@app.route("/nqueues/<int:nq_id>/available-refs")
def nq_available_refs(nq_id):
    """Lista imagens disponíveis para uso como ref, agrupadas por categoria."""
    nq = next((q for q in named_queues if q["id"] == nq_id), None)
    if nq is None:
        return jsonify({"error": "Fila não encontrada"}), 404

    proj_name = nq.get("project", "")
    ep_code   = nq.get("ep_code", "")

    IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

    def _list(folder: Path, category: str):
        if not folder.exists():
            return []
        return [
            {"path": str(f.relative_to(PROJECT_ROOT)), "name": f.name, "category": category}
            for f in sorted(folder.iterdir())
            if f.is_file() and f.suffix.lower() in IMAGE_EXTS
        ]

    result = {"imagens": [], "ambiente": [], "elementos": [], "projeto": []}

    if proj_name and ep_code:
        ep_dir = PROJECTS_DIR / proj_name / "episodios" / ep_code
        result["imagens"]   = _list(ep_dir / "imagens",   "imagens")
        result["ambiente"]  = _list(ep_dir / "ambiente",  "ambiente")
        result["elementos"] = _list(ep_dir / "elementos", "elementos")

    if proj_name:
        result["projeto"] = _list(PROJECTS_DIR / proj_name / "imagens",    "projeto")
        result["figurantes"] = _list(PROJECTS_DIR / proj_name / "figurantes", "figurantes")

    return jsonify(result)


@app.route("/nqueues/<int:nq_id>/jobs/<int:job_id>/video", methods=["DELETE"])
def delete_nq_job_video(nq_id, job_id):
    """Deleta o arquivo de vídeo gerado e reseta o job para idle."""
    with nq_lock:
        nq = next((q for q in named_queues if q["id"] == nq_id), None)
        if nq is None:
            return jsonify({"error": "Fila não encontrada"}), 404
        job = next((j for j in nq["jobs"] if j["id"] == job_id), None)
        if job is None:
            return jsonify({"error": "Cena não encontrada"}), 404
        if job["status"] == "running":
            return jsonify({"error": "Não é possível deletar vídeo em execução"}), 400
        vid = job.get("output_video", "")
        if vid:
            try:
                vid_path = PROJECT_ROOT / vid
                if vid_path.exists():
                    vid_path.unlink()
            except Exception:
                pass
        job["output_video"] = ""
        job["status"] = "idle"
        job["error"] = ""
        job["started_at"] = ""
        job["finished_at"] = ""
    _save_queues()
    return jsonify({"ok": True})


@app.route("/nqueues/<int:nq_id>/upload/<subfolder>", methods=["POST"])
def nq_upload_file(nq_id, subfolder):
    """Upload de arquivo para pasta do episódio (trilha, imagens, audios)."""
    if subfolder not in ("trilha", "imagens", "audios"):
        return jsonify({"error": "Pasta inválida"}), 400
    nq = next((q for q in named_queues if q["id"] == nq_id), None)
    if nq is None:
        return jsonify({"error": "Fila não encontrada"}), 404
    proj_name = nq.get("project", "")
    ep_code   = nq.get("ep_code", "")
    if not proj_name or not ep_code:
        return jsonify({"error": "Episódio não vinculado a projeto"}), 400
    ep_dir = PROJECTS_DIR / proj_name / "episodios" / ep_code / subfolder
    ep_dir.mkdir(parents=True, exist_ok=True)
    uploaded = []
    for file in request.files.getlist("files"):
        fname = secure_filename(file.filename)
        if not fname:
            continue
        file.save(str(ep_dir / fname))
        uploaded.append(fname)
    return jsonify({"ok": True, "uploaded": uploaded})


def _nq_gen_prompt_image(nq_id: int, item_type: str, idx: int):
    """Gera imagem para um ambiente ou elemento pelo índice no NQ.

    item_type: 'environments' | 'new_elements'
    Salva em projetos/<proj>/episodios/<ep_code>/ambiente/ ou /elementos/
    Atualiza o campo 'generated_ref' do item e persiste.
    """
    import urllib.request as urllib_req

    with nq_lock:
        nq = next((q for q in named_queues if q["id"] == nq_id), None)
    if nq is None:
        return jsonify({"error": "Fila não encontrada"}), 404

    items = nq.get(item_type, [])
    if idx < 0 or idx >= len(items):
        return jsonify({"error": "Índice inválido"}), 400

    item = items[idx]
    proj_name = nq.get("project", "")
    ep_code   = nq.get("ep_code", "")
    if not proj_name or not ep_code:
        return jsonify({"error": "Episódio sem projeto ou ep_code"}), 400

    subfolder = "ambiente" if item_type == "environments" else "elementos"
    img_dir = PROJECTS_DIR / proj_name / "episodios" / ep_code / subfolder
    img_dir.mkdir(parents=True, exist_ok=True)

    safe_name = re.sub(r'[^\w\-]', '_', item.get("name", "item")[:40])
    dest = img_dir / f"{safe_name}.png"

    cfg = _load_effective_cfg(proj_name, nq.get("image_style"))
    img_prompt = item.get("image_prompt") or item.get("description") or item.get("name", "")

    # Refs: existing_ref + match por nome + personagens do episódio como referência de estilo
    # Os personagens carregam o estilo visual do projeto (paleta, traço, arte) — essencial para consistência
    ref_paths = []
    existing = item.get("existing_ref") or item.get("generated_ref")
    if existing and (PROJECT_ROOT / existing).exists():
        ref_paths.append(existing)
    # Match automático por nome do ambiente/elemento contra imagens do projeto
    if proj_name and len(ref_paths) < 4:
        item_name = item.get("name", "")
        auto = _auto_match_refs(item_name, proj_name, exclude=ref_paths)
        for a in auto:
            if len(ref_paths) >= 4:
                break
            ref_paths.append(a)
        if auto:
            print(f"[auto_match] '{item_name}' → {auto}")
    # Personagens/figurantes do episódio como refs de estilo visual
    ep_chars = list(nq.get("characters", [])) + list(nq.get("figurantes", []))
    for c in ep_chars:
        if len(ref_paths) >= 4:
            break
        if c not in ref_paths and (PROJECT_ROOT / c).exists():
            ref_paths.append(c)

    try:
        result = _dispatch_image(cfg, img_prompt, ref_paths or None)
        url = result["images"][0]["url"]
        _download_image(url, dest)
        rel = str(dest.relative_to(PROJECT_ROOT))
        with nq_lock:
            nq[item_type][idx]["generated_ref"] = rel
        _save_queues()
        return jsonify({"ok": True, "path": rel})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/nqueues/<int:nq_id>/environments/<int:idx>/generate-image", methods=["POST"])
def nq_gen_env_image(nq_id, idx):
    return _nq_gen_prompt_image(nq_id, "environments", idx)


@app.route("/nqueues/<int:nq_id>/new-elements/<int:idx>/generate-image", methods=["POST"])
def nq_gen_element_image(nq_id, idx):
    return _nq_gen_prompt_image(nq_id, "new_elements", idx)


def _nq_patch_prompt_item(nq_id: int, item_type: str, idx: int):
    """Atualiza campos editáveis de um ambiente ou elemento (name, image_prompt, description)."""
    data = request.get_json(force=True, silent=True) or {}
    with nq_lock:
        nq = next((q for q in named_queues if q["id"] == nq_id), None)
        if nq is None:
            return jsonify({"error": "Fila não encontrada"}), 404
        items = nq.get(item_type, [])
        if idx < 0 or idx >= len(items):
            return jsonify({"error": "Índice inválido"}), 400
        for field in ("name", "image_prompt", "description", "existing_ref"):
            if field in data:
                items[idx][field] = data[field]
    _save_queues()
    return jsonify({"ok": True})


@app.route("/nqueues/<int:nq_id>/environments/<int:idx>", methods=["PATCH"])
def nq_patch_env(nq_id, idx):
    return _nq_patch_prompt_item(nq_id, "environments", idx)


@app.route("/nqueues/<int:nq_id>/new-elements/<int:idx>", methods=["PATCH"])
def nq_patch_element(nq_id, idx):
    return _nq_patch_prompt_item(nq_id, "new_elements", idx)


@app.route("/nqueues/<int:nq_id>/generate-all-prompt-images", methods=["POST"])
def nq_gen_all_prompt_images(nq_id):
    """Gera imagens de todos ambientes + elementos em background e retorna job_id."""
    import urllib.request as urllib_req

    with nq_lock:
        nq = next((q for q in named_queues if q["id"] == nq_id), None)
    if nq is None:
        return jsonify({"error": "Fila não encontrada"}), 404

    proj_name = nq.get("project", "")
    ep_code   = nq.get("ep_code", "")
    if not proj_name or not ep_code:
        return jsonify({"error": "Episódio sem projeto ou ep_code"}), 400

    bulk_job_id = uuid.uuid4().hex[:8]
    bulk_state: dict = {"status": "running", "done": 0, "total": 0, "errors": []}

    with nq_lock:
        environments = list(nq.get("environments", []))
        new_elements = list(nq.get("new_elements", []))
    bulk_state["total"] = len(environments) + len(new_elements)

    # Store in a simple global dict for polling
    _bulk_img_state[bulk_job_id] = bulk_state

    def _run():
        cfg = _load_effective_cfg(proj_name)
        ep_chars = list(nq.get("characters", [])) + list(nq.get("figurantes", []))
        def _gen(item_type, items, subfolder):
            img_dir = PROJECTS_DIR / proj_name / "episodios" / ep_code / subfolder
            img_dir.mkdir(parents=True, exist_ok=True)
            for idx2, item in enumerate(items):
                safe_name = re.sub(r'[^\w\-]', '_', item.get("name", "item")[:40])
                dest = img_dir / f"{safe_name}.png"
                img_prompt = item.get("image_prompt") or item.get("description") or item.get("name", "")
                ref_paths = []
                existing = item.get("existing_ref") or item.get("generated_ref")
                if existing and (PROJECT_ROOT / existing).exists():
                    ref_paths.append(existing)
                # Match automático por nome contra imagens do projeto
                if proj_name and len(ref_paths) < 4:
                    auto = _auto_match_refs(item.get("name", ""), proj_name, exclude=ref_paths)
                    for a in auto:
                        if len(ref_paths) >= 4: break
                        ref_paths.append(a)
                # Personagens do episódio como refs de estilo visual do projeto
                for c in ep_chars:
                    if len(ref_paths) >= 4: break
                    if c not in ref_paths and (PROJECT_ROOT / c).exists():
                        ref_paths.append(c)
                try:
                    result = _dispatch_image(cfg, img_prompt, ref_paths or None)
                    url = result["images"][0]["url"]
                    _download_image(url, dest)
                    rel = str(dest.relative_to(PROJECT_ROOT))
                    with nq_lock:
                        nq[item_type][idx2]["generated_ref"] = rel
                    _save_queues()
                    _bulk_img_state[bulk_job_id]["done"] += 1
                except Exception as err:
                    _bulk_img_state[bulk_job_id]["errors"].append(str(err))
                    _bulk_img_state[bulk_job_id]["done"] += 1

        _gen("environments", environments, "ambiente")
        _gen("new_elements", new_elements, "elementos")
        _bulk_img_state[bulk_job_id]["status"] = "done"

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "bulk_job_id": bulk_job_id, "total": bulk_state["total"]})


@app.route("/nqueues/bulk-img-status/<bulk_job_id>")
def nq_bulk_img_status(bulk_job_id):
    state = _bulk_img_state.get(bulk_job_id)
    if not state:
        return jsonify({"error": "não encontrado"}), 404
    return jsonify(state)


@app.route("/nqueues/<int:nq_id>/analyze-environments", methods=["POST"])
def nq_analyze_environments(nq_id):
    """Analisa as cenas de um episódio via Claude CLI e gera environments + new_elements com prompts.
    Não gera imagens — apenas estrutura com prompts para geração futura.
    Roda em background; retorna analyse_job_id para polling.
    """
    with nq_lock:
        nq = next((q for q in named_queues if q["id"] == nq_id), None)
    if nq is None:
        return jsonify({"error": "Fila não encontrada"}), 404

    proj_name = nq.get("project", "")
    jobs      = nq.get("jobs", [])

    analyse_job_id = uuid.uuid4().hex[:8]
    _bulk_img_state[analyse_job_id] = {"status": "running", "phase_msg": "Analisando cenas…"}

    def _run():
        try:
            # Coletar contexto do projeto
            all_images, all_docs_content = [], []
            if proj_name:
                proj_dir = PROJECTS_DIR / proj_name
                IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
                for subfolder in ("imagens", "figurantes"):
                    img_dir = proj_dir / subfolder
                    if img_dir.exists():
                        for f in sorted(img_dir.iterdir()):
                            if f.is_file() and f.suffix.lower() in IMAGE_EXTS:
                                all_images.append(str(f.relative_to(PROJECT_ROOT)))
                docs_dir = proj_dir / "docs"
                if docs_dir.exists():
                    for f in sorted(docs_dir.iterdir()):
                        if f.is_file() and f.suffix in (".md", ".txt") and not f.name.startswith("_sys_"):
                            try:
                                all_docs_content.append(
                                    f"--- {f.name} ---\n{f.read_text(encoding='utf-8', errors='ignore')[:2000]}"
                                )
                            except Exception:
                                pass

            # Resumo das cenas
            scenes_summary = []
            for j in jobs:
                entry = f"- [{j.get('label','')}] prompt: {j.get('prompt','')[:200]}"
                if j.get("image_prompt"):
                    entry += f" | image_prompt: {j.get('image_prompt','')[:150]}"
                if j.get("ref_imgs"):
                    refs = j["ref_imgs"] if isinstance(j["ref_imgs"], list) else [j["ref_imgs"]]
                    entry += f" | refs: {', '.join(str(r) for r in refs[:4])}"
                scenes_summary.append(entry)

            images_list  = "\n".join(f"- {p}" for p in all_images) if all_images else "Nenhuma"
            scenes_block = "\n".join(scenes_summary) if scenes_summary else "Nenhuma cena"
            docs_block   = ("\n\nDocumentos do projeto:\n" + "\n\n".join(all_docs_content)[:3000]) if all_docs_content else ""

            analyse_prompt = f"""Você é um supervisor de arte de uma série animada. Analise as cenas abaixo e identifique:
1. Todos os AMBIENTES/LOCAÇÕES únicos que aparecem (corredor, sala de aula, laboratório, etc.)
2. Elementos visuais NOVOS que aparecem nas cenas mas NÃO têm imagem de referência na lista abaixo

Imagens disponíveis no projeto (paths exatos):
{images_list}

Cenas do episódio:
{scenes_block}
{docs_block}

Retorne SOMENTE um JSON válido (sem markdown) com este formato exato:
{{
  "environments": [
    {{
      "name": "Nome do Ambiente",
      "description": "Descrição visual detalhada do ambiente",
      "image_prompt": "Prompt completo em inglês para gerar imagem deste ambiente via IA (detalhes visuais, iluminação, perspectiva, 16:9)",
      "existing_ref": "path/exato/da/imagem.png ou null"
    }}
  ],
  "new_elements": [
    {{
      "name": "Nome do Elemento",
      "type": "character|object|creature",
      "description": "Descrição visual do elemento",
      "image_prompt": "Prompt completo em inglês para gerar imagem deste elemento via IA",
      "existing_ref": "path/exato/da/imagem.png ou null"
    }}
  ]
}}

Regras CRÍTICAS para existing_ref:
- Analise o nome e descrição de cada ambiente/elemento e compare com CADA arquivo da lista acima
- Se o nome do arquivo (sem extensão) corresponder ao ambiente ou elemento — mesmo parcialmente — coloque o PATH EXATO
- Exemplos: ambiente "Escola" → "projetos/PROJ/imagens/escola.png"; elemento "Pix-Z" → "projetos/PROJ/imagens/pix-z.png"
- Se não houver correspondência clara: null
- new_elements: APENAS elementos sem NENHUMA imagem correspondente na lista
- image_prompt deve ser detalhado e consistente com o estilo visual da série"""

            _env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
            proc = subprocess.run(
                ["/home/nmaldaner/.local/bin/claude", "-p", analyse_prompt],
                capture_output=True, text=True, timeout=180, env=_env
            )
            raw = proc.stdout.strip()
            if raw.startswith("```"):
                raw = re.sub(r"^```[a-z]*\n?", "", raw)
                raw = re.sub(r"\n?```$", "", raw)

            result = json.loads(raw)
            environments = result.get("environments", [])
            new_elements = result.get("new_elements", [])

            with nq_lock:
                nq["environments"] = environments
                nq["new_elements"]  = new_elements
            _save_queues()

            _bulk_img_state[analyse_job_id] = {
                "status": "done",
                "environments": environments,
                "new_elements": new_elements,
                "phase_msg": f"Concluído: {len(environments)} ambiente(s) · {len(new_elements)} elemento(s) novo(s)",
            }
        except Exception as e:
            _bulk_img_state[analyse_job_id] = {
                "status": "error",
                "error": str(e),
                "phase_msg": f"Erro: {e}",
            }

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "analyse_job_id": analyse_job_id})


@app.route("/nqueues/<int:nq_id>/recreate-prompts", methods=["POST"])
def nq_recreate_prompts(nq_id):
    """Re-analisa toda a história do episódio e refaz prompts (prompt, audio_text, image_prompt)
    de todas as cenas, mantendo labels, refs, seeds e configurações técnicas."""
    with nq_lock:
        nq = next((q for q in named_queues if q["id"] == nq_id), None)
    if nq is None:
        return jsonify({"error": "Fila não encontrada"}), 404

    proj_name = nq.get("project", "")
    jobs      = nq.get("jobs", [])
    if not jobs:
        return jsonify({"error": "Episódio sem cenas"}), 400

    recreate_job_id = uuid.uuid4().hex[:8]
    _bulk_img_state[recreate_job_id] = {"status": "running", "phase_msg": "Preparando re-análise…"}

    def _run():
        try:
            # Coletar contexto do projeto
            all_images, all_docs_content, all_audios = [], [], []
            proj_dir = PROJECTS_DIR / proj_name if proj_name else None
            if proj_dir and proj_dir.exists():
                IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
                for subfolder in ("imagens", "figurantes"):
                    img_dir = proj_dir / subfolder
                    if img_dir.exists():
                        for f in sorted(img_dir.iterdir()):
                            if f.is_file() and f.suffix.lower() in IMAGE_EXTS:
                                all_images.append(str(f.relative_to(PROJECT_ROOT)))
                docs_dir = proj_dir / "docs"
                if docs_dir.exists():
                    for f in sorted(docs_dir.iterdir()):
                        if f.is_file() and f.suffix in (".md", ".txt") and not f.name.startswith("_sys_"):
                            try:
                                all_docs_content.append(
                                    f"--- {f.name} ---\n{f.read_text(encoding='utf-8', errors='ignore')[:2000]}"
                                )
                            except Exception:
                                pass
                audios_dir = proj_dir / "audios"
                if audios_dir.exists():
                    for f in sorted(audios_dir.iterdir()):
                        if f.is_file():
                            all_audios.append(f.name)

            # Carregar system prompts do projeto
            _sys_video = _load_project_prompt(proj_dir, "_sys_video.md", DEFAULT_VIDEO_GUIDE) if proj_dir else DEFAULT_VIDEO_GUIDE
            _sys_audio = _load_project_prompt(proj_dir, "_sys_audio.md", DEFAULT_AUDIO_GUIDE) if proj_dir else DEFAULT_AUDIO_GUIDE

            images_list = "\n".join(f"- {p}" for p in all_images) if all_images else "Nenhuma"
            docs_block = ("\n\nDocumentos do projeto:\n" + "\n\n".join(all_docs_content)[:4000]) if all_docs_content else ""
            audios_block = ("\n\nÁudios disponíveis:\n" + "\n".join(f"- {a}" for a in all_audios)) if all_audios else ""

            # Descrição do episódio
            ep_desc = nq.get("description", "") or ""

            # Serializar cenas atuais para o Claude analisar
            scenes_current = []
            for i, j in enumerate(jobs):
                scenes_current.append({
                    "index": i,
                    "label": j.get("label", ""),
                    "prompt": j.get("prompt", ""),
                    "image_prompt": j.get("image_prompt", ""),
                    "audio_text": j.get("audio_text", ""),
                    "voice_id": j.get("voice_id", ""),
                    "audio_bg": j.get("audio_bg", ""),
                    "duration": j.get("duration", 5),
                    "ref_imgs": j.get("ref_imgs", []),
                    "task_type": j.get("task_type", "reference_to_video"),
                })

            _bulk_img_state[recreate_job_id]["phase_msg"] = "Claude analisando e recriando prompts…"

            recreate_prompt = f"""Você é um diretor de produção de série animada com IA (SkyReels V3).

Analise o episódio abaixo e REFAÇA os prompts de TODAS as cenas, melhorando qualidade e coerência.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DESCRIÇÃO DO EPISÓDIO:
{ep_desc}

Imagens de referência disponíveis:
{images_list}
{docs_block}
{audios_block}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GUIA DE VÍDEO:
{_sys_video}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GUIA DE ÁUDIO:
{_sys_audio}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CENAS ATUAIS (a serem melhoradas):
{json.dumps(scenes_current, ensure_ascii=False, indent=2)}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
INSTRUÇÕES:

Para CADA cena, refaça os seguintes campos:
1. "prompt" — prompt de vídeo CURTO em inglês (máx 2 frases): tipo de plano + ação + câmera
2. "audio_text" — fala/narração em português PROPORCIONAL à duração do vídeo:
   · 5s = máx 15-20 palavras · 8s = máx 25-35 palavras · 10s = máx 40-50 palavras
3. "image_prompt" — prompt de imagem em inglês para fal.ai/Flux
4. "voice_id" — extraia dos docs do projeto (tabela de casting). Use o voice_id do personagem que FALA na cena. NÃO invente voice_ids.
5. "audio_bg" — path da trilha (projetos/<nome>/audios/<arquivo>) ou "" se não houver

MANTENHA INALTERADOS: label, index, ref_imgs, task_type, duration, seed, offload, low_vram, resolution

Retorne SOMENTE um array JSON com os campos atualizados para cada cena, no formato:
[
  {{
    "index": 0,
    "prompt": "...",
    "image_prompt": "...",
    "audio_text": "...",
    "voice_id": "...",
    "audio_bg": ""
  }},
  ...
]

APENAS o JSON. Nada mais."""

            _env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
            proc = subprocess.run(
                ["/home/nmaldaner/.local/bin/claude", "-p", recreate_prompt, "--output-format", "json"],
                capture_output=True, text=True, timeout=360, env=_env
            )
            raw = proc.stdout.strip()
            if not raw:
                stderr_hint = (proc.stderr or "")[:300]
                raise ValueError(f"Claude CLI retornou vazio. stderr: {stderr_hint}")

            # Extrair JSON da resposta (pode vir dentro de result/content no output-format json)
            try:
                wrapper = json.loads(raw)
                if isinstance(wrapper, dict) and "result" in wrapper:
                    raw = wrapper["result"].strip()
                elif isinstance(wrapper, dict) and "content" in wrapper:
                    raw = wrapper["content"].strip()
                elif isinstance(wrapper, list):
                    # Já é o array direto
                    raw = json.dumps(wrapper)
            except (json.JSONDecodeError, TypeError):
                pass  # raw não é JSON wrapper, tentar limpar

            # Limpar markdown fences
            if raw.startswith("```"):
                raw = re.sub(r"^```[a-z]*\n?", "", raw)
                raw = re.sub(r"\n?```$", "", raw)

            # Extrair array JSON se tiver texto antes/depois
            bracket_start = raw.find("[")
            bracket_end   = raw.rfind("]")
            if bracket_start >= 0 and bracket_end > bracket_start:
                raw = raw[bracket_start:bracket_end + 1]

            updates = json.loads(raw)
            if not isinstance(updates, list):
                raise ValueError("Resposta não é um array")

            # Aplicar atualizações
            updated_count = 0
            with nq_lock:
                for upd in updates:
                    idx = upd.get("index")
                    if idx is None or idx < 0 or idx >= len(nq["jobs"]):
                        continue
                    job = nq["jobs"][idx]
                    for field in ("prompt", "image_prompt", "audio_text", "voice_id", "audio_bg"):
                        if field in upd:
                            job[field] = upd[field]
                    updated_count += 1
            _save_queues()

            _bulk_img_state[recreate_job_id] = {
                "status": "done",
                "phase_msg": f"Concluído: {updated_count} cena(s) atualizadas",
            }
        except Exception as e:
            _bulk_img_state[recreate_job_id] = {
                "status": "error",
                "error": str(e),
                "phase_msg": f"Erro: {e}",
            }

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "job_id": recreate_job_id})


@app.route("/nqueues/<int:nq_id>/characters", methods=["PATCH"])
def nq_patch_characters(nq_id):
    """Salva a seleção de personagens e figurantes do episódio."""
    data = request.get_json(force=True, silent=True) or {}
    with nq_lock:
        nq = next((q for q in named_queues if q["id"] == nq_id), None)
        if nq is None:
            return jsonify({"error": "Fila não encontrada"}), 404
        nq["characters"]  = data.get("characters", [])
        nq["figurantes"]  = data.get("figurantes", [])
    _save_queues()
    return jsonify({"ok": True})


@app.route("/nqueues/<int:nq_id>/analyze-characters", methods=["POST"])
def nq_analyze_characters(nq_id):
    """Claude analisa as cenas do episódio e identifica quais imagens do projeto
    correspondem a personagens/figurantes presentes. Roda em background."""
    with nq_lock:
        nq = next((q for q in named_queues if q["id"] == nq_id), None)
    if nq is None:
        return jsonify({"error": "Fila não encontrada"}), 404

    proj_name = nq.get("project", "")
    jobs      = nq.get("jobs", [])
    analyse_job_id = uuid.uuid4().hex[:8]
    _bulk_img_state[analyse_job_id] = {"status": "running", "phase_msg": "Identificando personagens…"}

    def _run():
        try:
            # Coletar imagens do projeto
            all_images, figurante_images, docs_content = [], [], []
            if proj_name:
                proj_dir = PROJECTS_DIR / proj_name
                for f in sorted((proj_dir / "imagens").iterdir()) if (proj_dir / "imagens").exists() else []:
                    if f.is_file():
                        all_images.append(str(f.relative_to(PROJECT_ROOT)))
                for f in sorted((proj_dir / "figurantes").iterdir()) if (proj_dir / "figurantes").exists() else []:
                    if f.is_file():
                        figurante_images.append(str(f.relative_to(PROJECT_ROOT)))
                for f in sorted((proj_dir / "docs").iterdir()) if (proj_dir / "docs").exists() else []:
                    if f.is_file() and f.suffix in (".md", ".txt") and not f.name.startswith("_sys_"):
                        try:
                            docs_content.append(f"--- {f.name} ---\n{f.read_text(encoding='utf-8', errors='ignore')[:1500]}")
                        except Exception:
                            pass

            # Refs já usadas nas cenas
            used_refs: set = set()
            for j in jobs:
                refs = j.get("ref_imgs", [])
                if isinstance(refs, list):
                    used_refs.update(refs)
                elif refs:
                    used_refs.add(str(refs))

            # Resumo das cenas
            scenes = "\n".join(
                f"- [{j.get('label','')}] {j.get('image_prompt') or j.get('prompt','')[:150]}"
                for j in jobs
            ) or "Nenhuma cena"

            imgs_list = "\n".join(f"- {p}" for p in all_images) or "Nenhuma"
            figs_list = "\n".join(f"- {p}" for p in figurante_images) or "Nenhuma"
            docs_str  = ("\n\n" + "\n\n".join(docs_content)[:3000]) if docs_content else ""

            prompt = f"""Você é supervisor de arte de uma série animada. Analise as cenas do episódio e identifique quais imagens de personagens e figurantes estão presentes.

Imagens de personagens do projeto (pasta imagens/):
{imgs_list}

Imagens de figurantes do projeto (pasta figurantes/):
{figs_list}

Cenas do episódio:
{scenes}
{docs_str}

Retorne SOMENTE JSON válido (sem markdown):
{{
  "characters": ["path/da/imagem1.png", "path/da/imagem2.png"],
  "figurantes": ["path/do/figurante1.png"]
}}

Regras:
- "characters": paths das imagens de personagens (de imagens/) que APARECEM nas cenas deste episódio
- "figurantes": paths das imagens de figurantes (de figurantes/) relevantes para este episódio
- Use APENAS paths exatos da lista acima — não invente
- Se um personagem não aparece em nenhuma cena, não inclua
- Se não há figurantes relevantes: "figurantes": []"""

            _env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
            proc = subprocess.run(
                ["/home/nmaldaner/.local/bin/claude", "-p", prompt],
                capture_output=True, text=True, timeout=120, env=_env
            )
            raw = proc.stdout.strip()
            if raw.startswith("```"):
                raw = re.sub(r"^```[a-z]*\n?", "", raw)
                raw = re.sub(r"\n?```$", "", raw)
            result = json.loads(raw)
            characters = result.get("characters", [])
            figurantes = result.get("figurantes", [])

            with nq_lock:
                nq["characters"] = characters
                nq["figurantes"] = figurantes
            _save_queues()

            _bulk_img_state[analyse_job_id] = {
                "status": "done",
                "characters": characters,
                "figurantes": figurantes,
                "phase_msg": f"Concluído: {len(characters)} personagem(ns) · {len(figurantes)} figurante(s)",
            }
        except Exception as e:
            _bulk_img_state[analyse_job_id] = {"status": "error", "error": str(e), "phase_msg": f"Erro: {e}"}

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "analyse_job_id": analyse_job_id})


@app.route("/nqueues/<int:nq_id>/description", methods=["PATCH"])
def nq_patch_description(nq_id):
    """Salva ou atualiza o texto de descrição/sinopse do episódio no NQ."""
    data = request.get_json(force=True, silent=True) or {}
    text = data.get("description", "")
    with nq_lock:
        nq = next((q for q in named_queues if q["id"] == nq_id), None)
        if nq is None:
            return jsonify({"error": "Fila não encontrada"}), 404
        nq["description"] = text
    _save_queues()
    return jsonify({"ok": True})


@app.route("/nqueues/<int:nq_id>/regenerate-prompts", methods=["POST"])
def nq_regenerate_prompts(nq_id):
    """Re-gera os prompts do episódio usando a descrição salva no NQ.
    Roda as 2 fases do generate_episode em background.
    Quando concluído, SUBSTITUI os jobs do NQ pelos novos (preserva environments/characters).
    """
    with nq_lock:
        nq = next((q for q in named_queues if q["id"] == nq_id), None)
    if nq is None:
        return jsonify({"error": "Fila não encontrada"}), 404

    proj_name   = nq.get("project", "")
    description = nq.get("description", "")
    if not description:
        return jsonify({"error": "Episódio sem descrição. Adicione um texto antes de refazer."}), 400

    regen_job_id = uuid.uuid4().hex[:8]
    _bulk_img_state[regen_job_id] = {"status": "running", "phase_msg": "Fase 1: identificando ambientes…"}

    def _run():
        try:
            proj_dir = PROJECTS_DIR / proj_name if proj_name else None
            all_images, all_audios, all_docs = [], [], []
            if proj_dir and proj_dir.exists():
                for f in sorted((proj_dir / "imagens").iterdir()) if (proj_dir / "imagens").exists() else []:
                    if f.is_file(): all_images.append(str(f.relative_to(PROJECT_ROOT)))
                for f in sorted((proj_dir / "audios").iterdir()) if (proj_dir / "audios").exists() else []:
                    if f.is_file(): all_audios.append(f.name)
                for f in sorted((proj_dir / "docs").iterdir()) if (proj_dir / "docs").exists() else []:
                    if f.is_file() and f.suffix in (".md", ".txt") and not f.name.startswith("_sys_"):
                        try:
                            all_docs.append(f"--- {f.name} ---\n{f.read_text(encoding='utf-8', errors='ignore')[:2000]}")
                        except Exception:
                            pass

            _env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
            proc = None

            # ── Fase 1 ──────────────────────────────────────────────────────
            _bulk_img_state[regen_job_id]["phase_msg"] = "Fase 1: identificando ambientes e elementos…"
            _fase1_template = _load_project_prompt(proj_dir, "_sys_fase1.md", DEFAULT_PHASE1_TEMPLATE) if proj_dir else DEFAULT_PHASE1_TEMPLATE
            p1 = _build_phase1_prompt(description, all_images, all_docs, _fase1_template)
            proc = subprocess.run(["/home/nmaldaner/.local/bin/claude", "-p", p1],
                                  capture_output=True, text=True, timeout=180, env=_env)
            raw1 = proc.stdout.strip()
            if raw1.startswith("```"):
                raw1 = re.sub(r"^```[a-z]*\n?", "", raw1); raw1 = re.sub(r"\n?```$", "", raw1)
            p1r  = json.loads(raw1)
            environments = p1r.get("environments", [])
            new_elements = p1r.get("new_elements", [])

            amb_map = {env["name"].lower(): env["existing_ref"] for env in environments if env.get("existing_ref")}

            # ── Fase 2 ──────────────────────────────────────────────────────
            _bulk_img_state[regen_job_id]["phase_msg"] = "Fase 2: gerando cenas…"
            task_type  = nq.get("jobs", [{}])[0].get("task_type", "reference_to_video") if nq.get("jobs") else "reference_to_video"
            resolution = nq.get("jobs", [{}])[0].get("resolution", "720P") if nq.get("jobs") else "720P"
            duration   = nq.get("jobs", [{}])[0].get("duration", 5) if nq.get("jobs") else 5

            _sys_template = _load_project_prompt(proj_dir, "_sys_episodio.md", _load_system_prompt()) if proj_dir else _load_system_prompt()
            imgs_list = "\n".join(f"- {p}" for p in all_images) or "Nenhuma"
            env_section = ""
            if environments:
                env_section = "\n\nMAPA DE AMBIENTES:\n"
                for env in environments:
                    ref = amb_map.get(env["name"].lower()) or env.get("existing_ref")
                    env_section += f"- {env['name']}: {env.get('description','')}\n"
                    if ref: env_section += f"  → REFERÊNCIA: {ref}\n"
            resources = f"\nImagens de referência:\n{imgs_list}\n"
            if all_audios: resources += "\nÁudios:\n" + "\n".join(f"- {a}" for a in all_audios) + "\n"
            if all_docs: resources += "\nDocumentos:\n" + "\n\n".join(all_docs) + "\n"
            if env_section: resources += env_section

            json_tpl = (
                f'{{\n  "label": "Cena 01 — Título",\n  "task_type": "{task_type}",\n'
                f'  "prompt": "...",\n  "image_prompt": "...",\n  "audio_text": "...",\n'
                f'  "voice_id": "",\n  "audio_bg": "",\n  "resolution": "{resolution}",\n'
                f'  "duration": <5-8>,\n  "num_inference_steps": 4,\n  "seed": <1000-9999>,\n'
                f'  "offload": false,\n  "low_vram": false,\n  "ref_imgs": []\n}}'
            )
            p2 = _sys_template.format(description=description, resources=resources,
                                       task_type=task_type, resolution=resolution,
                                       duration=duration, json_template=json_tpl)
            proc = subprocess.run(["/home/nmaldaner/.local/bin/claude", "-p", p2],
                                  capture_output=True, text=True, timeout=360, env=_env)
            raw2 = proc.stdout.strip()
            if raw2.startswith("```"):
                raw2 = re.sub(r"^```[a-z]*\n?", "", raw2); raw2 = re.sub(r"\n?```$", "", raw2)
            new_jobs_data = json.loads(raw2)
            if not isinstance(new_jobs_data, list): raise ValueError("Resposta não é array")

            # ── Substituir jobs no NQ ────────────────────────────────────────
            with nq_lock:
                nq_now = next((q for q in named_queues if q["id"] == nq_id), None)
                if nq_now:
                    new_jobs = []
                    for i, jd in enumerate(new_jobs_data):
                        if not jd.get("task_type"): continue
                        new_jobs.append({
                            "id": _next_job_id(), "nq_id": nq_id, "nq_job_index": i,
                            "status": "idle", "output_video": "",
                            "label": jd.get("label") or f"{jd['task_type']} — seed {jd.get('seed',42)}",
                            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                            **{k: v for k, v in jd.items() if k not in ("id","nq_id","status")},
                        })
                    nq_now["jobs"]         = new_jobs
                    nq_now["environments"] = environments
                    nq_now["new_elements"] = new_elements
            _save_queues()

            _bulk_img_state[regen_job_id] = {
                "status": "done",
                "phase_msg": f"Concluído! {len(new_jobs)} cenas · {len(environments)} ambiente(s)",
            }
        except Exception as e:
            _bulk_img_state[regen_job_id] = {"status": "error", "error": str(e), "phase_msg": f"Erro: {e}"}

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "regen_job_id": regen_job_id})


@app.route("/nqueues/<int:nq_id>/project-link", methods=["DELETE"])
def unlink_nq_from_project(nq_id):
    """Remove a associação do episódio com o projeto sem excluir a fila."""
    with nq_lock:
        nq = next((q for q in named_queues if q["id"] == nq_id), None)
        if nq is None:
            return jsonify({"error": "Fila não encontrada"}), 404
        if nq["status"] == "running":
            return jsonify({"error": "Não é possível desvincular uma fila em execução"}), 400
        nq.pop("project", None)
    _save_queues()
    return jsonify({"ok": True})


@app.route("/nqueues/<int:nq_id>/run", methods=["POST"])
def run_nq_route(nq_id):
    ok = run_named_queue(nq_id)
    if not ok:
        return jsonify({"error": "Fila não encontrada, já em execução, ou sem cenas pendentes"}), 400
    return jsonify({"ok": True})


@app.route("/nqueues/<int:nq_id>/jobs/<int:job_id>/run", methods=["POST"])
def run_nq_job_route(nq_id, job_id):
    ok = run_single_nq_job_fn(nq_id, job_id)
    if not ok:
        return jsonify({"error": "Cena não encontrada ou já em execução"}), 400
    return jsonify({"ok": True})


@app.route("/nqueues/<int:nq_id>/reset", methods=["POST"])
def reset_nq_route(nq_id):
    """Repetir do erro: reset error+idle jobs (keep done), run from first error."""
    with nq_lock:
        nq = next((q for q in named_queues if q["id"] == nq_id), None)
        if nq is None:
            return jsonify({"error": "Fila não encontrada"}), 404
        if nq["status"] == "running":
            return jsonify({"error": "Não é possível operar uma fila em execução"}), 400
        for j in nq["jobs"]:
            if j["status"] in ("error", "idle"):
                j["status"] = "idle"
                j["output_video"] = ""
                j.pop("started_at", None)
                j.pop("finished_at", None)
    _save_queues()
    ok = run_named_queue(nq_id)
    if not ok:
        return jsonify({"error": "Sem cenas a executar"}), 400
    return jsonify({"ok": True})


@app.route("/nqueues/<int:nq_id>/restart", methods=["POST"])
def restart_nq_route(nq_id):
    """Reiniciar do zero: reset ALL jobs (including done), run from beginning."""
    with nq_lock:
        nq = next((q for q in named_queues if q["id"] == nq_id), None)
        if nq is None:
            return jsonify({"error": "Fila não encontrada"}), 404
        if nq["status"] == "running":
            return jsonify({"error": "Não é possível operar uma fila em execução"}), 400
        for j in nq["jobs"]:
            j["status"] = "idle"
            j["output_video"] = ""
            j.pop("started_at", None)
            j.pop("finished_at", None)
    _save_queues()
    ok = run_named_queue(nq_id)
    if not ok:
        return jsonify({"error": "Sem cenas a executar"}), 400
    return jsonify({"ok": True})


def _strip_audio_prefix(text: str) -> str:
    """Remove prefixos de nome de personagem do audio_text antes de enviar ao ElevenLabs.
    Ex: 'Valen: texto' → 'texto', '[Lumi] texto' → 'texto', '(narração) texto' → 'texto'
    """
    import re as _re
    # Padrão: "[Nome] texto" ou "[Nome]: texto"
    text = _re.sub(r'^\s*\[[^\]]{1,40}\]\s*:?\s*', '', text)
    # Padrão: "Nome: texto" ou "Nome — texto" ou "Nome - texto" (hífen só com espaço antes)
    text = _re.sub(r'^\s*[A-ZÀ-Ú][a-zA-ZÀ-ú ]{1,30}\s*(?::|—| - )\s*', '', text)
    # Padrão: "(narração)", "(narrador)", "(voz off)", etc.
    text = _re.sub(r'^\s*\([^)]{1,30}\)\s*', '', text)
    return text.strip()


def _audio_duration(path: Path) -> float:
    """Returns audio duration in seconds using ffprobe."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(path)],
            capture_output=True, text=True, timeout=10
        )
        data = json.loads(r.stdout) if r.returncode == 0 else {}
        return float(data.get("format", {}).get("duration", 0))
    except Exception:
        return 0.0


def _video_info(path):
    """Returns (has_audio, duration_seconds) for a video file."""
    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_streams", "-show_format", str(path)],
        capture_output=True, text=True, timeout=15
    )
    data = json.loads(r.stdout) if r.returncode == 0 else {}
    has_aud = any(s.get("codec_type") == "audio" for s in data.get("streams", []))
    duration = float(data.get("format", {}).get("duration", 0))
    return has_aud, duration


def _mix_audio_scene(video_path: Path, speech_path: Path = None,
                     bg_path: Path = None, bg_volume: float = 0.28) -> bool:
    """Mixes speech and/or background audio into a video, replacing it in-place.
    - speech_path: narração/diálogo (volume 100%)
    - bg_path: trilha de fundo (volume bg_volume, padrão 28%)
    - Usa -map 0:v para sempre descartar o áudio existente no vídeo.
    """
    if not speech_path and not bg_path:
        return False
    tmp = video_path.with_name(video_path.stem + "_mixed_tmp.mp4")
    try:
        cmd = ["ffmpeg", "-y", "-i", str(video_path)]
        if speech_path:
            cmd += ["-i", str(speech_path)]
        if bg_path:
            cmd += ["-i", str(bg_path)]

        if speech_path and bg_path:
            si, bi = 1, 2
            fc = (
                f"[{si}:a]aresample=44100,volume=1.0[speech];"
                f"[{bi}:a]aresample=44100,volume={bg_volume}[bg];"
                f"[speech][bg]amix=inputs=2:dropout_transition=0[a]"
            )
            cmd += ["-filter_complex", fc,
                    "-map", "0:v", "-map", "[a]",
                    "-c:v", "copy", "-c:a", "aac", "-ar", "44100", "-ac", "2",
                    "-shortest", str(tmp)]
        elif speech_path:
            cmd += ["-map", "0:v", "-map", "1:a",
                    "-c:v", "copy", "-c:a", "aac", "-ar", "44100", "-ac", "2",
                    "-shortest", str(tmp)]
        else:  # bg only
            fc = f"[1:a]aresample=44100,volume={bg_volume}[a]"
            cmd += ["-filter_complex", fc,
                    "-map", "0:v", "-map", "[a]",
                    "-c:v", "copy", "-c:a", "aac", "-ar", "44100", "-ac", "2",
                    "-shortest", str(tmp)]

        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode == 0 and tmp.exists():
            tmp.replace(video_path)
            return True
        if tmp.exists():
            tmp.unlink()
        return False
    except Exception:
        if tmp.exists():
            tmp.unlink()
        return False


# Backwards-compat alias
def _mix_audio_into_video(video_path: Path, audio_path: Path) -> bool:
    return _mix_audio_scene(video_path, speech_path=audio_path)


@app.route("/nqueues/<int:nq_id>/finalize", methods=["POST"])
def finalize_nq_route(nq_id):
    with nq_lock:
        nq = next((q for q in named_queues if q["id"] == nq_id), None)
        if nq is None:
            return jsonify({"error": "Fila não encontrada"}), 404
        videos = [
            PROJECT_ROOT / j["output_video"]
            for j in nq["jobs"]
            if j.get("status") == "done" and j.get("output_video")
        ]
        nq_name = nq["name"]

    if not videos:
        return jsonify({"error": "Nenhuma cena concluída para finalizar"}), 400

    missing = [str(v) for v in videos if not v.exists()]
    if missing:
        return jsonify({"error": f"Arquivo(s) não encontrado(s): {', '.join(missing)}"}), 400

    out_dir = PROJECT_ROOT / "result" / "finalized"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y-%m-%d_%H-%M-%S")
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in nq_name)
    out_path = out_dir / f"{safe_name}_{ts}.mp4"

    # Check audio presence per video
    infos = [_video_info(v) for v in videos]
    any_audio = any(has_aud for has_aud, _ in infos)

    import tempfile

    if not any_audio:
        # All silent — simple concat copy
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            for v in videos:
                f.write(f"file '{v}'\n")
            list_path = f.name
        try:
            result = subprocess.run(
                ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                 "-i", list_path, "-c", "copy", str(out_path)],
                capture_output=True, text=True, timeout=600)
        finally:
            os.unlink(list_path)
    else:
        # Mixed audio/silent — filter_complex to normalize all streams then concat
        cmd = ["ffmpeg", "-y"]
        for v in videos:
            cmd.extend(["-i", str(v)])

        # Determine target resolution (most common among done videos)
        from collections import Counter
        res_list = []
        for v in videos:
            r = subprocess.run(
                ["ffprobe", "-v", "quiet", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height", "-of", "csv=p=0", str(v)],
                capture_output=True, text=True, timeout=10)
            res_list.append(r.stdout.strip())
        target_res = Counter(res_list).most_common(1)[0][0]
        tw, th = target_res.split(",")

        filter_parts = []
        concat_inputs = ""
        for i, (has_aud, dur) in enumerate(infos):
            filter_parts.append(
                f"[{i}:v]scale={tw}:{th}:force_original_aspect_ratio=decrease,"
                f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2:black,"
                f"setpts=PTS-STARTPTS[v{i}]"
            )
            if has_aud:
                filter_parts.append(
                    f"[{i}:a]aresample=44100,aformat=channel_layouts=stereo,"
                    f"asetpts=PTS-STARTPTS[a{i}]"
                )
            else:
                filter_parts.append(
                    f"anullsrc=r=44100:cl=stereo,atrim=duration={dur:.3f},"
                    f"asetpts=PTS-STARTPTS[a{i}]"
                )
            concat_inputs += f"[v{i}][a{i}]"

        n = len(videos)
        filter_parts.append(f"{concat_inputs}concat=n={n}:v=1:a=1[v][a]")
        cmd += [
            "-filter_complex", ";".join(filter_parts),
            "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
            "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "128k",
            str(out_path)
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)

    if result.returncode != 0:
        return jsonify({"error": f"ffmpeg falhou: {result.stderr[-500:]}"}), 500

    rel_path = str(out_path.relative_to(PROJECT_ROOT))
    return jsonify({"ok": True, "output_video": rel_path, "scene_count": len(videos)})


@app.route("/nqueues/<int:nq_id>/mix-audio", methods=["POST"])
def nq_mix_audio(nq_id):
    """Mixes input_audio and/or audio_bg into done scene videos.
    - Skips jobs with no audio sources.
    - Always re-mixes if audio_bg is set (discards existing audio track via -map 0:v).
    - Skips jobs whose video already has audio when only input_audio is set (no bg change).
    """
    with nq_lock:
        nq = next((q for q in named_queues if q["id"] == nq_id), None)
        if nq is None:
            return jsonify({"error": "Fila não encontrada"}), 404
        jobs = nq["jobs"]

    mixed, skipped, errors = 0, 0, []
    for job in jobs:
        if job.get("status") != "done" or job.get("task_type") == "talking_avatar":
            skipped += 1
            continue
        output_video = job.get("output_video", "")
        sp_str = job.get("input_audio", "")
        bg_str = job.get("audio_bg", "")
        if not output_video or (not sp_str and not bg_str):
            skipped += 1
            continue
        vpath = PROJECT_ROOT / output_video
        if not vpath.exists():
            errors.append(f"{job.get('label','?')}: vídeo não encontrado")
            continue
        sp = (PROJECT_ROOT / sp_str) if sp_str else None
        bg = (PROJECT_ROOT / bg_str) if bg_str else None
        sp = sp if (sp and sp.exists()) else None
        bg = bg if (bg and bg.exists()) else None
        if not sp and not bg:
            errors.append(f"{job.get('label','?')}: arquivo(s) de áudio não encontrado(s)")
            continue
        # Se só tem speech e o vídeo já tem áudio: pula (já mixado)
        # Se tem audio_bg: sempre re-mixa (pode ter mudado a trilha)
        if not bg:
            has_aud, _ = _video_info(vpath)
            if has_aud:
                skipped += 1
                continue
        bg_vol = float(job.get("audio_bg_volume", 0.28))
        ok = _mix_audio_scene(vpath, speech_path=sp, bg_path=bg, bg_volume=bg_vol)
        if ok:
            mixed += 1
        else:
            errors.append(f"{job.get('label','?')}: ffmpeg falhou")

    return jsonify({"ok": True, "mixed": mixed, "skipped": skipped, "errors": errors})


@app.route("/nqueues/<int:nq_id>/set-audio-bg", methods=["POST"])
def nq_set_audio_bg(nq_id):
    """Define audio_bg (e volume) em todos os jobs da fila, ou limpa se audio_bg vazio."""
    data = request.get_json(force=True)
    audio_bg    = data.get("audio_bg", "").strip()
    bg_volume   = round(float(data.get("bg_volume", 0.28)), 3)
    only_silent = bool(data.get("only_silent", False))  # True = só jobs sem input_audio
    with nq_lock:
        nq = next((q for q in named_queues if q["id"] == nq_id), None)
        if nq is None:
            return jsonify({"error": "Fila não encontrada"}), 404
        updated = 0
        for job in nq["jobs"]:
            if only_silent and job.get("input_audio"):
                continue  # pula jobs que já têm narração
            job["audio_bg"]        = audio_bg
            job["audio_bg_volume"] = bg_volume if audio_bg else 0.28
            updated += 1
    _save_queues()
    return jsonify({"ok": True, "updated": updated, "audio_bg": audio_bg, "bg_volume": bg_volume})


# ─── Config global ───────────────────────────────────────────

@app.route("/config", methods=["GET"])
def get_global_config():
    return jsonify(_load_global_config())


@app.route("/config", methods=["POST"])
def save_global_config():
    data = request.get_json(force=True)
    GLOBAL_CONFIG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    return jsonify({"ok": True})


# ─── Projetos ────────────────────────────────────────────────

@app.route("/projects", methods=["GET"])
def list_projects():
    projs = []
    if PROJECTS_DIR.exists():
        for d in sorted(PROJECTS_DIR.iterdir()):
            if d.is_dir():
                projs.append({"name": d.name, "path": str(d.relative_to(PROJECT_ROOT))})
    return jsonify(projs)


@app.route("/projects", methods=["POST"])
def create_project():
    data = request.get_json(force=True)
    name = re.sub(r'[^a-zA-Z0-9_\- ]', '', data.get("name", "")).strip()
    if not name:
        return jsonify({"error": "Nome inválido"}), 400
    proj_dir = PROJECTS_DIR / name
    if proj_dir.exists():
        return jsonify({"error": "Projeto já existe"}), 409
    for sub in ("imagens", "audios", "docs", "episodios", "temp", "figurantes"):
        (proj_dir / sub).mkdir(parents=True, exist_ok=True)
    _ensure_project_prompts(proj_dir)
    return jsonify({"ok": True, "name": name})


@app.route("/projects/<name>", methods=["GET"])
def get_project(name):
    proj_dir = PROJECTS_DIR / name
    if not proj_dir.exists():
        return jsonify({"error": "Projeto não encontrado"}), 404
    _ensure_project_prompts(proj_dir)
    folders = {}
    for sub in ("imagens", "audios", "trilha", "docs", "episodios", "temp", "figurantes"):
        sub_dir = proj_dir / sub
        sub_dir.mkdir(exist_ok=True)
        files = []
        for f in sorted(sub_dir.iterdir()):
            if f.is_file():
                files.append({
                    "name": f.name,
                    "size": f.stat().st_size,
                    "path": str(f.relative_to(PROJECT_ROOT))
                })
        folders[sub] = files
    with nq_lock:
        episodes = [
            {
                "id":               q["id"],
                "name":             q["name"],
                "ep_code":          q.get("ep_code", ""),
                "total":            len(q.get("jobs", [])),
                "done":             sum(1 for j in q.get("jobs", []) if j["status"] == "done"),
                "running":          any(j["status"] == "running" for j in q.get("jobs", [])),
                "error":            any(j["status"] == "error" for j in q.get("jobs", [])),
                "status":           q.get("status", "idle"),
                "has_environments": bool(q.get("environments") or q.get("new_elements")),
                "has_characters":   bool(q.get("characters") or q.get("figurantes")),
            }
            for q in named_queues
            if q.get("project") == name
        ]
    return jsonify({"name": name, "folders": folders, "episodes": episodes})


@app.route("/projects/<name>/voices")
def get_project_voices(name):
    """Retorna o mapa personagem → voice_id extraído dos docs do projeto."""
    voices = _parse_project_voices(name)
    return jsonify({"project": name, "voices": voices})


@app.route("/projects/<name>/upload/<subfolder>", methods=["POST"])
def upload_project_file(name, subfolder):
    if subfolder not in ("imagens", "audios", "trilha", "docs", "episodios", "temp", "figurantes"):
        return jsonify({"error": "Pasta inválida"}), 400
    proj_dir = PROJECTS_DIR / name / subfolder
    proj_dir.mkdir(parents=True, exist_ok=True)
    if not (PROJECTS_DIR / name).exists():
        return jsonify({"error": "Projeto não encontrado"}), 404
    uploaded = []
    for file in request.files.getlist("files"):
        fname = secure_filename(file.filename)
        if not fname:
            continue
        file.save(str(proj_dir / fname))
        uploaded.append(fname)
    return jsonify({"ok": True, "uploaded": uploaded})


@app.route("/projects/<name>/files/<subfolder>/<filename>", methods=["DELETE"])
def delete_project_file(name, subfolder, filename):
    if subfolder not in ("imagens", "audios", "trilha", "docs", "episodios", "temp", "figurantes"):
        return jsonify({"error": "Pasta inválida"}), 400
    fpath = PROJECTS_DIR / name / subfolder / filename
    if not fpath.exists():
        return jsonify({"error": "Arquivo não encontrado"}), 404
    fpath.unlink()
    return jsonify({"ok": True})


@app.route("/projects/<name>", methods=["DELETE"])
def delete_project(name):
    import shutil
    proj_dir = PROJECTS_DIR / name
    if not proj_dir.exists():
        return jsonify({"error": "Projeto não encontrado"}), 404
    shutil.rmtree(str(proj_dir))
    return jsonify({"ok": True})


@app.route("/projects/<name>/config", methods=["GET"])
def get_project_config(name):
    cfg_file = PROJECTS_DIR / name / "config.json"
    if cfg_file.exists():
        return jsonify(json.loads(cfg_file.read_text()))
    return jsonify({})


@app.route("/projects/<name>/config", methods=["POST"])
def save_project_config(name):
    proj_dir = PROJECTS_DIR / name
    if not proj_dir.exists():
        return jsonify({"error": "Projeto não encontrado"}), 404
    data = request.get_json(force=True)
    (proj_dir / "config.json").write_text(json.dumps(data, ensure_ascii=False, indent=2))
    return jsonify({"ok": True})


@app.route("/system-prompt/episode", methods=["GET"])
def get_system_prompt():
    return jsonify({"prompt": _load_system_prompt(), "default": DEFAULT_SYSTEM_PROMPT})


@app.route("/system-prompt/episode", methods=["POST"])
def save_system_prompt():
    data = request.get_json(force=True)
    text = data.get("prompt", "").strip()
    if not text:
        return jsonify({"error": "Prompt não pode ser vazio"}), 400
    SYSTEM_PROMPT_FILE.write_text(text, encoding="utf-8")
    return jsonify({"ok": True})


@app.route("/system-prompt/episode/reset", methods=["POST"])
def reset_system_prompt():
    if SYSTEM_PROMPT_FILE.exists():
        SYSTEM_PROMPT_FILE.unlink()
    return jsonify({"ok": True, "prompt": DEFAULT_SYSTEM_PROMPT})


def _build_phase1_prompt(description: str, image_paths: list, docs_content: list,
                         template: str | None = None) -> str:
    """Prompt para Fase 1: Claude identifica ambientes e elementos novos necessários."""
    images_list = "\n".join(f"- {p}" for p in image_paths) if image_paths else "Nenhuma"
    docs_str = ("\n\nDocumentos do projeto:\n" + "\n\n".join(docs_content)[:3000]) if docs_content else ""
    tpl = template or DEFAULT_PHASE1_TEMPLATE
    return tpl.format(images_list=images_list, docs_str=docs_str, description=description)


@app.route("/projects/<name>/generate-episode", methods=["POST"])
def generate_episode_prompts(name):
    data = request.get_json(force=True)
    description = data.get("description", "") or data.get("concept", "")
    doc_title   = data.get("doc_title", "").strip()
    task_type   = data.get("task_type", "reference_to_video")
    resolution  = data.get("resolution", "720P")
    duration    = int(data.get("duration", 5))
    ref_imgs    = data.get("ref_imgs", [])

    # Garantir que os arquivos de system prompt existam no projeto
    proj_dir  = PROJECTS_DIR / name
    if proj_dir.exists():
        _ensure_project_prompts(proj_dir)

    # Salvar descrição em docs/ antes de gerar
    saved_doc = None
    doc_path  = None
    if proj_dir.exists() and description:
        docs_dir = proj_dir / "docs"
        docs_dir.mkdir(exist_ok=True)
        safe_title = re.sub(r'[^\w\-_ ]', '', doc_title or "descricao_episodio").strip() or "descricao_episodio"
        doc_path = docs_dir / f"{safe_title}.md"
        doc_path.write_text(description, encoding="utf-8")
        saved_doc = str(doc_path.relative_to(PROJECT_ROOT))

    # Coletar TODOS os recursos para contexto de consistência
    all_images, all_audios, all_docs_content = [], [], []

    # Imagens: pasta do projeto
    img_proj_dir = proj_dir / "imagens"
    if img_proj_dir.exists():
        for f in sorted(img_proj_dir.iterdir()):
            if f.is_file():
                all_images.append(str(f.relative_to(PROJECT_ROOT)))

    if proj_dir.exists():
        for f in sorted((proj_dir / "audios").iterdir()) if (proj_dir / "audios").exists() else []:
            if f.is_file():
                all_audios.append(f.name)
        for f in sorted((proj_dir / "docs").iterdir()) if (proj_dir / "docs").exists() else []:
            if f.is_file() and f.suffix in (".md", ".txt") and f != doc_path and not f.name.startswith("_sys_"):
                try:
                    all_docs_content.append(f"--- {f.name} ---\n{f.read_text(encoding='utf-8', errors='ignore')[:2000]}")
                except Exception:
                    pass

    # Usar ref_imgs selecionadas (vêm de uploads/) ou todas as imagens de uploads/
    effective_imgs = ref_imgs if ref_imgs else all_images
    images_list = "\n".join(f"- {p}" for p in effective_imgs) if effective_imgs else "Nenhuma"

    resources_section = f"\nImagens de referência do projeto (use os paths exatos nas cenas):\n{images_list}\n"
    if all_audios:
        resources_section += f"\nÁudios disponíveis no projeto:\n" + "\n".join(f"- {a}" for a in all_audios) + "\n"
    if all_docs_content:
        resources_section += f"\nDocumentos do projeto (contexto de consistência):\n" + "\n\n".join(all_docs_content) + "\n"

    json_template = (
        f'{{\n'
        f'  "label": "Cena 01 — Título curto",\n'
        f'  "task_type": "{task_type}",\n'
        f'  "prompt": "Cinematic video description in English — camera movement, lighting, characters, action...",\n'
        f'  "image_prompt": "Flux/fal.ai image prompt in English — characters, environment, art style, colors...",\n'
        f'  "audio_text": "Narração ou diálogos em português para esta cena (ou string vazia se silenciosa)",\n'
        f'  "voice_id": "ElevenLabs voice_id do personagem que fala nesta cena (extraia dos docs do projeto; vazio se narração genérica)",\n'
        f'  "audio_bg": "path para trilha de fundo do projeto (projetos/<nome>/audios/<arquivo>.mp3) ou string vazia",\n'
        f'  "resolution": "{resolution}",\n'
        f'  "duration": <duração em segundos — OBRIGATÓRIO entre 5 e 8, nunca mais que 10>,\n'
        f'  "num_inference_steps": 4,\n'
        f'  "seed": <número entre 1000 e 9999>,\n'
        f'  "offload": false,\n'
        f'  "low_vram": false,\n'
        f'  "ref_imgs": ["uploads/personagem1.jpg", "uploads/ambiente.png"]  // MÁXIMO 4 — use 2-3 idealmente\n'
        f'}}'
    )

    # Fase 2 usará json_template e template do sistema; per-project override em docs/_sys_episodio.md
    _sys_template  = _load_project_prompt(proj_dir, "_sys_episodio.md", _load_system_prompt())
    _fase1_template = _load_project_prompt(proj_dir, "_sys_fase1.md", DEFAULT_PHASE1_TEMPLATE)

    # Pre-calcular ep_code antes de iniciar o thread (usa lock para consistência)
    with nq_lock:
        provisional_ep_code = _next_ep_code(name)

    # Iniciar geração em background — retorna job_id imediatamente
    job_id = uuid.uuid4().hex[:8]
    with _ep_gen_lock:
        _ep_gen_state[job_id] = {
            "status": "running",
            "phase": "phase1",
            "phase_msg": "Fase 1: identificando ambientes e elementos visuais…",
            "jobs": [],
            "saved_doc": saved_doc,
            "ep_title": doc_title,
            "ep_code": provisional_ep_code,
            "error": None,
            "raw": "",
            "environments": [],
            "new_elements": [],
            "new_refs": [],
        }
        _ep_gen_by_project[name] = job_id

    def _run():
        _env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        proc = None
        try:
            # ─── FASE 1: identificar ambientes e elementos novos ───────────────
            with _ep_gen_lock:
                _ep_gen_state[job_id]["phase"]     = "phase1"
                _ep_gen_state[job_id]["phase_msg"] = "Fase 1: identificando ambientes e elementos visuais…"

            phase1_prompt = _build_phase1_prompt(description, effective_imgs, all_docs_content, _fase1_template)
            proc = subprocess.run(
                ["/home/nmaldaner/.local/bin/claude", "-p", phase1_prompt],
                capture_output=True, text=True, timeout=180, env=_env
            )
            raw1 = proc.stdout.strip()
            if raw1.startswith("```"):
                raw1 = re.sub(r"^```[a-z]*\n?", "", raw1)
                raw1 = re.sub(r"\n?```$", "", raw1)

            phase1_result = json.loads(raw1)
            environments = phase1_result.get("environments", [])
            new_elements = phase1_result.get("new_elements", [])
            with _ep_gen_lock:
                _ep_gen_state[job_id]["environments"] = environments
                _ep_gen_state[job_id]["new_elements"] = new_elements

            # ─── Construir amb_map apenas com existing_refs (sem gerar imagens) ─
            new_refs = []
            amb_map  = {}   # env_name (lower) → path da imagem de referência existente
            for env in environments:
                existing = env.get("existing_ref")
                if existing:
                    amb_map[env["name"].lower()] = existing

            # ─── FASE 2: gerar cenas com referências completas ────────────────
            with _ep_gen_lock:
                _ep_gen_state[job_id]["phase"]     = "phase2"
                _ep_gen_state[job_id]["phase_msg"] = "Fase 2: criando cenas com referências completas…"

            # Lista atualizada: refs originais + novas geradas
            all_refs = list(effective_imgs) + [nr["path"] for nr in new_refs]
            updated_images_list = "\n".join(f"- {p}" for p in all_refs) if all_refs else "Nenhuma"

            # Mapa de ambientes para orientar a IA na fase 2
            env_section = ""
            if environments:
                env_section = (
                    "\n\nMAPA DE AMBIENTES DO EPISÓDIO"
                    " — use EXATAMENTE estas imagens em todas as cenas de cada ambiente:\n"
                )
                for env in environments:
                    # Prioridade: imagem de ambiente gerada > existing_ref > nova ref de new_elements
                    ref = amb_map.get(env["name"].lower()) or env.get("generated_ref") or env.get("existing_ref")
                    for nr in new_refs:
                        if nr["name"].lower() == env["name"].lower():
                            ref = nr["path"]
                            break
                    env_section += f"- {env['name']}: {env.get('description', '')}\n"
                    if ref:
                        env_section += (
                            f"  → REFERÊNCIA OBRIGATÓRIA: {ref}"
                            f" (use em TODA cena deste ambiente — garante consistência visual)\n"
                        )

            resources_section_updated = (
                f"\nImagens de referência do projeto (use os paths exatos nas cenas):\n"
                f"{updated_images_list}\n"
            )
            if all_audios:
                resources_section_updated += (
                    "\nÁudios disponíveis no projeto:\n"
                    + "\n".join(f"- {a}" for a in all_audios) + "\n"
                )
            if all_docs_content:
                resources_section_updated += (
                    "\nDocumentos do projeto (contexto de consistência):\n"
                    + "\n\n".join(all_docs_content) + "\n"
                )
            if env_section:
                resources_section_updated += env_section

            phase2_prompt = _sys_template.format(
                description=description,
                resources=resources_section_updated,
                task_type=task_type,
                resolution=resolution,
                duration=duration,
                json_template=json_template,
            )

            proc = subprocess.run(
                ["/home/nmaldaner/.local/bin/claude", "-p", phase2_prompt],
                capture_output=True, text=True, timeout=360, env=_env
            )
            raw2 = proc.stdout.strip()
            if raw2.startswith("```"):
                raw2 = re.sub(r"^```[a-z]*\n?", "", raw2)
                raw2 = re.sub(r"\n?```$", "", raw2)
            jobs = json.loads(raw2)
            if not isinstance(jobs, list):
                raise ValueError("Resposta não é um array")

            with _ep_gen_lock:
                if job_id in _ep_gen_state:
                    n_envs = len(environments)
                    n_elems = len(new_elements)
                    done_msg = f"Concluído! {len(jobs)} cenas"
                    if n_envs:
                        done_msg += f" · {n_envs} ambiente(s)"
                    if n_elems:
                        done_msg += f" · {n_elems} elemento(s) novo(s)"
                    _ep_gen_state[job_id]["status"]   = "done"
                    _ep_gen_state[job_id]["phase"]     = "done"
                    _ep_gen_state[job_id]["phase_msg"] = done_msg
                    _ep_gen_state[job_id]["jobs"]      = jobs

        except Exception as e:
            with _ep_gen_lock:
                if job_id in _ep_gen_state:
                    _ep_gen_state[job_id]["status"] = "error"
                    _ep_gen_state[job_id]["error"]  = str(e)
                    _ep_gen_state[job_id]["raw"]    = proc.stdout[:500] if proc else ""

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"job_id": job_id, "saved_doc": saved_doc})


@app.route("/projects/<name>/generate-episode-status/<job_id>")
def generate_episode_status(name, job_id):
    with _ep_gen_lock:
        state = dict(_ep_gen_state.get(job_id, {}))
    if not state:
        return jsonify({"error": "não encontrado"}), 404
    return jsonify(state)


@app.route("/projects/<name>/regenerate-scene", methods=["POST"])
def regenerate_scene_prompt(name):
    """Re-gera o prompt de uma cena específica via Claude CLI."""
    data       = request.get_json(force=True)
    label      = data.get("label", "")
    resolution = data.get("resolution", "720P")
    duration   = int(data.get("duration", 5))
    ref_imgs   = data.get("ref_imgs", [])

    images_list = "\n".join(f"- {p}" for p in ref_imgs) if ref_imgs else "Nenhuma"
    json_template = (
        f'{{\n'
        f'  "label": "{label}",\n'
        f'  "task_type": "reference_to_video",\n'
        f'  "prompt": "Descrição cinemática detalhada em inglês para geração de vídeo por IA...",\n'
        f'  "resolution": "{resolution}",\n'
        f'  "duration": {duration},\n'
        f'  "num_inference_steps": 4,\n'
        f'  "seed": <número entre 1000 e 9999>,\n'
        f'  "offload": false,\n'
        f'  "low_vram": false,\n'
        f'  "ref_imgs": [<inclua paths relevantes da lista acima, ou [] se não houver>]\n'
        f'}}'
    )
    prompt = f"""Você é um assistente de produção de vídeo. Gere um prompt detalhado para esta cena de vídeo IA (SkyReels).

Cena: {label}
Imagens de referência disponíveis (use os paths exatos):
{images_list}
Resolução: {resolution}
Duração sugerida: ~{duration}s

Retorne SOMENTE um objeto JSON válido (não um array), começando com {{ e terminando com }}:
{json_template}

APENAS o JSON, nada mais."""

    _env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    result = None
    try:
        result = subprocess.run(
            ["/home/nmaldaner/.local/bin/claude", "-p", prompt],
            capture_output=True, text=True, timeout=60, env=_env
        )
        raw = result.stdout.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-z]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)
        job = json.loads(raw)
        if isinstance(job, list):
            job = job[0]
        return jsonify({"ok": True, "job": job})
    except Exception as e:
        return jsonify({
            "error": str(e),
            "raw": result.stdout[:500] if result else ""
        }), 500


# ── Kie.ai nano-banana-2 integration ─────────────────────────────────────────

KIE_STYLES: dict = {
    "anime":          ("An anime-style illustration of",    "Studio Ghibli inspired, soft colors, detailed backgrounds, expressive characters."),
    "anime_gacha":    ("A vibrant anime gacha-game style illustration of", "Genshin Impact inspired, cel-shaded characters, futuristic sci-fi setting, neon accents, detailed tech outfits, glowing energy effects, bokeh pixel background, expressive faces, dynamic pose, vibrant colors, high quality digital art."),
    "photorealistic": ("A photorealistic",                  "Captured with professional camera equipment, natural lighting, sharp details, high dynamic range."),
    "cinematic":      ("A cinematic film still of",         "Dramatic lighting, shallow depth of field, anamorphic lens flare, color graded in teal and orange."),
    "illustration":   ("A beautiful illustration of",       "Digital art style, vibrant colors, clean lines, professional quality illustration."),
    "3d_render":      ("A high-quality 3D render of",       "Studio lighting, PBR materials, octane render quality, smooth surfaces, ambient occlusion."),
    "concept_art":    ("Professional concept art of",       "Industry-standard quality, dynamic composition, atmospheric perspective, matte painting techniques."),
    "watercolor":     ("A watercolor painting of",          "Soft washes of color, visible brush strokes, paper texture, artistic imperfections, dreamy quality."),
    "product":        ("A professional product photography shot of", "White or minimal background, studio lighting, sharp focus, commercial quality, clean composition."),
}


def _enhance_prompt_for_kie(prompt: str, style: str) -> str:
    """Apply style prefix+suffix from nano-banana-2 prompt enhancement engine."""
    key = style.lower().replace(" ", "_").replace("-", "_")
    if key not in KIE_STYLES:
        return prompt
    prefix, suffix = KIE_STYLES[key]
    return f"{prefix} {prompt}. {suffix}"


def _kie_call_image(
    prompt: str,
    api_key: str,
    aspect_ratio: str = "16:9",
    resolution: str = "1K",
    image_urls: list | None = None,
) -> dict:
    """Call Kie.ai REST API (nano-banana-2) with polling.

    Supports text-to-image and image-to-image (up to 14 reference images via image_input).
    Returns {"images": [{"url": "..."}]} for compatibility with _fal_call_image.
    Polls every 5 s for up to 3 minutes (36 attempts).
    """
    import time
    import urllib.request as _req

    base = "https://api.kie.ai/api/v1/jobs"
    auth = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    inp: dict = {
        "prompt": prompt,
        "aspect_ratio": aspect_ratio,
        "resolution": resolution,
        "output_format": "png",
        "google_search": False,
    }
    # Image-to-image: refs como URLs públicas ou data URIs base64
    valid_inputs = [u for u in (image_urls or []) if u and (u.startswith("http") or u.startswith("data:"))]
    if valid_inputs:
        inp["image_input"] = valid_inputs[:14]
        print(f"[kie.ai] image-to-image com {len(valid_inputs)} ref(s) ({sum(1 for u in valid_inputs if u.startswith('data:'))} base64)")

    body = json.dumps({"model": "nano-banana-2", "input": inp}).encode()
    print(f"[kie.ai] createTask payload size: {len(body)/1024:.1f} KB")
    req = _req.Request(f"{base}/createTask", data=body, headers=auth, method="POST")
    with _req.urlopen(req, timeout=30) as r:
        resp_data = json.loads(r.read())
    if not resp_data.get("data") or not resp_data["data"].get("taskId"):
        raise RuntimeError(f"Kie.ai createTask falhou: {resp_data}")
    task_id = resp_data["data"]["taskId"]
    print(f"[kie.ai] task created: {task_id}")

    # Poll for result
    for attempt in range(60):
        time.sleep(5)
        poll = _req.Request(
            f"{base}/recordInfo?taskId={task_id}",
            headers={"Authorization": f"Bearer {api_key}"},
            method="GET",
        )
        with _req.urlopen(poll, timeout=30) as r:
            poll_data = json.loads(r.read())
        data = poll_data.get("data") or {}
        state = data.get("state", "")
        print(f"[kie.ai] attempt {attempt+1}: {state}")
        if state == "success":
            # resultJson é uma string JSON: {"resultUrls": ["https://..."]}
            result_json = data.get("resultJson", "{}")
            result = json.loads(result_json) if isinstance(result_json, str) else result_json
            urls = result.get("resultUrls") or []
            if not urls:
                raise RuntimeError(f"Kie.ai success mas sem URLs: {data}")
            return {"images": [{"url": u} for u in urls]}
        if state in ("failed", "error"):
            raise RuntimeError(f"Kie.ai task failed: {data.get('failMsg') or poll_data}")

    raise TimeoutError(f"Kie.ai task {task_id} timed out after 5 minutes")


def _resolve_ep_ref(ref: str, ep_dir: Path) -> str:
    """Resolve uma ref quebrada de episódio buscando o arquivo mais próximo na pasta.

    Se `ref` não existe mas aponta para uma pasta do episódio (ambiente/ ou elementos/),
    tenta encontrar o arquivo com nome mais similar usando tokens normalizados.
    Retorna o ref original se não encontrar nada melhor.
    """
    import unicodedata

    if Path(ref).exists():
        return ref  # já existe, sem necessidade de resolução

    ref_path = Path(ref)
    # Só tenta resolver refs dentro de episodios/
    if "episodios" not in ref_path.parts:
        return ref

    # Determina a subpasta (ambiente ou elementos)
    subfolder = None
    for part in ref_path.parts:
        if part in ("ambiente", "elementos"):
            subfolder = part
            break
    if not subfolder:
        return ref

    folder = ep_dir / subfolder
    if not folder.exists():
        return ref

    candidates = list(folder.glob("*.png")) + list(folder.glob("*.jpg"))
    if not candidates:
        return ref

    def _norm(s: str) -> set:
        s = unicodedata.normalize("NFD", s.lower())
        s = "".join(c for c in s if unicodedata.category(c) != "Mn")
        s = re.sub(r"[^a-z0-9]", " ", s)
        return set(t for t in s.split() if len(t) >= 3)

    ref_tokens = _norm(ref_path.stem)
    if not ref_tokens:
        return ref

    best, best_score = None, 0
    for c in candidates:
        c_tokens = _norm(c.stem)
        score = len(ref_tokens & c_tokens)  # interseção de tokens
        if score > best_score:
            best_score = score
            best = c

    if best and best_score > 0:
        resolved = str(best.resolve().relative_to(PROJECT_ROOT.resolve()))
        print(f"[resolve_ref] '{ref_path.name}' → '{best.name}' (score={best_score})")
        return resolved

    return ref


def _auto_match_refs(item_name: str, proj_name: str, exclude: list | None = None) -> list[str]:
    """(A) Match por nome: busca em imagens/ e figurantes/ do projeto arquivos cujo nome
    contenha alguma palavra-chave do nome do ambiente/elemento.
    Retorna lista de paths relativos ao PROJECT_ROOT (até 2 matches).
    """
    import unicodedata

    def _norm(s: str) -> str:
        s = unicodedata.normalize("NFD", s.lower())
        s = "".join(c for c in s if unicodedata.category(c) != "Mn")
        return re.sub(r"[^a-z0-9]", " ", s)

    # Tokens do nome do item (≥3 chars, ignora stopwords)
    STOP = {"de", "do", "da", "dos", "das", "em", "na", "no", "the", "of", "and", "com", "uma", "um"}
    tokens = [t for t in _norm(item_name).split() if len(t) >= 3 and t not in STOP]
    if not tokens:
        return []

    exclude_set = set(exclude or [])
    matches: list[str] = []
    IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

    for subfolder in ("imagens", "figurantes"):
        folder = PROJECTS_DIR / proj_name / subfolder
        if not folder.exists():
            continue
        for f in sorted(folder.iterdir()):
            if f.suffix.lower() not in IMAGE_EXTS:
                continue
            rel = str(f.relative_to(PROJECT_ROOT))
            if rel in exclude_set or rel in matches:
                continue
            fname_norm = _norm(f.stem)
            # Match se qualquer token do item aparece no nome do arquivo ou vice-versa
            if any(t in fname_norm or fname_norm.startswith(t) for t in tokens):
                matches.append(rel)
                if len(matches) >= 2:
                    return matches
    return matches


def _local_path_to_url(rel_path: str, server_host: str = "127.0.0.1", port: int = 7860) -> str | None:
    """Convert a local project-relative path to a URL served by this Flask app."""
    full = PROJECT_ROOT / rel_path
    if not full.exists():
        return None
    return f"http://{server_host}:{port}/file/{rel_path}"


def _path_to_base64(path: str, max_size: int = 512) -> str | None:
    """Convert a local file path to a base64 data URI.
    Redimensiona para max_size px no lado maior para manter o payload pequeno (~100KB).
    """
    import base64, io
    full = Path(path) if Path(path).is_absolute() else PROJECT_ROOT / path
    if not full.exists():
        return None
    try:
        from PIL import Image
        img = Image.open(full).convert("RGB")
        img.thumbnail((max_size, max_size), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        b64 = base64.b64encode(buf.getvalue()).decode()
        return f"data:image/jpeg;base64,{b64}"
    except Exception:
        # Fallback: envia original em base64 sem redimensionar
        import mimetypes
        mime = mimetypes.guess_type(str(full))[0] or "image/png"
        b64 = base64.b64encode(full.read_bytes()).decode()
        return f"data:{mime};base64,{b64}"


_temp_upload_cache: dict[str, str] = {}  # rel_path → public URL (cache por sessão)


def _upload_temp(rel_path: str, max_size: int = 768) -> str | None:
    """Faz upload de uma imagem local para um host público temporário e retorna a URL pública.

    Tenta catbox.moe (sem API key, permanente) e depois 0x0.st como fallback.
    Resultado é cacheado por sessão para evitar re-uploads do mesmo arquivo.
    """
    import io, urllib.request as _urr

    if rel_path in _temp_upload_cache:
        cached = _temp_upload_cache[rel_path]
        print(f"[temp-upload] cache hit: {rel_path} → {cached}")
        return cached

    full = PROJECT_ROOT / rel_path
    if not full.exists():
        print(f"[temp-upload] arquivo não encontrado: {rel_path}")
        return None

    # Redimensiona para max_size px para upload mais rápido
    try:
        from PIL import Image
        img = Image.open(full).convert("RGB")
        img.thumbnail((max_size, max_size), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=88)
        img_bytes = buf.getvalue()
        fname = full.stem + ".jpg"
        ctype = "image/jpeg"
    except Exception:
        img_bytes = full.read_bytes()
        fname = full.name
        ctype = "image/png"

    def _multipart_body(boundary: str, field_data: dict, file_field: str, filename: str, content_type: str, file_bytes: bytes) -> bytes:
        parts = b""
        for k, v in field_data.items():
            parts += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
        parts += (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; filename=\"{filename}\"\r\n"
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode() + file_bytes + f"\r\n--{boundary}--\r\n".encode()
        return parts

    # 1) catbox.moe
    try:
        bnd = "CatboxBoundary7MA4"
        body = _multipart_body(bnd, {"reqtype": "fileupload"}, "fileToUpload", fname, ctype, img_bytes)
        req = _urr.Request(
            "https://catbox.moe/user/api.php", data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={bnd}"}
        )
        with _urr.urlopen(req, timeout=30) as resp:
            url = resp.read().decode().strip()
        if url.startswith("https://"):
            _temp_upload_cache[rel_path] = url
            print(f"[temp-upload] catbox.moe: {rel_path} → {url}")
            return url
        print(f"[temp-upload] catbox.moe resposta inesperada: {url!r}")
    except Exception as e:
        print(f"[temp-upload] catbox.moe falhou: {e}")

    # 2) 0x0.st (fallback)
    try:
        bnd2 = "ZeroXZeroBoundary"
        body2 = _multipart_body(bnd2, {}, "file", fname, ctype, img_bytes)
        req2 = _urr.Request(
            "https://0x0.st", data=body2,
            headers={"Content-Type": f"multipart/form-data; boundary={bnd2}"}
        )
        with _urr.urlopen(req2, timeout=30) as resp2:
            url2 = resp2.read().decode().strip()
        if url2.startswith("https://"):
            _temp_upload_cache[rel_path] = url2
            print(f"[temp-upload] 0x0.st: {rel_path} → {url2}")
            return url2
        print(f"[temp-upload] 0x0.st resposta inesperada: {url2!r}")
    except Exception as e2:
        print(f"[temp-upload] 0x0.st falhou: {e2}")

    return None


def _download_image(url: str, dest: Path) -> None:
    """Download de imagem com User-Agent de browser para evitar bloqueios (403)."""
    import urllib.request as _dl
    req = _dl.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; SkyReels/1.0)"})
    with _dl.urlopen(req, timeout=60) as r:
        dest.write_bytes(r.read())


def _dispatch_image(cfg: dict, prompt: str, ref_paths: list | None = None) -> dict:
    """Route image generation to kie.ai or fal.ai based on configured image_model.

    - kie-ai/nano-banana-2 → REST API, image+text.
                             Refs locais são convertidas para base64 (data URI) e enviadas
                             diretamente no campo image_input — sem necessidade de URL pública.
    - fal-ai/* → fal_client SDK, supports image references via upload
    Returns {"images": [{"url": "..."}]}
    """
    model = cfg.get("image_model", "fal-ai/nano-banana")

    if model == "kie-ai/nano-banana-2":
        kie_key = cfg.get("kie_api_key", "") or os.environ.get("KIE_API_KEY", "")
        if not kie_key:
            raise ValueError("KIE_API_KEY não configurada — configure em ⚙ APIs")
        style = cfg.get("kie_image_style", "anime")
        enhanced = _enhance_prompt_for_kie(prompt, style)
        print(f"[kie.ai] style={style} | enhanced: {enhanced[:120]}")
        # Kie.ai exige URLs públicas. Fazemos upload das refs para catbox.moe (ou 0x0.st).
        public_refs: list[str] = []
        for rp in (ref_paths or [])[:4]:
            if rp.startswith("http"):
                public_refs.append(rp)
            else:
                pub_url = _upload_temp(rp)
                if pub_url:
                    public_refs.append(pub_url)
        if ref_paths and not public_refs:
            print("[kie.ai] upload de refs falhou — gerando sem refs")
        return _kie_call_image(enhanced, kie_key, image_urls=public_refs or None)

    # fal.ai path
    import fal_client as _fal
    fal_key = cfg.get("fal_key", "") or os.environ.get("FAL_KEY", "")
    if not fal_key:
        raise ValueError("FAL_KEY não configurada — configure em ⚙ APIs")
    os.environ["FAL_KEY"] = fal_key
    image_urls_fal: list = []
    if ref_paths:
        for rp in ref_paths[:4]:
            full = PROJECT_ROOT / rp
            if full.exists():
                image_urls_fal.append(_fal.upload_file(str(full)))
    return _fal_call_image(_fal, model, prompt, image_urls_fal or None)


def _fal_call_image(fal_client, model, prompt, image_urls=None):
    """Call fal.ai image generation, adapting parameters to the model type.

    Supported models:
      fal-ai/nano-banana           – Gemini 2.5 Flash, text-to-image
      fal-ai/nano-banana/edit      – Gemini 2.5 Flash, image+text edit
      fal-ai/flux/dev              – Flux Dev, text-to-image
      fal-ai/gpt-image-1-mini/edit – GPT Image 1 Mini, always edit
    """
    image_urls = [u for u in (image_urls or []) if u]

    # ── GPT Image 1 ─────────────────────────────────────────────────────────
    # Sempre requer image_urls (array). Sem refs → fallback para nano-banana.
    if "gpt-image-1" in model:
        if image_urls:
            return fal_client.subscribe(model, arguments={
                "prompt": prompt,
                "image_urls": image_urls,
                "n": 1,
                "size": "1792x1024",
            })
        # Sem imagem de referência → fallback text-to-image
        return fal_client.subscribe("fal-ai/nano-banana", arguments={
            "prompt": prompt,
            "num_images": 1,
            "aspect_ratio": "16:9",
            "output_format": "png",
        })

    # ── nano-banana / any model that has an /edit variant ───────────────────
    if image_urls:
        # Prefer explicit edit variant; default to nano-banana/edit when model
        # is text-only (e.g. flux/dev which has no /edit variant).
        if "nano-banana" in model:
            edit_model = "fal-ai/nano-banana/edit"
        elif model.endswith("/edit"):
            edit_model = model
        else:
            edit_model = "fal-ai/nano-banana/edit"
        return fal_client.subscribe(edit_model, arguments={
            "prompt": prompt,
            "image_urls": image_urls,
            "num_images": 1,
            "aspect_ratio": "16:9",
            "output_format": "png",
        })

    # ── Text-to-image fallback ───────────────────────────────────────────────
    txt_model = model.replace("/edit", "") if model.endswith("/edit") else model
    if "nano-banana" in txt_model or txt_model == "fal-ai/nano-banana":
        return fal_client.subscribe("fal-ai/nano-banana", arguments={
            "prompt": prompt,
            "num_images": 1,
            "aspect_ratio": "16:9",
            "output_format": "png",
        })
    return fal_client.subscribe(txt_model, arguments={
        "prompt": prompt,
        "num_images": 1,
        "image_size": "landscape_16_9",
    })


@app.route("/projects/<name>/generate-images", methods=["POST"])
def generate_episode_images(name):
    import urllib.request as urllib_req
    cfg = _load_effective_cfg(name)
    data    = request.get_json(force=True)
    jobs    = data.get("jobs", [])
    img_dir = PROJECTS_DIR / name / "imagens"
    img_dir.mkdir(exist_ok=True)

    updated = []
    for job in jobs:
        img_prompt = job.get("image_prompt") or job.get("prompt", "")[:400]
        try:
            res = _dispatch_image(cfg, img_prompt)
            url = res["images"][0]["url"]
            fname = secure_filename(f"{job.get('label','scene')[:40]}.jpg").replace(" ", "_")
            dest  = img_dir / fname
            _download_image(url, dest)
            rel   = str(dest.relative_to(PROJECT_ROOT))
            job   = {**job, "ref_imgs": [rel]}
        except Exception as e:
            job = {**job, "_img_error": str(e)}
        updated.append(job)

    return jsonify({"ok": True, "jobs": updated})


@app.route("/projects/<name>/generate-audio", methods=["POST"])
def generate_episode_audio(name):
    try:
        from elevenlabs.client import ElevenLabs as EL
    except ImportError:
        return jsonify({"error": "elevenlabs não instalado. Execute: pip install elevenlabs"}), 500

    cfg    = _load_global_config()
    el_key = cfg.get("elevenlabs_key", "") or os.environ.get("ELEVENLABS_API_KEY", "")
    voice  = cfg.get("elevenlabs_voice_id", "")
    if not el_key or not voice:
        return jsonify({"error": "ElevenLabs não configurado. Clique em ⚙ na aba Projetos."}), 400

    data    = request.get_json(force=True)
    jobs    = data.get("jobs", [])
    aud_dir = PROJECTS_DIR / name / "audios"
    aud_dir.mkdir(exist_ok=True)
    client  = EL(api_key=el_key)

    updated = []
    for job in jobs:
        text = _strip_audio_prefix(job.get("audio_text") or "")
        if not text:
            updated.append(job)
            continue
        try:
            audio_bytes = b"".join(client.text_to_speech.convert(
                text=text, voice_id=voice,
                model_id="eleven_multilingual_v2",
                output_format="mp3_44100_128"
            ))
            fname = secure_filename(f"{job.get('label','scene')[:40]}.mp3").replace(" ", "_")
            dest  = aud_dir / fname
            dest.write_bytes(audio_bytes)
            rel   = str(dest.relative_to(PROJECT_ROOT))
            import math
            aud_dur = _audio_duration(dest)
            new_job = {**job, "input_audio": rel}
            if aud_dur > 0:
                min_dur = math.ceil(aud_dur) + 1
                if new_job.get("duration", 0) < min_dur:
                    new_job["duration"] = min_dur
            job = new_job
        except Exception as e:
            job = {**job, "_audio_error": str(e)}
        updated.append(job)

    return jsonify({"ok": True, "jobs": updated})


def _nq_get_project(nq_id):
    """Retorna (nq, proj_name) ou (None, None)."""
    with nq_lock:
        nq = next((q for q in named_queues if q["id"] == nq_id), None)
    if nq is None:
        return None, None
    return nq, nq.get("project", "")


@app.route("/nqueues/<int:nq_id>/generate-images", methods=["POST"])
def nq_generate_images(nq_id):
    import urllib.request as urllib_req
    nq, proj_name = _nq_get_project(nq_id)
    if nq is None:
        return jsonify({"error": "Fila não encontrada"}), 404
    if not proj_name:
        return jsonify({"error": "Episódio não vinculado a um projeto"}), 400

    cfg     = _load_effective_cfg(proj_name)
    ep_slug = nq.get("ep_code") or re.sub(r'[^\w\-]', '_', nq.get("name", f"ep_{nq_id}"))[:60]
    img_dir = PROJECTS_DIR / proj_name / "episodios" / ep_slug / "imagens"
    img_dir.mkdir(parents=True, exist_ok=True)

    with nq_lock:
        jobs         = list(nq.get("jobs", []))
        environments = list(nq.get("environments", []))
        new_elements = list(nq.get("new_elements", []))

    # Mapa: generated_ref → path real (para resolver refs quebradas das cenas)
    # Usa os generated_ref que foram salvos após gerar imagens de ambiente/elemento
    ep_ref_map: dict[str, str] = {}
    for item in environments + new_elements:
        gen = item.get("generated_ref")
        if gen and Path(gen).exists():
            ep_ref_map[Path(gen).stem.lower()] = gen

    def _resolve_job_ref(ref: str) -> str | None:
        """Resolve ref de cena: se não existe e aponta para episodio/, busca via generated_ref map."""
        if not ref or ref.startswith("http"):
            return None
        if Path(ref).exists():
            return ref
        if "episodios" not in ref:
            return None  # ref externa não encontrada — descarta
        # Fuzzy match por tokens contra os generated_ref conhecidos
        import unicodedata
        def _norm(s):
            s = unicodedata.normalize("NFD", s.lower())
            s = "".join(c for c in s if unicodedata.category(c) != "Mn")
            return set(t for t in re.sub(r"[^a-z0-9]", " ", s).split() if len(t) >= 3)
        ref_tokens = _norm(Path(ref).stem)
        best, best_score = None, 0
        for stem, path in ep_ref_map.items():
            score = len(ref_tokens & _norm(stem))
            if score > best_score:
                best_score = score
                best = path
        if best and best_score >= 1:
            print(f"[resolve_ref] '{Path(ref).name}' → '{Path(best).name}' (score={best_score})")
            return best
        # Fallback: _resolve_ep_ref por arquivo real na pasta
        ep_dir = PROJECTS_DIR / proj_name / "episodios" / ep_slug
        return _resolve_ep_ref(ref, ep_dir) if Path(_resolve_ep_ref(ref, ep_dir)).exists() else None

    errors = []
    for i, job in enumerate(jobs):
        if job.get("status") == "done":
            continue
        img_prompt = job.get("image_prompt") or job.get("prompt", "")[:400]
        raw_refs = [r for r in (job.get("ref_imgs") or []) if r and not r.startswith("http")]
        ref_paths = [_resolve_job_ref(r) for r in raw_refs]
        ref_paths = [r for r in ref_paths if r]  # remove None
        try:
            res   = _dispatch_image(cfg, img_prompt, ref_paths or None)
            url   = res["images"][0]["url"]
            fname = secure_filename(f"{job.get('label','scene')[:40]}.png").replace(" ", "_")
            dest  = img_dir / fname
            _download_image(url, dest)
            rel   = str(dest.relative_to(PROJECT_ROOT))
            orig_refs = [r for r in (job.get("ref_imgs") or []) if r != rel]
            jobs[i] = {**job, "ref_imgs": ([rel] + orig_refs)[:4]}
        except Exception as e:
            errors.append(f"Cena {i+1}: {e}")

    with nq_lock:
        nq2 = next((q for q in named_queues if q["id"] == nq_id), None)
        if nq2:
            nq2["jobs"] = jobs
    _save_queues()
    return jsonify({"ok": True, "updated": len(jobs), "errors": errors})


@app.route("/nqueues/<int:nq_id>/generate-audio", methods=["POST"])
def nq_generate_audio(nq_id):
    nq, proj_name = _nq_get_project(nq_id)
    if nq is None:
        return jsonify({"error": "Fila não encontrada"}), 404
    if not proj_name:
        return jsonify({"error": "Episódio não vinculado a um projeto"}), 400
    try:
        from elevenlabs.client import ElevenLabs as EL
    except ImportError:
        return jsonify({"error": "elevenlabs não instalado"}), 500

    cfg         = _load_global_config()
    el_key      = cfg.get("elevenlabs_key", "") or os.environ.get("ELEVENLABS_API_KEY", "")
    global_voice = cfg.get("elevenlabs_voice_id", "")
    if not el_key:
        return jsonify({"error": "ElevenLabs não configurado. Clique em ⚙ na aba Projetos."}), 400

    # Mapa de vozes por personagem extraído dos docs do projeto
    proj_voices = _parse_project_voices(proj_name)

    ep_slug = nq.get("ep_code") or re.sub(r'[^\w\-]', '_', nq.get("name", f"ep_{nq_id}"))[:60]
    aud_dir = PROJECTS_DIR / proj_name / "episodios" / ep_slug / "audios"
    aud_dir.mkdir(parents=True, exist_ok=True)
    client  = EL(api_key=el_key)

    with nq_lock:
        jobs = list(nq.get("jobs", []))

    errors = []
    for i, job in enumerate(jobs):
        text = _strip_audio_prefix(job.get("audio_text") or "")
        if not text:
            continue
        # Voz: 1) voice_id do próprio job, 2) match por nome do personagem no label, 3) global
        voice = (
            job.get("voice_id")
            or (proj_voices and _match_voice(proj_voices, job.get("label", ""), ""))
            or global_voice
        )
        if not voice:
            errors.append(f"Cena {i+1}: nenhuma voz configurada")
            continue
        try:
            audio_bytes = b"".join(client.text_to_speech.convert(
                text=text, voice_id=voice,
                model_id="eleven_multilingual_v2",
                output_format="mp3_44100_128"
            ))
            fname = secure_filename(f"{job.get('label','scene')[:40]}.mp3").replace(" ", "_")
            dest  = aud_dir / fname
            dest.write_bytes(audio_bytes)
            rel   = str(dest.relative_to(PROJECT_ROOT))
            # Sincroniza duração do job com a duração real do áudio (+1s de respiro)
            import math
            aud_dur = _audio_duration(dest)
            new_job = {**job, "input_audio": rel, "voice_id": voice}
            if aud_dur > 0:
                min_dur = math.ceil(aud_dur) + 1
                if new_job.get("duration", 0) < min_dur:
                    new_job["duration"] = min_dur
                    print(f"[audio-gen] cena '{job.get('label','')}': áudio {aud_dur:.1f}s → duration={min_dur}s")
            jobs[i] = new_job
        except Exception as e:
            errors.append(f"Cena {i+1}: {e}")

    with nq_lock:
        nq2 = next((q for q in named_queues if q["id"] == nq_id), None)
        if nq2:
            nq2["jobs"] = jobs
    _save_queues()
    return jsonify({"ok": True, "updated": len(jobs), "errors": errors})


@app.route("/nqueues/<int:nq_id>/jobs/<int:job_id>/generate-audio", methods=["POST"])
def nq_job_generate_audio(nq_id, job_id):
    """Regenera o áudio de uma cena específica."""
    nq, proj_name = _nq_get_project(nq_id)
    if nq is None:
        return jsonify({"error": "Fila não encontrada"}), 404
    if not proj_name:
        return jsonify({"error": "Episódio não vinculado a um projeto"}), 400
    try:
        from elevenlabs.client import ElevenLabs as EL
    except ImportError:
        return jsonify({"error": "elevenlabs não instalado"}), 500

    cfg          = _load_global_config()
    el_key       = cfg.get("elevenlabs_key", "") or os.environ.get("ELEVENLABS_API_KEY", "")
    global_voice = cfg.get("elevenlabs_voice_id", "")
    if not el_key:
        return jsonify({"error": "ElevenLabs não configurado"}), 400

    with nq_lock:
        nq2  = next((q for q in named_queues if q["id"] == nq_id), None)
        job  = next((j for j in nq2["jobs"] if j["id"] == job_id), None) if nq2 else None
    if job is None:
        return jsonify({"error": "Cena não encontrada"}), 404

    text = _strip_audio_prefix(job.get("audio_text", ""))
    if not text:
        return jsonify({"error": "Cena sem audio_text"}), 400

    proj_voices = _parse_project_voices(proj_name)
    voice = (
        job.get("voice_id")
        or (proj_voices and _match_voice(proj_voices, job.get("label", ""), ""))
        or global_voice
    )
    if not voice:
        return jsonify({"error": "Nenhuma voz configurada para esta cena"}), 400

    ep_slug = nq2.get("ep_code") or re.sub(r'[^\w\-]', '_', nq2.get("name", f"ep_{nq_id}"))[:60]
    aud_dir = PROJECTS_DIR / proj_name / "episodios" / ep_slug / "audios"
    aud_dir.mkdir(parents=True, exist_ok=True)
    client = EL(api_key=el_key)

    try:
        import math
        audio_bytes = b"".join(client.text_to_speech.convert(
            text=text, voice_id=voice,
            model_id="eleven_multilingual_v2",
            output_format="mp3_44100_128"
        ))
        fname = secure_filename(f"{job.get('label','scene')[:40]}.mp3").replace(" ", "_")
        dest  = aud_dir / fname
        dest.write_bytes(audio_bytes)
        rel   = str(dest.relative_to(PROJECT_ROOT))
        aud_dur = _audio_duration(dest)
        with nq_lock:
            nq3 = next((q for q in named_queues if q["id"] == nq_id), None)
            if nq3:
                for j in nq3["jobs"]:
                    if j["id"] == job_id:
                        j["input_audio"] = rel
                        j["voice_id"]    = voice
                        if aud_dur > 0:
                            min_dur = math.ceil(aud_dur) + 1
                            if j.get("duration", 0) < min_dur:
                                j["duration"] = min_dur
                        break
        _save_queues()
        result = {"ok": True, "input_audio": rel, "voice_id": voice}
        if aud_dur > 0:
            result["audio_duration"] = round(aud_dur, 1)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/nqueues/<int:nq_id>/jobs/<int:job_id>/generate-image", methods=["POST"])
def nq_job_generate_image(nq_id, job_id):
    """Regenera a imagem de referência de uma cena específica."""
    import urllib.request as urllib_req
    nq, proj_name = _nq_get_project(nq_id)
    if nq is None:
        return jsonify({"error": "Fila não encontrada"}), 404
    if not proj_name:
        return jsonify({"error": "Episódio não vinculado a um projeto"}), 400

    with nq_lock:
        nq2 = next((q for q in named_queues if q["id"] == nq_id), None)
        job = next((j for j in nq2["jobs"] if j["id"] == job_id), None) if nq2 else None
    if job is None:
        return jsonify({"error": "Cena não encontrada"}), 404
    cfg = _load_effective_cfg(proj_name, nq2.get("image_style") if nq2 else None)

    img_prompt = job.get("image_prompt") or job.get("prompt", "")[:400]
    if not img_prompt:
        return jsonify({"error": "Cena sem image_prompt"}), 400

    ep_slug = nq2.get("ep_code") or re.sub(r'[^\w\-]', '_', nq2.get("name", f"ep_{nq_id}"))[:60]
    img_dir = PROJECTS_DIR / proj_name / "episodios" / ep_slug / "imagens"
    img_dir.mkdir(parents=True, exist_ok=True)

    try:
        ref_paths = [r for r in (job.get("ref_imgs") or []) if r and not r.startswith("http") and (PROJECT_ROOT / r).exists()]
        res   = _dispatch_image(cfg, img_prompt, ref_paths or None)
        url   = res["images"][0]["url"]
        fname = secure_filename(f"{job.get('label','scene')[:40]}.png").replace(" ", "_")
        dest  = img_dir / fname
        _download_image(url, dest)
        rel   = str(dest.relative_to(PROJECT_ROOT))

        with nq_lock:
            nq3 = next((q for q in named_queues if q["id"] == nq_id), None)
            if nq3:
                for j in nq3["jobs"]:
                    if j["id"] == job_id:
                        orig = [r for r in (j.get("ref_imgs") or []) if r != rel]
                        j["ref_imgs"] = ([rel] + orig)[:4]
                        break
        _save_queues()
        return jsonify({"ok": True, "image_path": rel})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────────────────────────

_load_queues()
_save_queues()   # persiste ep_codes atribuídos retroactivamente

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=7860, debug=False, threaded=True)
