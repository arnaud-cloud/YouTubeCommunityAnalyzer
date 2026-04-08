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

For each numbered commenter, rate their overall SUBSTANCE on a single 0.0–1.0 scale.
Substance combines what they contribute (facts, questions, nuance) AND how deeply they engage (specific references, developed reasoning vs. surface reactions).

  0.0 = purely emotional, sycophantic, or complaining — zero substance
  0.3 = minimal: a very short or generic reaction with nothing added
  0.5 = some engagement but stays superficial, or neutral fan interaction
  0.7 = adds genuine value: a fact, question, or developed point
  1.0 = highly analytical: precise references, original perspective, developed reasoning

Required output format (JSON only, no other text):
{"scores": [{"index": 1, "constructiveness": 0.7, "reason": "one sentence in English"}, ...]}

Use the integer index shown before each commenter's name. One entry per commenter."""

_TONE_SYSTEM_PROMPT_FR = """\
IMPORTANT : Vous devez répondre uniquement en JSON. Pas de prose, pas d'explications, pas de questions, pas de markdown. Uniquement l'objet JSON.

Vous êtes un évaluateur de qualité de commentaires. Les commentaires peuvent être dans n'importe quelle langue — évaluez-les tels quels et répondez toujours au format JSON ci-dessous.

Pour chaque commentateur numéroté, évaluez leur style de commentaire global sur une échelle de 0,0 à 1,0 selon TROIS dimensions indépendantes :

Pour chaque commentateur numéroté, évaluez leur SUBSTANCE globale sur une échelle unique de 0,0 à 1,0.
La substance combine ce qu'ils apportent (faits, questions, nuances) ET la profondeur de leur engagement (références précises, raisonnement développé vs. réactions de surface).

  0,0 = purement émotionnel, adulateur ou plaintif — aucune substance
  0,3 = minimal : réaction très courte ou générique, rien d'ajouté
  0,5 = quelque engagement mais superficiel, ou interaction neutre de fan
  0,7 = apporte une vraie valeur : un fait, une question ou un point développé
  1,0 = très analytique : références précises, perspective originale, raisonnement développé

Format de sortie requis (JSON uniquement, aucun autre texte) :
{"scores": [{"index": 1, "constructiveness": 0.7, "reason": "une phrase en français"}, ...]}

Utilisez l'index entier affiché avant le nom de chaque commentateur. Une entrée par commentateur."""

_TONE_SYSTEM_PROMPT_ES = """\
IMPORTANTE: Debes responder únicamente con JSON. Sin prosa, sin explicaciones, sin preguntas, sin markdown. Solo el objeto JSON.

Eres un evaluador de calidad de comentarios. Los comentarios pueden estar en cualquier idioma — evalúalos tal como están y responde siempre en el formato JSON a continuación.

Para cada comentarista numerado, califica su estilo en una escala de 0,0 a 1,0 según TRES dimensiones independientes:

Para cada comentarista numerado, califica su SUSTANCIA global en una escala única de 0,0 a 1,0.
La sustancia combina lo que aportan (hechos, preguntas, matices) Y la profundidad de su participación (referencias específicas, razonamiento desarrollado vs. reacciones superficiales).

  0,0 = puramente emocional, adulador o quejoso — cero sustancia
  0,3 = mínimo: reacción muy corta o genérica, sin nada añadido
  0,5 = alguna participación pero superficial, o interacción neutra de fan
  0,7 = aporta valor genuino: un hecho, una pregunta o un punto desarrollado
  1,0 = muy analítico: referencias precisas, perspectiva original, razonamiento desarrollado

Formato de salida requerido (solo JSON, sin otro texto):
{"scores": [{"index": 1, "constructiveness": 0.7, "reason": "una frase en español"}, ...]}

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
Rate their overall SUBSTANCE on a single 0.0–1.0 scale for this batch.
Substance combines what they contribute (facts, questions, nuance) AND how deeply they engage (specific references, developed reasoning vs. surface reactions).

  0.0 = no substance — purely promotional, self-serving, or content-free
  0.3 = minimal engagement, generic or very short, nothing added
  0.5 = some engagement but stays superficial
  0.7 = adds genuine value: a fact, question, or developed point
  1.0 = highly analytical: precise references, original perspective, developed reasoning

Respond with JSON only:
{"constructiveness": 0.7, "reason": "one sentence characterizing this batch"}"""

_CREATOR_BATCH_PROMPT_FR = """\
IMPORTANT : Répondez uniquement en JSON. Pas de prose, pas d'explications, pas de markdown.

Vous effectuez une analyse approfondie du comportement de commentaires d'un seul créateur YouTube.
Évaluez leur SUBSTANCE globale sur une échelle unique de 0,0 à 1,0 pour ce lot.
La substance combine ce qu'ils apportent (faits, questions, nuances) ET la profondeur de leur engagement (références précises, raisonnement développé vs. réactions de surface).

  0,0 = aucune substance — purement promotionnel, égocentrique ou sans contenu
  0,3 = engagement minimal, générique ou très court, rien d'ajouté
  0,5 = quelque engagement mais superficiel
  0,7 = apporte une vraie valeur : un fait, une question ou un point développé
  1,0 = très analytique : références précises, perspective originale, raisonnement développé

Répondez uniquement en JSON :
{"constructiveness": 0.7, "reason": "une phrase caractérisant ce lot"}"""

_CREATOR_BATCH_PROMPT_ES = """\
IMPORTANTE: Responde únicamente con JSON. Sin prosa, sin explicaciones, sin markdown.

Estás realizando un análisis detallado del comportamiento de comentarios de un único creador de YouTube.
Califica su SUSTANCIA global en una escala única de 0,0 a 1,0 para este lote.
La sustancia combina lo que aportan (hechos, preguntas, matices) Y la profundidad de su participación (referencias específicas, razonamiento desarrollado vs. reacciones superficiales).

  0,0 = sin sustancia — puramente promocional, egocéntrico o sin contenido
  0,3 = participación mínima, genérica o muy corta, sin nada añadido
  0,5 = alguna participación pero superficial
  0,7 = aporta valor genuino: un hecho, una pregunta o un punto desarrollado
  1,0 = muy analítico: referencias precisas, perspectiva original, razonamiento desarrollado

Responde únicamente con JSON:
{"constructiveness": 0.7, "reason": "una frase que caracterice este lote"}"""

_CREATOR_CONSOLIDATE_PROMPT = """\
IMPORTANT: Respond with JSON only. No prose, no explanations, no markdown.

You have analyzed a YouTube creator's comments across multiple batches.
Below are the partial substance scores from each batch.

Synthesize into a single final score. Weight all batches equally unless you notice a clear trend.

Respond with JSON only:
{"constructiveness": 0.7, "reason": "one sentence final synthesis"}"""

_CREATOR_CONSOLIDATE_PROMPT_FR = """\
IMPORTANT : Répondez uniquement en JSON. Pas de prose, pas d'explications, pas de markdown.

Vous avez analysé les commentaires d'un créateur YouTube en plusieurs lots.
Voici les scores de substance partiels de chaque lot.

Synthétisez en un score final unique. Pondérez tous les lots de manière égale sauf si vous observez une tendance claire.

Répondez uniquement en JSON :
{"constructiveness": 0.7, "reason": "une phrase de synthèse finale"}"""

_CREATOR_CONSOLIDATE_PROMPT_ES = """\
IMPORTANTE: Responde únicamente con JSON. Sin prosa, sin explicaciones, sin markdown.

Has analizado los comentarios de un creador de YouTube en múltiples lotes.
A continuación se muestran las puntuaciones de sustancia parciales de cada lote.

Sintetiza en una puntuación final única. Pondera todos los lotes por igual salvo tendencia clara.

Responde únicamente con JSON:
{"constructiveness": 0.7, "reason": "una frase de síntesis final"}"""

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

You are analyzing whether a YouTube creator shows ego-revealing behavior in their public comments.

CONTEXT: This creator presents themselves as spiritually advanced — calm, non-reactive, beyond ego.
Your job is to find where this mask slips. Look for TWO types of behavior, both count equally:

DEFENSIVENESS — reacting to challenge or criticism:
- Dismissing or belittling commenters who question, criticize, or challenge them
- Using spiritual language as a shield ("you're not at my level", "low vibration energy", "that's your projection")
- Passive-aggressive responses — outwardly calm but subtly cutting
- Claiming detachment while visibly reacting (long rebuttals, sarcasm, condescension)
- Shutting down dialogue with authority claims rather than engaging the substance
- Needing the last word, especially after being challenged

BRAGGING — unsolicited self-promotion or status signalling:
- Unprompted mentions of their own achievements, follower count, income, or influence
- Dropping credentials or testimonials to establish superiority ("thousands of students have transformed...")
- Comparing themselves favourably to others, implicitly or explicitly
- Spiritual one-upmanship ("at my level of consciousness...", "after years of deep practice...")
- Turning other people's questions or struggles into an opportunity to showcase themselves

COMBINED SCORE — count instances of BOTH types together into a single ego_score:
  0.0        = none found. DEFAULT: use this when you cannot cite a specific example.
  0.2 – 0.49 = subtle instances only — ambiguous, no clear case. More subtle instances → higher in this range.
  0.5        = one clear, unmistakable instance (base score; additional subtle cases push slightly above 0.5)
  0.65       = one clear instance + accumulation of subtle ones
  0.75       = two clear instances (subtle cases also add up within this range)
  1.0        = three or more clear instances

RULES:
- If you cannot point to a specific comment or exchange as evidence, score 0.0.
- "Clear instance" = unmistakable behavior that a neutral observer would agree on.
- "Subtle instance" = could be interpreted charitably, but leans defensive or boastful.
- Answering a direct question about oneself is NOT bragging. Correcting misinformation is NOT defensiveness.
- Output ONE single JSON object. Do NOT produce separate objects for each behavior type.

Respond with a single JSON object only:
{"defensiveness": 0.0, "instances": 0, "reason": "max one sentence — cite comment numbers as evidence (e.g. '[12] dismissive reply'), or state none found"}"""

_DEFENSIVENESS_PROMPT_FR = """\
IMPORTANT : Répondez uniquement en JSON. Pas de prose, pas d'explications, pas de markdown.

Vous analysez si un créateur YouTube révèle son ego dans ses commentaires publics.

CONTEXTE : Ce créateur se présente comme spirituellement avancé — calme, non-réactif, au-delà de l'ego.
Votre rôle est de trouver où ce masque se fissure. Recherchez DEUX types de comportements, les deux comptent également :

DÉFENSIVITÉ — réaction à une critique ou un défi :
- Rejeter ou dénigrer les commentateurs qui les questionnent, critiquent ou défient
- Utiliser le langage spirituel comme bouclier ("vous n'êtes pas à mon niveau", "basse vibration", "c'est votre projection")
- Réponses passives-agressives — apparemment calmes mais subtilement blessantes
- Prétendre au détachement tout en réagissant visiblement (longues réfutations, sarcasme, condescendance)
- Fermer le dialogue par des affirmations d'autorité plutôt qu'en s'engageant sur le fond
- Avoir besoin d'avoir le dernier mot, surtout après avoir été challengé

VANTARDISE — auto-promotion ou signalisation de statut non sollicitées :
- Mentions non sollicitées de leurs propres réalisations, nombre d'abonnés, revenus ou influence
- Utilisation de références ou témoignages pour établir leur supériorité ("des milliers d'étudiants ont été transformés...")
- Se comparer favorablement aux autres, implicitement ou explicitement
- Surenchère spirituelle ("à mon niveau de conscience...", "après des années de pratique profonde...")
- Transformer les questions ou difficultés des autres en occasion de se mettre en valeur

SCORE COMBINÉ — comptez les instances des DEUX types ensemble dans un seul ego_score :
  0,0         = aucune trouvée. VALEUR PAR DÉFAUT : utilisez-la si vous ne pouvez pas citer d'exemple concret.
  0,2 – 0,49  = instances subtiles uniquement — ambiguës, aucun cas clair. Plus d'instances → score plus haut.
  0,5         = un cas clair et indiscutable (score de base ; des cas subtils supplémentaires poussent légèrement au-dessus)
  0,65        = un cas clair + accumulation d'instances subtiles
  0,75        = deux cas clairs (les cas subtils s'accumulent aussi)
  1,0         = trois cas clairs ou plus

RÈGLES :
- Si vous ne pouvez pas pointer un commentaire ou échange précis, notez 0,0.
- "Cas clair" = comportement indiscutable qu'un observateur neutre reconnaîtrait.
- "Instance subtile" = pourrait être interprétée charitablement, mais penche vers la défensivité ou la vantardise.
- Répondre à une question directe sur soi n'est PAS de la vantardise. Corriger une erreur n'est PAS de la défensivité.
- Produisez UN SEUL objet JSON. Ne produisez PAS d'objets séparés pour chaque type de comportement.

Répondez avec un seul objet JSON :
{"defensiveness": 0.0, "instances": 0, "reason": "une phrase max — citez les numéros de commentaires comme preuves (ex. '[12] réponse dismissive'), ou indiquez qu'aucune n'a été trouvée"}"""

_DEFENSIVENESS_PROMPT_ES = """\
IMPORTANTE: Responde únicamente con JSON. Sin prosa, sin explicaciones, sin markdown.

Estás analizando si un creador de YouTube revela su ego en sus comentarios públicos.

CONTEXTO: Este creador se presenta como espiritualmente avanzado — tranquilo, no reactivo, más allá del ego.
Tu trabajo es encontrar dónde se rompe esta máscara. Busca DOS tipos de comportamiento, ambos cuentan por igual:

DEFENSIVIDAD — reacción a críticas o desafíos:
- Desestimar o menospreciar a los comentaristas que los cuestionan, critican o desafían
- Usar el lenguaje espiritual como escudo ("no estás en mi nivel", "baja vibración", "eso es tu proyección")
- Respuestas pasivo-agresivas — aparentemente tranquilas pero sutilmente hirientes
- Alegar desapego mientras se reacciona visiblemente (largas réplicas, sarcasmo, condescendencia)
- Cerrar el diálogo con afirmaciones de autoridad en lugar de abordar el fondo
- Necesitar tener la última palabra, especialmente tras ser desafiados

FANFARRONERÍA — autopromoción o señalización de estatus no solicitadas:
- Menciones no solicitadas de sus logros, número de seguidores, ingresos o influencia
- Uso de credenciales o testimonios para establecer superioridad ("miles de estudiantes se han transformado...")
- Compararse favorablemente con otros, implícita o explícitamente
- Superioridad espiritual ("a mi nivel de conciencia...", "tras años de práctica profunda...")
- Convertir las preguntas o dificultades de otros en una oportunidad para destacarse

PUNTUACIÓN COMBINADA — cuenta instancias de AMBOS tipos juntas en una sola ego_score:
  0,0        = ninguna encontrada. VALOR POR DEFECTO: úsalo cuando no puedas citar un ejemplo concreto.
  0,2 – 0,49 = instancias sutiles únicamente — ambiguas, sin caso claro. Más instancias → más alto en el rango.
  0,5        = una instancia clara e inconfundible (puntuación base; casos sutiles adicionales empujan ligeramente por encima)
  0,65       = una instancia clara + acumulación de casos sutiles
  0,75       = dos instancias claras (los casos sutiles también se acumulan)
  1,0        = tres o más instancias claras

REGLAS:
- Si no puedes señalar un comentario o intercambio específico, puntúa 0,0.
- "Instancia clara" = comportamiento inconfundible que un observador neutral reconocería.
- "Instancia sutil" = podría interpretarse favorablemente, pero se inclina hacia la defensividad o fanfarronería.
- Responder una pregunta directa sobre uno mismo NO es fanfarronería. Corregir información errónea NO es defensividad.
- Produce UN SOLO objeto JSON. NO produzcas objetos separados para cada tipo de comportamiento.

Responde con un único objeto JSON:
{"defensiveness": 0.0, "instances": 0, "reason": "máximo una frase — cita números de comentarios como evidencia (ej. '[12] respuesta desestimadora'), o indica que no se encontró ninguna"}"""

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

            con = _clamp(item.get("constructiveness"))
            # Fall back to legacy keys if needed
            if con is None:
                con = _clamp(item.get("score"))
            if con is None:
                dep = _clamp(item.get("depth"))
                pol = _clamp(item.get("politeness"))
                parts = [x for x in (pol, dep) if x is not None]
                con = round(sum(parts) / len(parts), 4) if parts else None

            if con is None:
                return False

            conn.execute(
                "UPDATE commenter_scores "
                "SET llm_tone_score = ?, "
                "    llm_politeness_score = NULL, "
                "    llm_constructiveness_score = ?, "
                "    llm_depth_score = NULL, "
                "    llm_tone_reason = ?, "
                "    llm_tone_backend = ?, llm_tone_model = ? "
                "WHERE community_id = ? AND author_channel_id = ?",
                (con, con, reason, current_backend, current_model, community_id, aid),
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
        """Submit one batch, return {constructiveness, reason} or None."""
        comment_block = "\n".join(f"  [{i+1}] {c[:300]}" for i, c in enumerate(batch))
        user_prompt = f"Creator: {author_name}\n\nComments ({len(batch)}):\n{comment_block}"
        try:
            result = llm.complete_json(system_prompt_batch, user_prompt, max_tokens=256)
            if not isinstance(result, dict):
                return None
            con = _clamp(result.get("constructiveness"))
            if con is None:
                return None
            return {"constructiveness": con, "reason": result.get("reason", "")}
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
                f"Batch {i+1}: substance={r['constructiveness']}. {r['reason']}"
                for i, r in enumerate(batch_results)
            )
            user_prompt = (
                f"Creator: {author_name}\nTotal comments analyzed: {total}\n\n"
                f"Batch assessments:\n{batches_text}"
            )
            try:
                result = llm.complete_json(system_prompt_consolidate, user_prompt, max_tokens=256)
                if isinstance(result, dict):
                    con = _clamp(result.get("constructiveness"))
                    if con is not None:
                        final = {"constructiveness": con, "reason": result.get("reason", "")}
                    else:
                        raise ValueError("incomplete consolidation result")
                else:
                    raise ValueError("non-dict consolidation result")
            except Exception as e:
                log.warning(f"commenter_scoring: consolidation fallback to mean for {author_name}: {e}")
                final = {
                    "constructiveness": round(sum(r["constructiveness"] for r in batch_results) / len(batch_results), 4),
                    "reason": batch_results[-1]["reason"],
                }
    else:
        # Convergence-based: 100 comments at a time, stop when stable
        BATCH = 100
        CONVERGENCE = 0.05
        running_con = None
        running_reason = ""
        chunks = 0

        for start in range(0, total, BATCH):
            result = _single_batch(comments[start: start + BATCH])
            if result is None:
                continue

            if running_con is None:
                running_con = result["constructiveness"]
                running_reason = result["reason"]
                chunks += 1
                continue

            prev_con = running_con
            running_con = (running_con * chunks + result["constructiveness"]) / (chunks + 1)
            running_reason = result["reason"]
            chunks += 1

            delta = abs(running_con - prev_con)
            log.info(
                f"commenter_scoring: {author_name} chunk {chunks} "
                f"({min(start+BATCH, total)}/{total} comments), delta={delta:.4f}"
            )
            if delta < CONVERGENCE:
                log.info(f"commenter_scoring: converged after {chunks} chunks for {author_name}")
                break

        if running_con is None:
            return False

        final = {"constructiveness": round(running_con, 4), "reason": running_reason}

    con = final["constructiveness"]
    conn.execute(
        "UPDATE commenter_scores "
        "SET llm_tone_score = ?, "
        "    llm_politeness_score = NULL, "
        "    llm_constructiveness_score = ?, "
        "    llm_depth_score = NULL, "
        "    llm_tone_reason = ?, "
        "    llm_tone_backend = ?, llm_tone_model = ? "
        "WHERE community_id = ? AND author_channel_id = ?",
        (con, con, final["reason"], current_backend, current_model, community_id, author_channel_id),
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
        result = llm.complete_json(system_prompt, user_prompt, max_tokens=512)
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
    llm = LLMClient(cfg, role="creator")
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
            progress_callback(i, len(rows))
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
