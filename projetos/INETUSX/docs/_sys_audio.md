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

## REGRA CRÍTICA: Proporção áudio/vídeo
O áudio DEVE caber na duração do clip de vídeo. Referência:
- 5s de vídeo  = máximo 1-2 frases curtas (~15-20 palavras)
- 8s de vídeo  = máximo 2-3 frases curtas (~25-35 palavras)
- 10s de vídeo = máximo 3-4 frases curtas (~40-50 palavras)

Texto longo demais gera áudio maior que o clip = dessincronização.

EXCEÇÃO: se a descrição do episódio pedir EXPLICITAMENTE narração longa,
monólogo ou sequência de imagens, pode usar texto mais longo e ajustar
a duração do vídeo proporcionalmente.

## Casting de vozes
Definido na tabela de personagens nos documentos do projeto (campo voice_id).
