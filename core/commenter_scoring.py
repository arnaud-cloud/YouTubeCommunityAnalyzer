"""
Commenter credibility scorer — Step 1.5 (between Collect and Summarize).

Pure algorithmic scoring from existing DB data. No LLM calls.
Scores are per-community (relative rankings within that community's channels).
Results cached in the commenter_scores table; consumed by Steps 2 and 3.

Scoring formula:
    When llm_tone_score is available:
    quality_score = (
        avg(engagement_normalized) / 100  * 0.20   # community validation
      + channel_spread_score              * 0.20   # breadth of engagement
      + llm_tone_score                    * 0.15   # politeness + constructiveness + depth
      + vocab_richness_percentile         * 0.05   # varied vocabulary
      + like_ratio_percentile             * 0.10   # likes-per-comment within community
      + factual_anchor_ratio              * 0.15   # URL/date mentions
      + avg_length_score                  * 0.15   # comment thoughtfulness
    ) * (1 - reply_penalty)

    Without llm_tone_score (algorithmic only):
    quality_score = (
        avg(engagement_normalized) / 100  * 0.20
      + channel_spread_score              * 0.20
      + vocab_richness_percentile         * 0.20
      + like_ratio_percentile             * 0.10
      + factual_anchor_ratio              * 0.15
      + avg_length_score                  * 0.15
    ) * (1 - reply_penalty)

Tiers: A >= 0.65, B >= 0.45, C >= 0.25, D < 0.25
"""

from __future__ import annotations

import bisect
import json
import logging
import math
import re

from .db import get_community_channel_ids, get_all_settings
from .llm_client import LLMClient, _settings_to_llm_config

log = logging.getLogger(__name__)

# Regex for factual anchor detection: URLs and date-like patterns
_FACTUAL_RE = re.compile(
    r"https?://\S+"
    r"|"
    r"\b\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}\b"
    r"|"
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+\d{4}\b",
    re.IGNORECASE,
)

# Regex for vocabulary richness: 3+ char words, handles French accents
_WORD_RE = re.compile(r"[a-zA-ZÀ-ÿ]{3,}")

_TONE_SYSTEM_PROMPT = """\
IMPORTANT: You must respond with JSON only. No prose, no explanations, no questions, no markdown. Just the JSON object.

You are a comment quality evaluator. Comments may be in any language — evaluate them as-is and always respond in the JSON format below.

For each numbered commenter, rate their style on a 0.0–1.0 scale across THREE independent dimensions:

  1. POLITENESS / COURTESY: Are they respectful toward creators and others?
     Humor, lightness, and good-natured irony are signs of politeness, not flaws.
     Only aggression, contempt, or personal attacks justify a low score.
     Anchors:
       0.0 = aggressive, contemptuous, personal attacks
       0.3 = hostile or condescending tone
       0.5 = neutral, neither warm nor cold
       0.7 = respectful and pleasant, humor or lightness welcome
       1.0 = warm, kind, creates a positive atmosphere

  2. CONSTRUCTIVENESS: Does it add a fact, question, or nuanced point? Or is it empty praise/complaint?
     Anchors:
       0.0 = purely sycophantic or complaining, no substance
       0.5 = neutral fan engagement, not particularly useful
       1.0 = adds facts, questions, nuance, or original perspective

  3. ANALYTICAL DEPTH: Engages with specifics, or stays at surface level?
     Anchors:
       0.0 = no analysis, purely emotional reaction
       0.5 = some detail but stays superficial
       1.0 = precise analysis, specific references, developed reasoning

Required output format (JSON only, no other text):
{"scores": [{"index": 1, "politeness": 0.8, "constructiveness": 0.6, "depth": 0.7, "reason": "one sentence in English"}, ...]}

Use the integer index shown before each commenter's name. One entry per commenter."""

_TONE_SYSTEM_PROMPT_FR = """\
IMPORTANT : Vous devez répondre uniquement en JSON. Pas de prose, pas d'explications, pas de questions, pas de markdown. Uniquement l'objet JSON.

Vous êtes un évaluateur de qualité de commentaires. Les commentaires peuvent être dans n'importe quelle langue — évaluez-les tels quels et répondez toujours au format JSON ci-dessous.

Pour chaque commentateur numéroté, évaluez leur style de commentaire global sur une échelle de 0,0 à 1,0 selon TROIS dimensions indépendantes :

  1. POLITESSE / COURTOISIE : Est-il respectueux envers les créateurs et les autres ?
     L'humour, la légèreté et l'ironie bienveillante sont des signes de politesse, pas des défauts.
     Seuls l'agressivité, le mépris ou les attaques personnelles justifient une note basse.
     Ancres :
       0,0 = agressif, méprisant, attaques personnelles
       0,3 = ton hostile ou condescendant
       0,5 = neutre, ni chaleureux ni froid
       0,7 = respectueux et agréable, humour ou légèreté bienvenue
       1,0 = chaleureux, bienveillant, crée une atmosphère positive

  2. CONSTRUCTIVITÉ : Apporte-t-il un fait, une question ou une nuance ? Ou est-ce une louange/plainte vide ?
     Ancres :
       0,0 = purement adulateur ou plaintif, aucune substance
       0,5 = engagement neutre de fan, pas particulièrement utile
       1,0 = apporte des faits, questions, nuances ou points de vue originaux

  3. PROFONDEUR ANALYTIQUE : S'engage-t-il sur des points précis, ou reste-t-il en surface ?
     Ancres :
       0,0 = aucune analyse, réaction purement émotionnelle
       0,5 = quelques détails mais reste superficiel
       1,0 = analyse précise, références spécifiques, raisonnement développé

Format de sortie requis (JSON uniquement, aucun autre texte) :
{"scores": [{"index": 1, "politeness": 0.8, "constructiveness": 0.6, "depth": 0.7, "reason": "une phrase en français"}, ...]}

Utilisez l'index entier affiché avant le nom de chaque commentateur. Une entrée par commentateur."""

_TONE_SYSTEM_PROMPT_ES = """\
IMPORTANTE: Debes responder únicamente con JSON. Sin prosa, sin explicaciones, sin preguntas, sin markdown. Solo el objeto JSON.

Eres un evaluador de calidad de comentarios. Los comentarios pueden estar en cualquier idioma — evalúalos tal como están y responde siempre en el formato JSON a continuación.

Para cada comentarista numerado, califica su estilo en una escala de 0,0 a 1,0 según TRES dimensiones independientes:

  1. AMABILIDAD / CORTESÍA: ¿Es respetuoso hacia los creadores y los demás?
     El humor, la ligereza y la ironía benévola son signos de cortesía, no defectos.
     Solo la agresividad, el desprecio o los ataques personales justifican una puntuación baja.
     Anclas:
       0,0 = agresivo, despectivo, ataques personales
       0,3 = tono hostil o condescendiente
       0,5 = neutral, ni cálido ni frío
       0,7 = respetuoso y agradable, humor o ligereza bienvenidos
       1,0 = cálido, amable, crea una atmósfera positiva

  2. CONSTRUCTIVIDAD: ¿Aporta un hecho, una pregunta o un matiz? ¿O son alabanzas/quejas vacías?
     Anclas:
       0,0 = puramente adulador o quejoso, sin sustancia
       0,5 = participación neutra de fan, no especialmente útil
       1,0 = aporta hechos, preguntas, matices o perspectiva original

  3. PROFUNDIDAD ANALÍTICA: ¿Se involucra con aspectos específicos o se mantiene en la superficie?
     Anclas:
       0,0 = sin análisis, reacción puramente emocional
       0,5 = algunos detalles pero superficial
       1,0 = análisis preciso, referencias específicas, razonamiento desarrollado

Formato de salida requerido (solo JSON, sin otro texto):
{"scores": [{"index": 1, "politeness": 0.8, "constructiveness": 0.6, "depth": 0.7, "reason": "una frase en español"}, ...]}

Usa el índice entero que aparece antes del nombre de cada comentarista. Una entrada por comentarista."""

_TONE_PROMPTS = {
    "french": _TONE_SYSTEM_PROMPT_FR,
    "spanish": _TONE_SYSTEM_PROMPT_ES,
    "english": _TONE_SYSTEM_PROMPT,
}

# ── Single-creator deep analysis prompts ────────────────────────────────────

_CREATOR_BATCH_PROMPT = """\
IMPORTANT: Respond with JSON only. No prose, no explanations, no markdown.

You are performing an in-depth analysis of a single YouTube creator's commenting behavior.
You will receive one batch of their comments and rate them on three dimensions.

Rate this creator on a 0.0–1.0 scale for this batch:

  1. POLITENESS / COURTESY: Are they respectful in their interactions?
     Humor, lightness, and good-natured irony are signs of politeness, not flaws.
     Only aggression, contempt, or personal attacks justify a low score.
     Anchors:
       0.0 = aggressive, contemptuous, personal attacks
       0.5 = neutral, neither warm nor cold
       1.0 = warm, kind, creates a positive atmosphere

  2. CONSTRUCTIVENESS: Do their comments add value to the conversation?
     Anchors:
       0.0 = purely promotional, self-serving, or content-free
       0.5 = neutral engagement with no particular value
       1.0 = adds facts, questions, nuance, or genuine insight

  3. ANALYTICAL DEPTH: Do they engage with specifics or stay at surface level?
     Anchors:
       0.0 = no analysis, purely emotional or promotional
       0.5 = some detail but stays superficial
       1.0 = precise analysis, specific references, developed reasoning

Respond with JSON only:
{"politeness": 0.8, "constructiveness": 0.7, "depth": 0.6, "reason": "one sentence characterizing this batch"}"""

_CREATOR_BATCH_PROMPT_FR = """\
IMPORTANT : Répondez uniquement en JSON. Pas de prose, pas d'explications, pas de markdown.

Vous effectuez une analyse approfondie du comportement de commentaires d'un seul créateur YouTube.
Vous recevrez un lot de leurs commentaires et les évaluerez sur trois dimensions.

Évaluez ce créateur sur une échelle de 0,0 à 1,0 pour ce lot :

  1. POLITESSE / COURTOISIE : Est-il respectueux dans ses interactions ?
     L'humour, la légèreté et l'ironie bienveillante sont des signes de politesse, pas des défauts.
     Seuls l'agressivité, le mépris ou les attaques personnelles justifient une note basse.
     Ancres :
       0,0 = agressif, méprisant, attaques personnelles
       0,5 = neutre, ni chaleureux ni froid
       1,0 = chaleureux, bienveillant, crée une atmosphère positive

  2. CONSTRUCTIVITÉ : Ses commentaires apportent-ils de la valeur à la conversation ?
     Ancres :
       0,0 = purement promotionnel, égocentrique ou sans contenu
       0,5 = engagement neutre sans valeur particulière
       1,0 = apporte des faits, questions, nuances ou une vraie perspicacité

  3. PROFONDEUR ANALYTIQUE : S'engage-t-il sur des points précis ou reste-t-il en surface ?
     Ancres :
       0,0 = aucune analyse, purement émotionnel ou promotionnel
       0,5 = quelques détails mais superficiel
       1,0 = analyse précise, références spécifiques, raisonnement développé

Répondez uniquement en JSON :
{"politeness": 0.8, "constructiveness": 0.7, "depth": 0.6, "reason": "une phrase caractérisant ce lot"}"""

_CREATOR_BATCH_PROMPT_ES = """\
IMPORTANTE: Responde únicamente con JSON. Sin prosa, sin explicaciones, sin markdown.

Estás realizando un análisis detallado del comportamiento de comentarios de un único creador de YouTube.
Recibirás un lote de sus comentarios y los evaluarás en tres dimensiones.

Evalúa a este creador en una escala de 0,0 a 1,0 para este lote:

  1. AMABILIDAD / CORTESÍA: ¿Es respetuoso en sus interacciones?
     El humor, la ligereza y la ironía benévola son signos de cortesía, no defectos.
     Solo la agresividad, el desprecio o los ataques personales justifican una puntuación baja.
     Anclas:
       0,0 = agresivo, despectivo, ataques personales
       0,5 = neutral, ni cálido ni frío
       1,0 = cálido, amable, crea una atmósfera positiva

  2. CONSTRUCTIVIDAD: ¿Sus comentarios añaden valor a la conversación?
     Anclas:
       0,0 = puramente promocional, egocéntrico o sin contenido
       0,5 = participación neutral sin valor particular
       1,0 = aporta hechos, preguntas, matices o perspectiva genuina

  3. PROFUNDIDAD ANALÍTICA: ¿Se involucra con aspectos específicos o permanece superficial?
     Anclas:
       0,0 = sin análisis, puramente emocional o promocional
       0,5 = algunos detalles pero superficial
       1,0 = análisis preciso, referencias específicas, razonamiento desarrollado

Responde únicamente con JSON:
{"politeness": 0.8, "constructiveness": 0.7, "depth": 0.6, "reason": "una frase que caracterice este lote"}"""

_CREATOR_CONSOLIDATE_PROMPT = """\
IMPORTANT: Respond with JSON only. No prose, no explanations, no markdown.

You have analyzed a YouTube creator's comments across multiple batches.
Below are the partial assessments from each batch.

Synthesize these into a single final overall judgment.
Weight all batches equally unless you notice a clear trend of improvement or decline.

Respond with JSON only:
{"politeness": 0.8, "constructiveness": 0.7, "depth": 0.6, "reason": "one sentence final synthesis"}"""

_CREATOR_CONSOLIDATE_PROMPT_FR = """\
IMPORTANT : Répondez uniquement en JSON. Pas de prose, pas d'explications, pas de markdown.

Vous avez analysé les commentaires d'un créateur YouTube en plusieurs lots.
Voici les évaluations partielles de chaque lot.

Synthétisez-les en un jugement final global.
Pondérez tous les lots de manière égale, sauf si vous observez une tendance claire d'amélioration ou de déclin.

Répondez uniquement en JSON :
{"politeness": 0.8, "constructiveness": 0.7, "depth": 0.6, "reason": "une phrase de synthèse finale"}"""

_CREATOR_CONSOLIDATE_PROMPT_ES = """\
IMPORTANTE: Responde únicamente con JSON. Sin prosa, sin explicaciones, sin markdown.

Has analizado los comentarios de un creador de YouTube en múltiples lotes.
A continuación se muestran las evaluaciones parciales de cada lote.

Sintetiza estas en un juicio final global.
Pondera todos los lotes por igual, a menos que observes una clara tendencia de mejora o declive.

Responde únicamente con JSON:
{"politeness": 0.8, "constructiveness": 0.7, "depth": 0.6, "reason": "una frase de síntesis final"}"""

_CREATOR_BATCH_PROMPTS = {
    "french": _CREATOR_BATCH_PROMPT_FR,
    "spanish": _CREATOR_BATCH_PROMPT_ES,
    "english": _CREATOR_BATCH_PROMPT,
}
_CREATOR_CONSOLIDATE_PROMPTS = {
    "french": _CREATOR_CONSOLIDATE_PROMPT_FR,
    "spanish": _CREATOR_CONSOLIDATE_PROMPT_ES,
    "english": _CREATOR_CONSOLIDATE_PROMPT,
}

# ── Creator defensiveness prompts ────────────────────────────────────────────

_DEFENSIVENESS_PROMPT = """\
IMPORTANT: Respond with JSON only. No prose, no explanations, no markdown.

You are analyzing whether a YouTube creator shows defensiveness in their public comments.

CONTEXT: This creator presents themselves as spiritually advanced — calm, non-reactive, beyond ego.
Your job is to find where this mask slips.

Look specifically for:
- Dismissing or belittling commenters who question, criticize, or challenge them
- Using spiritual language as a shield ("you're not at my level", "low vibration energy", "that's your projection")
- Passive-aggressive responses — outwardly calm but subtly cutting
- Claiming detachment while visibly reacting (long rebuttals, sarcasm, condescension)
- Shutting down dialogue with authority claims rather than engaging the substance
- Needing the last word, especially after being challenged

IMPORTANT CALIBRATION — even one instance already matters:
  0.0 = no defensiveness detected — genuinely open, engages with criticism gracefully
  0.2 = one very subtle instance — a slightly dismissive phrase, possibly accidental
  0.4 = one clear instance — unmistakable defensiveness in at least one exchange
  0.6 = multiple instances — recurring pattern that contradicts the "unshakeable" claim
  0.8 = systematic — routinely defensive or dismissive when challenged
  1.0 = extreme — aggressive, condescending, or using spiritual bypass to deflect all challenge

Note: Polite disagreement is NOT defensiveness. Only score true defensiveness, not general negativity.
In your reason, cite concrete behavior observed in the comments.

Respond with JSON only:
{"defensiveness": 0.4, "instances": 2, "reason": "one sentence citing specific behavior patterns"}"""

_DEFENSIVENESS_PROMPT_FR = """\
IMPORTANT : Répondez uniquement en JSON. Pas de prose, pas d'explications, pas de markdown.

Vous analysez si un créateur YouTube montre de la défensivité dans ses commentaires publics.

CONTEXTE : Ce créateur se présente comme spirituellement avancé — calme, non-réactif, au-delà de l'ego.
Votre rôle est de trouver où ce masque se fissure.

Recherchez spécifiquement :
- Rejeter ou dénigrer les commentateurs qui les questionnent, critiquent ou défient
- Utiliser le langage spirituel comme bouclier ("vous n'êtes pas à mon niveau", "basse vibration", "c'est votre projection")
- Réponses passives-agressives — apparemment calmes mais subtilement blessantes
- Prétendre au détachement tout en réagissant visiblement (longues réfutations, sarcasme, condescendance)
- Fermer le dialogue par des affirmations d'autorité plutôt qu'en s'engageant sur le fond
- Avoir besoin d'avoir le dernier mot, surtout après avoir été challengé

CALIBRATION IMPORTANTE — même une seule instance compte :
  0,0 = aucune défensivité détectée — genuinement ouvert, s'engage avec les critiques avec grâce
  0,2 = une instance très subtile — une formulation légèrement dismissive, peut-être accidentelle
  0,4 = une instance claire — défensivité indiscutable dans au moins un échange
  0,6 = plusieurs instances — schéma récurrent qui contredit la prétention à l'imperturbabilité
  0,8 = systématique — régulièrement défensif ou dismissif face aux défis
  1,0 = extrême — agressif, condescendant, ou déviant spirituellement tout défi

Remarque : Un désaccord poli standard n'est PAS de la défensivité. Évaluez uniquement la défensivité.
Dans votre justification, citez des comportements concrets observés dans les commentaires.

Répondez uniquement en JSON :
{"defensiveness": 0.4, "instances": 2, "reason": "une phrase citant des comportements spécifiques"}"""

_DEFENSIVENESS_PROMPT_ES = """\
IMPORTANTE: Responde únicamente con JSON. Sin prosa, sin explicaciones, sin markdown.

Estás analizando si un creador de YouTube muestra actitud defensiva en sus comentarios públicos.

CONTEXTO: Este creador se presenta como espiritualmente avanzado — tranquilo, no reactivo, más allá del ego.
Tu trabajo es encontrar dónde se rompe esta máscara.

Busca específicamente:
- Desestimar o menospreciar a los comentaristas que los cuestionan, critican o desafían
- Usar el lenguaje espiritual como escudo ("no estás en mi nivel", "baja vibración", "eso es tu proyección")
- Respuestas pasivo-agresivas — aparentemente tranquilas pero sutilmente hirientes
- Alegar desapego mientras se reacciona visiblemente (largas réplicas, sarcasmo, condescendencia)
- Cerrar el diálogo con afirmaciones de autoridad en lugar de abordar el fondo
- Necesitar tener la última palabra, especialmente tras ser desafiados

CALIBRACIÓN IMPORTANTE — incluso una sola instancia importa:
  0,0 = no se detecta defensividad — genuinamente abierto, responde a las críticas con gracia
  0,2 = una instancia muy sutil — una frase ligeramente desestimadora, posiblemente accidental
  0,4 = una instancia clara — defensividad inconfundible en al menos un intercambio
  0,6 = múltiples instancias — patrón recurrente que contradice la pretensión de ecuanimidad
  0,8 = sistemático — habitualmente defensivo o desestimador cuando se le desafía
  1,0 = extremo — agresivo, condescendiente, o usando bypass espiritual ante todo desafío

Nota: El desacuerdo educado NO es defensividad. Evalúa solo la defensividad real.
En tu justificación, cita comportamientos concretos observados en los comentarios.

Responde únicamente con JSON:
{"defensiveness": 0.4, "instances": 2, "reason": "una frase citando comportamientos específicos"}"""

_DEFENSIVENESS_PROMPTS = {
    "french": _DEFENSIVENESS_PROMPT_FR,
    "spanish": _DEFENSIVENESS_PROMPT_ES,
    "english": _DEFENSIVENESS_PROMPT,
}


def _score_tier(score: float) -> str:
    if score >= 0.65:
        return "A"
    if score >= 0.45:
        return "B"
    if score >= 0.25:
        return "C"
    return "D"


def _vocab_ttr(text: str) -> float | None:
    """Type-token ratio for a single comment. Returns None if too short to be meaningful."""
    words = _WORD_RE.findall(text.lower())
    if len(words) < 5:
        return None
    return len(set(words)) / len(words)


def _load_commenter_stats(conn, channel_ids: list[str]) -> list[dict]:
    """
    Load per-commenter aggregate stats for the given channels.
    Two passes: SQL aggregate query, then Python text scan for factual anchors + vocab richness.
    """
    if not channel_ids:
        return []

    ph = ",".join("?" * len(channel_ids))
    rows = conn.execute(
        f"""SELECT author_channel_id,
                   MAX(author_name)                                 AS author_name,
                   COUNT(*)                                         AS comment_count,
                   COUNT(DISTINCT channel_id)                       AS channel_count,
                   SUM(like_count)                                  AS total_likes,
                   AVG(COALESCE(engagement_normalized, 0))          AS avg_engagement_norm,
                   CASE
                     WHEN SUM(CASE WHEN channel_id != author_channel_id THEN 1 ELSE 0 END) = 0
                       THEN 0.0
                     ELSE SUM(CASE WHEN is_reply = 1 AND channel_id != author_channel_id THEN 1.0 ELSE 0.0 END)
                          / SUM(CASE WHEN channel_id != author_channel_id THEN 1 ELSE 0 END)
                   END AS reply_ratio,
                   AVG(LENGTH(COALESCE(text, '')))                  AS avg_length
            FROM comments
            WHERE channel_id IN ({ph})
              AND author_channel_id IS NOT NULL
              AND author_channel_id != ''
            GROUP BY author_channel_id""",
        channel_ids,
    ).fetchall()

    stats = [dict(r) for r in rows]
    if not stats:
        return stats

    # Text pass: factual anchors + vocabulary richness
    author_ids = list({s["author_channel_id"] for s in stats})
    aid_ph = ",".join("?" * len(author_ids))
    text_rows = conn.execute(
        f"SELECT author_channel_id, text FROM comments "
        f"WHERE channel_id IN ({ph}) AND author_channel_id IN ({aid_ph})",
        channel_ids + author_ids,
    ).fetchall()

    anchor_counts: dict[str, list[int]] = {}
    vocab_data: dict[str, list[float]] = {}
    for r in text_rows:
        aid = r["author_channel_id"]
        text = r["text"] or ""

        # Factual anchors
        has_anchor = 1 if _FACTUAL_RE.search(text) else 0
        if aid not in anchor_counts:
            anchor_counts[aid] = [0, 0]
        anchor_counts[aid][0] += has_anchor
        anchor_counts[aid][1] += 1

        # Vocabulary richness
        ttr = _vocab_ttr(text)
        if ttr is not None:
            vocab_data.setdefault(aid, []).append(ttr)

    for s in stats:
        aid = s["author_channel_id"]
        ac = anchor_counts.get(aid, [0, 1])
        s["factual_ratio"] = ac[0] / max(ac[1], 1)
        ttrs = vocab_data.get(aid, [])
        s["vocab_richness"] = sum(ttrs) / len(ttrs) if ttrs else 0.0

    return stats


def _compute_component_scores(stats: list[dict]) -> list[dict]:
    """
    Normalize raw stats into 0-1 sub-scores and compute the final quality_score.
    Uses vocab_richness_score as the content-quality signal unless llm_tone_score
    is already populated on the row (set by score_community_tone()).
    """
    if not stats:
        return []

    max_channels = max(s["channel_count"] for s in stats)
    log_max = math.log(max_channels + 1)

    # Percentile lists
    like_per_comment_vals = sorted(
        s["total_likes"] / max(s["comment_count"], 1) for s in stats
    )
    vocab_vals = sorted(s.get("vocab_richness", 0.0) for s in stats)
    n = len(like_per_comment_vals)

    enriched = []
    for s in stats:
        # 1. Engagement normalization (0-100 scale → 0-1)
        eng_score = min(s["avg_engagement_norm"] / 100.0, 1.0)

        # 2. Channel spread: log scale, relative to max in community
        ch_spread = math.log(s["channel_count"] + 1) / log_max if log_max > 0 else 0.0

        # 3. Like-per-comment percentile
        lpc = s["total_likes"] / max(s["comment_count"], 1)
        rank = bisect.bisect_left(like_per_comment_vals, lpc)
        like_ratio = rank / (n - 1) if n > 1 else 0.5

        # 4. Factual anchor ratio (already 0-1)
        factual = min(s.get("factual_ratio", 0.0), 1.0)

        # 5. Comment length score: trapezoid (50=0, 50-300=1, >300 diminishes)
        avg_len = s["avg_length"] or 0.0
        if avg_len < 50:
            length_score = avg_len / 50.0
        elif avg_len <= 300:
            length_score = 1.0
        else:
            length_score = max(0.0, 1.0 - (avg_len - 300) / 500.0)

        # 6. Vocabulary richness percentile
        vr = s.get("vocab_richness", 0.0)
        vrank = bisect.bisect_left(vocab_vals, vr)
        vocab_score = vrank / (n - 1) if n > 1 else 0.5

        # 7. Reply penalty (external channels only)
        reply_penalty = min(s["reply_ratio"] * 0.5, 0.3)

        # 8. Defensiveness penalty (creators only; NULL for unassessed commenters)
        defensiveness = s.get("llm_defensiveness_score")
        defensiveness_penalty = min(float(defensiveness) * 0.4, 0.4) if defensiveness is not None else 0.0

        # Content-quality: combine T% + V% when both available, else V% alone
        llm_tone = s.get("llm_tone_score")
        if llm_tone is not None:
            raw = (
                eng_score        * 0.20
                + ch_spread      * 0.20
                + float(llm_tone) * 0.15
                + vocab_score    * 0.05
                + like_ratio     * 0.10
                + factual        * 0.15
                + length_score   * 0.15
            )
        else:
            raw = (
                eng_score        * 0.20
                + ch_spread      * 0.20
                + vocab_score    * 0.20
                + like_ratio     * 0.10
                + factual        * 0.15
                + length_score   * 0.15
            )
        quality_score = round(
            min(max(raw * (1.0 - reply_penalty) * (1.0 - defensiveness_penalty), 0.0), 1.0), 4
        )

        enriched.append({
            **s,
            "quality_score":        quality_score,
            "tier":                 _score_tier(quality_score),
            "avg_eng_score":        round(eng_score, 4),
            "channel_spread_score": round(ch_spread, 4),
            "like_ratio_score":     round(like_ratio, 4),
            "factual_anchor_score": round(factual, 4),
            "avg_length_score":     round(length_score, 4),
            "vocab_richness_score": round(vocab_score, 4),
            "reply_penalty":        round(reply_penalty, 4),
            "reply_ratio":          round(s["reply_ratio"], 4),
        })

    return enriched


def score_community(conn, community_id: int) -> int:
    """
    Compute and cache algorithmic credibility scores for all commenters in the community.
    Preserves existing llm_tone_score / llm_tone_reason values if present.
    Returns the number of commenters scored.
    """
    channel_ids = get_community_channel_ids(conn, community_id)
    if not channel_ids:
        log.warning(f"commenter_scoring: community {community_id} has no channels")
        return 0

    stats = _load_commenter_stats(conn, channel_ids)
    if not stats:
        log.info(f"commenter_scoring: no comments found for community {community_id}")
        return 0

    # Preserve existing LLM tone + defensiveness scores across re-scoring
    existing_tone: dict[str, dict] = {
        r["author_channel_id"]: {
            "llm_tone_score":              r["llm_tone_score"],
            "llm_politeness_score":        r["llm_politeness_score"],
            "llm_constructiveness_score":  r["llm_constructiveness_score"],
            "llm_depth_score":             r["llm_depth_score"],
            "llm_tone_reason":             r["llm_tone_reason"],
            "llm_tone_backend":            r["llm_tone_backend"],
            "llm_tone_model":              r["llm_tone_model"],
            "llm_defensiveness_score":     r["llm_defensiveness_score"],
            "llm_defensiveness_reason":    r["llm_defensiveness_reason"],
        }
        for r in conn.execute(
            "SELECT author_channel_id, llm_tone_score, llm_politeness_score, "
            "llm_constructiveness_score, llm_depth_score, llm_tone_reason, "
            "llm_tone_backend, llm_tone_model, "
            "llm_defensiveness_score, llm_defensiveness_reason "
            "FROM commenter_scores WHERE community_id = ?",
            (community_id,),
        ).fetchall()
    }
    for s in stats:
        tone = existing_tone.get(s["author_channel_id"])
        if tone:
            if tone.get("llm_tone_score") is not None:
                s["llm_tone_score"]             = tone["llm_tone_score"]
                s["llm_politeness_score"]       = tone["llm_politeness_score"]
                s["llm_constructiveness_score"] = tone["llm_constructiveness_score"]
                s["llm_depth_score"]            = tone["llm_depth_score"]
                s["llm_tone_reason"]            = tone["llm_tone_reason"]
                s["llm_tone_backend"]           = tone["llm_tone_backend"]
                s["llm_tone_model"]             = tone["llm_tone_model"]
            if tone.get("llm_defensiveness_score") is not None:
                s["llm_defensiveness_score"]    = tone["llm_defensiveness_score"]
                s["llm_defensiveness_reason"]   = tone["llm_defensiveness_reason"]

    enriched = _compute_component_scores(stats)

    conn.execute(
        "DELETE FROM commenter_scores WHERE community_id = ?", (community_id,)
    )
    conn.executemany(
        """INSERT INTO commenter_scores
               (community_id, author_channel_id, author_name,
                quality_score, tier,
                avg_engagement_norm, channel_spread_score, like_ratio_score,
                factual_anchor_score, avg_length_score, vocab_richness_score,
                llm_tone_score, llm_politeness_score, llm_constructiveness_score, llm_depth_score,
                llm_tone_reason, llm_tone_backend, llm_tone_model,
                llm_defensiveness_score, llm_defensiveness_reason,
                reply_penalty, reply_ratio,
                comment_count, channel_count, total_likes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                community_id,
                r["author_channel_id"],
                r["author_name"],
                r["quality_score"],
                r["tier"],
                r["avg_eng_score"],
                r["channel_spread_score"],
                r["like_ratio_score"],
                r["factual_anchor_score"],
                r["avg_length_score"],
                r["vocab_richness_score"],
                r.get("llm_tone_score"),
                r.get("llm_politeness_score"),
                r.get("llm_constructiveness_score"),
                r.get("llm_depth_score"),
                r.get("llm_tone_reason"),
                r.get("llm_tone_backend"),
                r.get("llm_tone_model"),
                r.get("llm_defensiveness_score"),
                r.get("llm_defensiveness_reason"),
                r["reply_penalty"],
                r["reply_ratio"],
                r["comment_count"],
                r["channel_count"],
                r["total_likes"],
            )
            for r in enriched
        ],
    )
    conn.commit()
    log.info(
        f"commenter_scoring: scored {len(enriched)} commenters "
        f"for community {community_id}"
    )
    return len(enriched)


def _localise_tone_prompt(conn, community_id: int, llm) -> str:
    """
    Detect the dominant language of the community's comments and return
    the pre-translated tone system prompt for that language.
    Supported: English, French, Spanish. Falls back to English for others.
    """
    sample_rows = conn.execute(
        """SELECT text FROM comments
           WHERE channel_id IN (
               SELECT source_id FROM community_sources WHERE community_id = ?
               UNION
               SELECT channel_id FROM community_channels WHERE community_id = ?
           )
           AND text IS NOT NULL AND LENGTH(text) > 20
           ORDER BY RANDOM() LIMIT 30""",
        (community_id, community_id),
    ).fetchall()

    if not sample_rows:
        return _TONE_SYSTEM_PROMPT

    sample_text = "\n".join(r["text"][:100] for r in sample_rows)

    try:
        lang = llm.complete(
            "You are a language detector. Reply with only the language name in English "
            "(e.g. 'French', 'English', 'Spanish'). Nothing else.",
            f"What language are most of these comments written in?\n\n{sample_text}",
            max_tokens=16,
        ).strip().strip(".").lower()
    except Exception as e:
        log.warning(f"commenter_scoring: language detection failed: {e}, using English prompt")
        return _TONE_SYSTEM_PROMPT

    prompt = _TONE_PROMPTS.get(lang, _TONE_SYSTEM_PROMPT)
    log.info(f"commenter_scoring: detected language '{lang}', using {'localised' if lang in _TONE_PROMPTS else 'English fallback'} prompt")
    return prompt


def score_community_tone(conn, community_id: int,
                         progress_callback=None,
                         scope: str = "all") -> int:
    """
    Run an LLM pass to score tone (politeness + constructiveness + depth)
    for commenters in the community. Updates llm_tone_score and llm_tone_reason,
    then recomputes quality_score / tier using the LLM score in the content slot.

    scope: "all" = every commenter, "creators" = channel owners only.
    Commenters already scored with the currently-configured backend+model are skipped.

    Requires commenter scores to already exist (call score_community() first).
    Returns number of commenters scored.
    """
    settings = get_all_settings(conn)
    cfg = _settings_to_llm_config(settings)
    llm = LLMClient(cfg, role="tone")
    current_backend = llm.backend
    current_model = llm._model

    # Auto-detect community language and translate system prompt if needed
    system_prompt = _localise_tone_prompt(conn, community_id, llm)

    # Determine creator channel IDs when scoping to creators only
    creator_ids: set[str] | None = None
    if scope == "creators":
        channel_ids_for_scope = get_community_channel_ids(conn, community_id)
        # Channel IDs in community_sources / community_channels map to channel records
        # The creator's author_channel_id equals their channel_id in the channels table
        creator_ids = set(channel_ids_for_scope)

    # Load scored commenters, filtered by scope and skip already-processed
    query = (
        "SELECT author_channel_id, author_name, llm_tone_backend, llm_tone_model "
        "FROM commenter_scores cs WHERE community_id = ? "
        "ORDER BY ("
        "  SELECT 1 FROM community_sources s "
        "  WHERE s.community_id = cs.community_id AND s.source_id = cs.author_channel_id "
        "  UNION SELECT 1 FROM community_channels c "
        "  WHERE c.community_id = cs.community_id AND c.channel_id = cs.author_channel_id "
        "  LIMIT 1"
        ") DESC NULLS LAST, quality_score DESC"
    )
    all_rows = conn.execute(query, (community_id,)).fetchall()

    rows = []
    for r in all_rows:
        # Scope filter
        if creator_ids is not None and r["author_channel_id"] not in creator_ids:
            continue
        # Skip if already scored with the same backend+model
        if r["llm_tone_backend"] == current_backend and r["llm_tone_model"] == current_model:
            continue
        rows.append(r)

    if not rows:
        return 0

    channel_ids = get_community_channel_ids(conn, community_id)
    ph = ",".join("?" * len(channel_ids))

    BATCH_SIZE = 10
    COMMENTS_PER_AUTHOR = 30
    total_scored = 0

    for batch_start in range(0, len(rows), BATCH_SIZE):
        batch = rows[batch_start: batch_start + BATCH_SIZE]
        author_ids = [r["author_channel_id"] for r in batch]
        aid_ph = ",".join("?" * len(author_ids))

        # Fetch top comments per author
        comments_by_author: dict[str, list[str]] = {r["author_channel_id"]: [] for r in batch}
        for cr in conn.execute(
            f"SELECT author_channel_id, text FROM comments "
            f"WHERE channel_id IN ({ph}) AND author_channel_id IN ({aid_ph}) "
            f"AND text IS NOT NULL AND LENGTH(text) > 10 "
            f"ORDER BY like_count DESC",
            channel_ids + author_ids,
        ).fetchall():
            aid = cr["author_channel_id"]
            if len(comments_by_author.get(aid, [])) < COMMENTS_PER_AUTHOR:
                comments_by_author.setdefault(aid, []).append(cr["text"])

        # Build user prompt — numbered by index for reliable matching
        sections = []
        index_to_aid: dict[int, str] = {}
        idx = 1
        for r in batch:
            aid = r["author_channel_id"]
            name = r["author_name"] or aid
            comments = comments_by_author.get(aid, [])
            if not comments:
                continue
            comment_block = "\n".join(f"  - {c[:200]}" for c in comments)
            sections.append(f"COMMENTER {idx}: {name}\nCOMMENTS:\n{comment_block}")
            index_to_aid[idx] = aid
            idx += 1

        if not sections:
            continue

        user_prompt = (
            f"Rate the following {len(sections)} commenter(s).\n\n"
            + "\n\n".join(sections)
        )

        if progress_callback:
            progress_callback(batch_start, len(rows))

        def _normalise_scores(result) -> list | None:
            """Return a list of score items from any shape the model returns, or None on failure."""
            if isinstance(result, list):
                return result
            if isinstance(result, dict):
                if "scores" in result:
                    s = result["scores"]
                    return s if isinstance(s, list) else [s]
                if "index" in result or "score" in result:
                    return [result]
                _REFUSAL_KEYS = {"code", "system", "message", "error", "response", "text"}
                if result.keys() & _REFUSAL_KEYS:
                    log.warning(f"commenter_scoring: model refusal (keys: {list(result.keys())}), will retry individually")
                else:
                    log.warning(f"commenter_scoring: unrecognised result shape — keys: {list(result.keys())}")
                return None
            log.warning(f"commenter_scoring: unexpected result type {type(result)}")
            return None

        def _store_score(item, idx_map) -> bool:
            """Parse one score item and write to DB. Returns True on success."""
            raw_index = item.get("index")
            reason = item.get("reason", "")
            try:
                item_index = int(raw_index)
            except (TypeError, ValueError):
                return False
            aid = idx_map.get(item_index)
            if aid is None:
                return False

            def _clamp(v):
                try:
                    return round(min(max(float(v), 0.0), 1.0), 4)
                except (TypeError, ValueError):
                    return None

            pol  = _clamp(item.get("politeness"))
            con  = _clamp(item.get("constructiveness"))
            dep  = _clamp(item.get("depth"))

            # Fall back to legacy "score" key if sub-scores missing
            if pol is None and con is None and dep is None:
                avg = _clamp(item.get("score"))
            else:
                parts = [x for x in (pol, con, dep) if x is not None]
                avg = round(sum(parts) / len(parts), 4) if parts else None

            if avg is None:
                return False

            conn.execute(
                "UPDATE commenter_scores "
                "SET llm_tone_score = ?, "
                "    llm_politeness_score = ?, "
                "    llm_constructiveness_score = ?, "
                "    llm_depth_score = ?, "
                "    llm_tone_reason = ?, "
                "    llm_tone_backend = ?, llm_tone_model = ? "
                "WHERE community_id = ? AND author_channel_id = ?",
                (avg, pol, con, dep, reason, current_backend, current_model, community_id, aid),
            )
            return True

        def _try_batch(sects: list[str], idx_map: dict[int, str], max_tok: int) -> set[int]:
            """Submit a batch prompt, return set of indices successfully stored."""
            prompt = f"Rate the following {len(sects)} commenter(s).\n\n" + "\n\n".join(sects)
            try:
                result = llm.complete_json(system_prompt, prompt, max_tokens=max_tok)
                items = _normalise_scores(result)
                if not items:
                    return set()
                stored = set()
                for item in items:
                    if _store_score(item, idx_map):
                        stored.add(int(item.get("index", -1)))
                return stored
            except Exception as e:
                log.warning(f"commenter_scoring: batch of {len(sects)} failed: {e}")
                return set()

        # Tier 1: full batch of 10
        matched_indices = _try_batch(sections, index_to_aid, max_tok=1024)
        total_scored += len(matched_indices)

        # Tier 2: sub-batches of 5 for unmatched
        unmatched = [(i, s) for i, s in zip(index_to_aid.keys(), sections)
                     if i not in matched_indices]
        if unmatched:
            log.info(f"commenter_scoring: {len(unmatched)}/{len(sections)} unmatched, retrying in sub-batches of 5")
            SUB_BATCH = 5
            for sub_start in range(0, len(unmatched), SUB_BATCH):
                sub = unmatched[sub_start: sub_start + SUB_BATCH]
                sub_idxs = [i for i, _ in sub]
                sub_sects = [s for _, s in sub]
                sub_map = {i: index_to_aid[i] for i in sub_idxs}
                stored = _try_batch(sub_sects, sub_map, max_tok=512)
                total_scored += len(stored)
                matched_indices |= stored

        # Tier 3: one-by-one for still-unmatched
        still_unmatched = [(i, s) for i, s in zip(index_to_aid.keys(), sections)
                           if i not in matched_indices]
        if still_unmatched:
            log.info(f"commenter_scoring: {len(still_unmatched)} still unmatched, retrying one-by-one")
            for solo_idx, solo_sect in still_unmatched:
                stored = _try_batch([solo_sect], {solo_idx: index_to_aid[solo_idx]}, max_tok=256)
                total_scored += len(stored)

        conn.commit()

    if total_scored == 0:
        return 0

    # Recompute quality_score / tier now that LLM scores are stored
    # Load fresh stats (existing rows already have llm_tone_score set)
    stats = _load_commenter_stats(conn, channel_ids)
    if not stats:
        return total_scored

    tone_map: dict[str, dict] = {
        r["author_channel_id"]: {
            "llm_tone_score":              r["llm_tone_score"],
            "llm_politeness_score":        r["llm_politeness_score"],
            "llm_constructiveness_score":  r["llm_constructiveness_score"],
            "llm_depth_score":             r["llm_depth_score"],
            "llm_tone_reason":             r["llm_tone_reason"],
            "llm_tone_backend":            r["llm_tone_backend"],
            "llm_tone_model":              r["llm_tone_model"],
            "llm_defensiveness_score":     r["llm_defensiveness_score"],
            "llm_defensiveness_reason":    r["llm_defensiveness_reason"],
        }
        for r in conn.execute(
            "SELECT author_channel_id, llm_tone_score, llm_politeness_score, "
            "llm_constructiveness_score, llm_depth_score, llm_tone_reason, "
            "llm_tone_backend, llm_tone_model, "
            "llm_defensiveness_score, llm_defensiveness_reason "
            "FROM commenter_scores WHERE community_id = ?",
            (community_id,),
        ).fetchall()
    }
    for s in stats:
        t = tone_map.get(s["author_channel_id"], {})
        if t.get("llm_tone_score") is not None:
            s["llm_tone_score"]             = t["llm_tone_score"]
            s["llm_politeness_score"]       = t["llm_politeness_score"]
            s["llm_constructiveness_score"] = t["llm_constructiveness_score"]
            s["llm_depth_score"]            = t["llm_depth_score"]
            s["llm_tone_reason"]            = t["llm_tone_reason"]
            s["llm_tone_backend"]           = t["llm_tone_backend"]
            s["llm_tone_model"]             = t["llm_tone_model"]
        if t.get("llm_defensiveness_score") is not None:
            s["llm_defensiveness_score"]    = t["llm_defensiveness_score"]
            s["llm_defensiveness_reason"]   = t["llm_defensiveness_reason"]

    enriched = _compute_component_scores(stats)

    conn.execute(
        "DELETE FROM commenter_scores WHERE community_id = ?", (community_id,)
    )
    conn.executemany(
        """INSERT INTO commenter_scores
               (community_id, author_channel_id, author_name,
                quality_score, tier,
                avg_engagement_norm, channel_spread_score, like_ratio_score,
                factual_anchor_score, avg_length_score, vocab_richness_score,
                llm_tone_score, llm_politeness_score, llm_constructiveness_score, llm_depth_score,
                llm_tone_reason, llm_tone_backend, llm_tone_model,
                llm_defensiveness_score, llm_defensiveness_reason,
                reply_penalty, reply_ratio,
                comment_count, channel_count, total_likes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                community_id,
                r["author_channel_id"],
                r["author_name"],
                r["quality_score"],
                r["tier"],
                r["avg_eng_score"],
                r["channel_spread_score"],
                r["like_ratio_score"],
                r["factual_anchor_score"],
                r["avg_length_score"],
                r["vocab_richness_score"],
                r.get("llm_tone_score"),
                r.get("llm_politeness_score"),
                r.get("llm_constructiveness_score"),
                r.get("llm_depth_score"),
                r.get("llm_tone_reason"),
                r.get("llm_tone_backend"),
                r.get("llm_tone_model"),
                r.get("llm_defensiveness_score"),
                r.get("llm_defensiveness_reason"),
                r["reply_penalty"],
                r["reply_ratio"],
                r["comment_count"],
                r["channel_count"],
                r["total_likes"],
            )
            for r in enriched
        ],
    )
    conn.commit()
    if progress_callback:
        progress_callback(len(rows), len(rows))
    log.info(
        f"commenter_scoring: tone-scored {total_scored} commenters "
        f"for community {community_id}"
    )
    return total_scored


def _localise_creator_prompts(conn, community_id: int, llm) -> tuple[str, str, str]:
    """
    Return (batch_prompt, consolidate_prompt, defensiveness_prompt) localised to the
    community's language. Single language-detection call shared across all three.
    """
    sample_rows = conn.execute(
        """SELECT text FROM comments
           WHERE channel_id IN (
               SELECT source_id FROM community_sources WHERE community_id = ?
               UNION
               SELECT channel_id FROM community_channels WHERE community_id = ?
           )
           AND text IS NOT NULL AND LENGTH(text) > 20
           ORDER BY RANDOM() LIMIT 30""",
        (community_id, community_id),
    ).fetchall()

    if not sample_rows:
        return _CREATOR_BATCH_PROMPT, _CREATOR_CONSOLIDATE_PROMPT, _DEFENSIVENESS_PROMPT

    sample_text = "\n".join(r["text"][:100] for r in sample_rows)
    try:
        lang = llm.complete(
            "You are a language detector. Reply with only the language name in English "
            "(e.g. 'French', 'English', 'Spanish'). Nothing else.",
            f"What language are most of these comments written in?\n\n{sample_text}",
            max_tokens=16,
        ).strip().strip(".").lower()
    except Exception as e:
        log.warning(f"commenter_scoring: creator language detection failed: {e}, using English prompt")
        return _CREATOR_BATCH_PROMPT, _CREATOR_CONSOLIDATE_PROMPT, _DEFENSIVENESS_PROMPT

    batch = _CREATOR_BATCH_PROMPTS.get(lang, _CREATOR_BATCH_PROMPT)
    consolidate = _CREATOR_CONSOLIDATE_PROMPTS.get(lang, _CREATOR_CONSOLIDATE_PROMPT)
    defensiveness = _DEFENSIVENESS_PROMPTS.get(lang, _DEFENSIVENESS_PROMPT)
    log.info(f"commenter_scoring: creator prompts localised to '{lang}'")
    return batch, consolidate, defensiveness


def _score_creator_detailed(
    conn, community_id: int,
    author_channel_id: str, author_name: str,
    llm, channel_ids: list[str],
    system_prompt_batch: str, system_prompt_consolidate: str,
    current_backend: str, current_model: str,
) -> bool:
    """
    Deep-analyze one creator's comments.
    ≤500 comments: all in batches of 50 → consolidation call.
    >500 comments: chunks of 100 → convergence detection (delta < 0.05).
    Returns True if scored successfully.
    """
    ph = ",".join("?" * len(channel_ids))
    comment_rows = conn.execute(
        f"SELECT text FROM comments "
        f"WHERE channel_id IN ({ph}) AND author_channel_id = ? "
        f"AND text IS NOT NULL AND LENGTH(text) > 10 "
        f"ORDER BY like_count DESC",
        channel_ids + [author_channel_id],
    ).fetchall()

    comments = [r["text"] for r in comment_rows]
    total = len(comments)
    if total == 0:
        return False

    log.info(f"commenter_scoring: detailed creator analysis — {author_name} ({total} comments)")

    def _clamp(v):
        try:
            return round(min(max(float(v), 0.0), 1.0), 4)
        except (TypeError, ValueError):
            return None

    def _single_batch(batch: list[str]) -> dict | None:
        """Submit one batch of comments, return {pol, con, dep, reason} or None."""
        comment_block = "\n".join(f"  [{i+1}] {c[:300]}" for i, c in enumerate(batch))
        user_prompt = f"Creator: {author_name}\n\nComments ({len(batch)}):\n{comment_block}"
        try:
            result = llm.complete_json(system_prompt_batch, user_prompt, max_tokens=256)
            if not isinstance(result, dict):
                return None
            pol = _clamp(result.get("politeness"))
            con = _clamp(result.get("constructiveness"))
            dep = _clamp(result.get("depth"))
            if pol is None or con is None or dep is None:
                return None
            return {"politeness": pol, "constructiveness": con,
                    "depth": dep, "reason": result.get("reason", "")}
        except Exception as e:
            log.warning(f"commenter_scoring: creator batch failed for {author_name}: {e}")
            return None

    if total <= 500:
        BATCH = 50
        batch_results = []
        for start in range(0, total, BATCH):
            r = _single_batch(comments[start: start + BATCH])
            if r:
                batch_results.append(r)

        if not batch_results:
            return False

        if len(batch_results) == 1:
            final = batch_results[0]
        else:
            batches_text = "\n".join(
                f"Batch {i+1}: politeness={r['politeness']}, "
                f"constructiveness={r['constructiveness']}, depth={r['depth']}. {r['reason']}"
                for i, r in enumerate(batch_results)
            )
            user_prompt = (
                f"Creator: {author_name}\nTotal comments analyzed: {total}\n\n"
                f"Batch assessments:\n{batches_text}"
            )
            try:
                result = llm.complete_json(system_prompt_consolidate, user_prompt, max_tokens=256)
                if isinstance(result, dict):
                    pol = _clamp(result.get("politeness"))
                    con = _clamp(result.get("constructiveness"))
                    dep = _clamp(result.get("depth"))
                    if pol is not None and con is not None and dep is not None:
                        final = {"politeness": pol, "constructiveness": con,
                                 "depth": dep, "reason": result.get("reason", "")}
                    else:
                        raise ValueError("incomplete consolidation result")
                else:
                    raise ValueError("non-dict consolidation result")
            except Exception as e:
                log.warning(f"commenter_scoring: consolidation fallback to mean for {author_name}: {e}")
                final = {
                    "politeness":      round(sum(r["politeness"]      for r in batch_results) / len(batch_results), 4),
                    "constructiveness": round(sum(r["constructiveness"] for r in batch_results) / len(batch_results), 4),
                    "depth":           round(sum(r["depth"]            for r in batch_results) / len(batch_results), 4),
                    "reason":          batch_results[-1]["reason"],
                }
    else:
        # Convergence-based: 100 comments at a time, stop when stable
        BATCH = 100
        CONVERGENCE = 0.05
        running_pol = running_con = running_dep = None
        running_reason = ""
        chunks = 0

        for start in range(0, total, BATCH):
            result = _single_batch(comments[start: start + BATCH])
            if result is None:
                continue

            if running_pol is None:
                running_pol = result["politeness"]
                running_con = result["constructiveness"]
                running_dep = result["depth"]
                running_reason = result["reason"]
                chunks += 1
                continue

            prev_pol, prev_con, prev_dep = running_pol, running_con, running_dep
            n = chunks + 1
            running_pol = (running_pol * chunks + result["politeness"])      / n
            running_con = (running_con * chunks + result["constructiveness"]) / n
            running_dep = (running_dep * chunks + result["depth"])            / n
            running_reason = result["reason"]
            chunks += 1

            delta = max(
                abs(running_pol - prev_pol),
                abs(running_con - prev_con),
                abs(running_dep - prev_dep),
            )
            log.info(
                f"commenter_scoring: {author_name} chunk {chunks} "
                f"({min(start+BATCH, total)}/{total} comments), delta={delta:.4f}"
            )
            if delta < CONVERGENCE:
                log.info(f"commenter_scoring: converged after {chunks} chunks for {author_name}")
                break

        if running_pol is None:
            return False

        final = {
            "politeness":      round(running_pol, 4),
            "constructiveness": round(running_con, 4),
            "depth":           round(running_dep, 4),
            "reason":          running_reason,
        }

    avg = round((final["politeness"] + final["constructiveness"] + final["depth"]) / 3, 4)
    conn.execute(
        "UPDATE commenter_scores "
        "SET llm_tone_score = ?, "
        "    llm_politeness_score = ?, "
        "    llm_constructiveness_score = ?, "
        "    llm_depth_score = ?, "
        "    llm_tone_reason = ?, "
        "    llm_tone_backend = ?, llm_tone_model = ? "
        "WHERE community_id = ? AND author_channel_id = ?",
        (avg, final["politeness"], final["constructiveness"], final["depth"],
         final["reason"], current_backend, current_model, community_id, author_channel_id),
    )
    conn.commit()
    return True


def _assess_creator_defensiveness(
    conn, community_id: int,
    author_channel_id: str, author_name: str,
    llm, channel_ids: list[str],
    system_prompt: str,
    current_backend: str, current_model: str,
) -> bool:
    """
    Assess defensiveness for a single creator using a focused prompt.
    Prioritises reply comments (where defensive behaviour surfaces) and
    tops up with regular comments to reach up to 60 samples total.
    Returns True if the score was stored successfully.
    """
    ph = ",".join("?" * len(channel_ids))

    # Fetch replies first (defensiveness shows in responses to others)
    reply_rows = conn.execute(
        f"SELECT text FROM comments "
        f"WHERE channel_id IN ({ph}) AND author_channel_id = ? AND is_reply = 1 "
        f"AND text IS NOT NULL AND LENGTH(text) > 10 "
        f"ORDER BY like_count DESC LIMIT 60",
        channel_ids + [author_channel_id],
    ).fetchall()
    replies = [r["text"] for r in reply_rows]

    # Top up with regular comments if fewer than 20 replies
    if len(replies) < 20:
        needed = 60 - len(replies)
        other_rows = conn.execute(
            f"SELECT text FROM comments "
            f"WHERE channel_id IN ({ph}) AND author_channel_id = ? AND is_reply = 0 "
            f"AND text IS NOT NULL AND LENGTH(text) > 10 "
            f"ORDER BY like_count DESC LIMIT {needed}",
            channel_ids + [author_channel_id],
        ).fetchall()
        comments = replies + [r["text"] for r in other_rows]
    else:
        comments = replies

    if not comments:
        return False

    log.info(
        f"commenter_scoring: defensiveness assessment — {author_name} "
        f"({len(replies)} replies + {len(comments)-len(replies)} other comments)"
    )

    comment_block = "\n".join(f"  [{i+1}] {c[:300]}" for i, c in enumerate(comments))
    user_prompt = f"Creator: {author_name}\n\nComments/replies ({len(comments)}):\n{comment_block}"

    try:
        result = llm.complete_json(system_prompt, user_prompt, max_tokens=256)
        if not isinstance(result, dict):
            log.warning(f"commenter_scoring: defensiveness — non-dict response for {author_name}")
            return False

        def _clamp(v):
            try:
                return round(min(max(float(v), 0.0), 1.0), 4)
            except (TypeError, ValueError):
                return None

        score = _clamp(result.get("defensiveness"))
        reason = result.get("reason", "")
        if score is None:
            log.warning(f"commenter_scoring: defensiveness — no score returned for {author_name}: {result}")
            return False

        conn.execute(
            "UPDATE commenter_scores "
            "SET llm_defensiveness_score = ?, llm_defensiveness_reason = ? "
            "WHERE community_id = ? AND author_channel_id = ?",
            (score, reason, community_id, author_channel_id),
        )
        conn.commit()
        log.info(f"commenter_scoring: defensiveness={score:.2f} for {author_name} — {reason}")
        return True

    except Exception as e:
        log.warning(f"commenter_scoring: defensiveness assessment failed for {author_name}: {e}")
        return False


def score_community_creators_detailed(conn, community_id: int,
                                       progress_callback=None) -> int:
    """
    Run detailed per-creator tone analysis for all channel owners in the community.
    Each creator gets an individual deep analysis (all their comments, with convergence
    for large accounts). Must be called after score_community().
    Returns the number of creators successfully scored.
    """
    settings = get_all_settings(conn)
    cfg = _settings_to_llm_config(settings)
    llm = LLMClient(cfg, role="tone")
    current_backend = llm.backend
    current_model = llm._model

    system_prompt_batch, system_prompt_consolidate, system_prompt_defensiveness = \
        _localise_creator_prompts(conn, community_id, llm)

    channel_ids = get_community_channel_ids(conn, community_id)
    if not channel_ids:
        return 0

    creator_ids = set(channel_ids)
    ph = ",".join("?" * len(creator_ids))
    rows = conn.execute(
        f"SELECT author_channel_id, author_name FROM commenter_scores "
        f"WHERE community_id = ? AND author_channel_id IN ({ph}) "
        f"ORDER BY quality_score DESC",
        (community_id, *creator_ids),
    ).fetchall()

    if not rows:
        return 0

    total_scored = 0
    for i, row in enumerate(rows):
        if progress_callback:
            progress_callback(i, len(rows) * 2)  # *2: tone + defensiveness passes
        success = _score_creator_detailed(
            conn, community_id,
            row["author_channel_id"], row["author_name"],
            llm, channel_ids,
            system_prompt_batch, system_prompt_consolidate,
            current_backend, current_model,
        )
        if success:
            total_scored += 1
        # Defensiveness pass — independent of tone success
        _assess_creator_defensiveness(
            conn, community_id,
            row["author_channel_id"], row["author_name"],
            llm, channel_ids,
            system_prompt_defensiveness,
            current_backend, current_model,
        )

    if total_scored > 0:
        # Recompute quality_score / tier with all new LLM scores
        stats = _load_commenter_stats(conn, channel_ids)
        tone_map = {
            r["author_channel_id"]: {
                "llm_tone_score":              r["llm_tone_score"],
                "llm_politeness_score":        r["llm_politeness_score"],
                "llm_constructiveness_score":  r["llm_constructiveness_score"],
                "llm_depth_score":             r["llm_depth_score"],
                "llm_tone_reason":             r["llm_tone_reason"],
                "llm_tone_backend":            r["llm_tone_backend"],
                "llm_tone_model":              r["llm_tone_model"],
                "llm_defensiveness_score":     r["llm_defensiveness_score"],
                "llm_defensiveness_reason":    r["llm_defensiveness_reason"],
            }
            for r in conn.execute(
                "SELECT author_channel_id, llm_tone_score, llm_politeness_score, "
                "llm_constructiveness_score, llm_depth_score, llm_tone_reason, "
                "llm_tone_backend, llm_tone_model, "
                "llm_defensiveness_score, llm_defensiveness_reason "
                "FROM commenter_scores WHERE community_id = ?",
                (community_id,),
            ).fetchall()
        }
        for s in stats:
            t = tone_map.get(s["author_channel_id"], {})
            if t.get("llm_tone_score") is not None:
                s["llm_tone_score"]             = t["llm_tone_score"]
                s["llm_politeness_score"]       = t["llm_politeness_score"]
                s["llm_constructiveness_score"] = t["llm_constructiveness_score"]
                s["llm_depth_score"]            = t["llm_depth_score"]
                s["llm_tone_reason"]            = t["llm_tone_reason"]
                s["llm_tone_backend"]           = t["llm_tone_backend"]
                s["llm_tone_model"]             = t["llm_tone_model"]
            if t.get("llm_defensiveness_score") is not None:
                s["llm_defensiveness_score"]    = t["llm_defensiveness_score"]
                s["llm_defensiveness_reason"]   = t["llm_defensiveness_reason"]

        enriched = _compute_component_scores(stats)
        conn.execute("DELETE FROM commenter_scores WHERE community_id = ?", (community_id,))
        conn.executemany(
            """INSERT INTO commenter_scores
                   (community_id, author_channel_id, author_name,
                    quality_score, tier,
                    avg_engagement_norm, channel_spread_score, like_ratio_score,
                    factual_anchor_score, avg_length_score, vocab_richness_score,
                    llm_tone_score, llm_politeness_score, llm_constructiveness_score, llm_depth_score,
                    llm_tone_reason, llm_tone_backend, llm_tone_model,
                    llm_defensiveness_score, llm_defensiveness_reason,
                    reply_penalty, reply_ratio,
                    comment_count, channel_count, total_likes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    community_id,
                    r["author_channel_id"],
                    r["author_name"],
                    r["quality_score"],
                    r["tier"],
                    r["avg_eng_score"],
                    r["channel_spread_score"],
                    r["like_ratio_score"],
                    r["factual_anchor_score"],
                    r["avg_length_score"],
                    r["vocab_richness_score"],
                    r.get("llm_tone_score"),
                    r.get("llm_politeness_score"),
                    r.get("llm_constructiveness_score"),
                    r.get("llm_depth_score"),
                    r.get("llm_tone_reason"),
                    r.get("llm_tone_backend"),
                    r.get("llm_tone_model"),
                    r.get("llm_defensiveness_score"),
                    r.get("llm_defensiveness_reason"),
                    r["reply_penalty"],
                    r["reply_ratio"],
                    r["comment_count"],
                    r["channel_count"],
                    r["total_likes"],
                )
                for r in enriched
            ],
        )
        conn.commit()

    if progress_callback:
        progress_callback(len(rows), len(rows))

    log.info(
        f"commenter_scoring: detailed creator analysis done — "
        f"{total_scored}/{len(rows)} creators scored for community {community_id}"
    )
    return total_scored


def get_scores_for_community(conn, community_id: int) -> dict[str, dict]:
    """
    Return {author_channel_id: score_row} for fast lookup during batch formatting.
    Returns empty dict if no scores have been computed yet.
    """
    rows = conn.execute(
        "SELECT * FROM commenter_scores WHERE community_id = ?",
        (community_id,),
    ).fetchall()
    return {r["author_channel_id"]: dict(r) for r in rows}
