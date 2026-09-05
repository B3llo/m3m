"""Shared lightweight text-similarity helpers (deterministic, stdlib only)."""

import re
import unicodedata

_STOPWORDS = {
    "a", "as", "ao", "com", "da", "das", "de", "do", "dos", "e", "em",
    "for", "from", "is", "it", "na", "nas", "no", "nos", "o", "os", "para",
    "que", "this", "the", "to", "um", "uma", "use", "used", "usar", "usa",
}

_TOKEN_PATTERN = re.compile(r"[^\W_]+", re.UNICODE)

# FTS5 bare terms collide with these operators; prefix expansion skips them.
_FTS_RESERVED = frozenset({"AND", "OR", "NOT", "NEAR"})


def meaningful_tokens(text: str) -> set:
    """Content-bearing tokens for lightweight similarity matching."""
    tokens = _TOKEN_PATTERN.findall(text.casefold())
    return {token for token in tokens if len(token) > 2 and token not in _STOPWORDS}


def jaccard(a: set, b: set) -> float:
    """Token-set overlap in [0, 1]; 1.0 means identical token sets."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def fold_accents(text: str) -> str:
    """Lowercase and strip diacritics so 'aprovação' matches 'aprovacao'."""
    normalized = unicodedata.normalize("NFD", (text or "").casefold())
    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")


# Seed synonym groups: one concept per group, English first (pivot language),
# Portuguese second (focus language), then starter terms in Spanish, French,
# German and Italian. Lookup is bidirectional and accent-insensitive, so any
# language in a group bridges to every other. New languages plug in by adding
# terms to these groups (or via synonyms.md overlays) — no code changes.
# Deliberately excluded: cross-language false friends (e.g. FR "file" means
# queue, EN "file" means arquivo) and ambiguous words without a dominant
# developer-context meaning.
SYNONYM_SEED = [
    ("refund", "refunds", "reembolso", "reembolsos", "reembolso", "rimborso", "Rückerstattung", "Erstattung", "remboursement"),
    ("deadline", "prazo", "prazos", "plazo", "scadenza", "Frist", "délai"),
    ("approval", "aprovação", "aprovacao", "aprobación", "aprobacion", "approvazione", "approbation", "Genehmigung"),
    ("approve", "aprovar", "aprobar", "approvare", "approuver", "genehmigen"),
    ("reject", "rejeitar", "rechazar", "rifiutare", "rejeter", "ablehnen"),
    ("restore", "restaurar", "restaurar", "ripristinare", "restaurer", "wiederherstellen"),
    ("archive", "arquivar", "archivar", "archiviare", "archiver", "archivieren"),
    ("password", "senha", "senhas", "contraseña", "password", "mot de passe", "Passwort"),
    ("credential", "credentials", "credencial", "credenciais", "credenciales", "credenziale", "identifiant"),
    ("secret", "segredo", "secreto", "segreto", "Geheimnis"),
    ("vault", "cofre", "caja fuerte", "cassaforte", "coffre-fort", "Tresor"),
    ("key", "chave", "clave", "chiave", "clé", "Schlüssel"),
    ("chargeback", "chargebacks", "estorno", "contracargo", "chargeback", "ritorno", "Rückbuchung"),
    ("discount", "desconto", "descuento", "sconto", "remise", "Rabatt"),
    ("deploy", "deployment", "implantação", "implantacao", "implementación", "distribuzione", "déploiement", "Bereitstellung"),
    ("production", "produção", "producao", "producción", "produzione", "production", "Produktion"),
    ("environment", "ambiente", "entorno", "ambiente", "environnement", "Umgebung"),
    ("version", "versão", "versao", "versión", "versione", "Version"),
    ("failure", "falha", "falhas", "fallo", "fallimento", "échec", "Fehler"),
    ("queue", "fila", "filas", "cola", "coda", "Warteschlange"),
    ("error", "erro", "error", "errore", "erreur", "Fehler"),
    ("warning", "aviso", "aviso", "avviso", "avertissement", "Warnung"),
    ("fix", "correção", "correcao", "corrección", "correzione", "correctif", "Korrektur"),
    ("database", "databases", "banco", "base de datos", "banca dati", "base de données", "Datenbank"),
    ("data", "dados", "datos", "dati", "données", "Daten"),
    ("table", "tabela", "tabla", "tabella", "tableau", "Tabelle"),
    ("index", "indexes", "índice", "indice", "índice", "indice", "Index"),
    ("migration", "migração", "migracao", "migración", "migrazione", "Migration"),
    ("test", "teste", "testes", "prueba", "test", "Test"),
    ("rule", "regra", "regras", "regla", "regola", "règle", "Regel"),
    ("memory", "memória", "memoria", "memoria", "mémoire", "Speicher"),
    ("search", "busca", "búsqueda", "ricerca", "recherche", "Suche"),
    ("permission", "permissão", "permissao", "permiso", "permesso", "autorisation", "Berechtigung"),
    ("user", "usuário", "usuario", "usuario", "utente", "utilisateur", "Benutzer"),
    ("command", "comando", "comando", "comando", "commande", "Befehl"),
    ("shortcut", "atalho", "atajo", "scorciatoia", "raccourci"),
    ("workflow", "fluxo", "flujo", "flusso", "flux", "Arbeitsablauf"),
    ("decision", "decisão", "decisao", "decisión", "decisione", "décision", "Entscheidung"),
    ("lesson", "lição", "licao", "lección", "lezione", "leçon"),
    ("tool", "ferramenta", "herramienta", "strumento", "outil", "Werkzeug"),
    ("project", "projeto", "proyecto", "progetto", "projet", "Projekt"),
    ("context", "contexto", "contexto", "contesto", "contexte", "Kontext"),
    ("session", "sessão", "sessao", "sesión", "sessione", "session", "Sitzung"),
    ("repository", "repositório", "repositorio", "repositorio", "dépôt", "Repository"),
    ("process", "processo", "proceso", "processo", "processus", "Prozess"),
    ("battery", "bateria", "batería", "batteria", "batterie", "Batterie"),
]


def parse_synonyms_md(text: str) -> list:
    """Parse a synonyms.md overlay: `term, alt: syn1, syn2` per line.

    Blank lines and `#` comments are ignored; malformed lines are skipped
    (fail-open). Returns a list of term groups.
    """
    groups = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        left, _, right = stripped.partition(":")
        terms = [part.strip() for part in re.split(r"[,;]", left + "," + right)]
        terms = [term for term in terms if term]
        if len(terms) >= 2:
            groups.append(tuple(dict.fromkeys(terms)))
    return groups


def build_synonym_map(extra_groups=()) -> dict:
    """Build folded term -> sorted synonyms from seed plus overlay groups."""
    members: dict = {}
    for group in list(SYNONYM_SEED) + list(extra_groups or ()):
        folded = [fold_accents(term) for term in group]
        folded = [term for term in folded if term]
        for term in folded:
            members.setdefault(term, set()).update(other for other in folded if other != term)
    return {term: sorted(syns) for term, syns in members.items()}


# Conservative endings stripped ONLY to build FTS prefix queries (never to
# rewrite content). Remainder must keep >= 5 chars; otherwise the full token
# is used. This avoids como/com-style conflations of suffix stripping.
_PREFIX_ENDINGS = (
    "mente", "ções", "coes", "ção", "cao", "dade", "agem",
    "ando", "endo", "indo", "ado", "ido", "ar", "er", "ir", "es", "s", "ns",
)


def _prefix_for(token: str) -> str:
    lowered = (token or "").casefold()
    for ending in _PREFIX_ENDINGS:
        if len(lowered) > len(ending) + 4 and lowered.endswith(ending):
            return lowered[: -len(ending)]
    return lowered


def expand_query_terms(tokens: list, synonym_map: dict) -> list:
    """Build tier-2 FTS terms: bare prefixes plus quoted synonyms/phrases.

    Prefixes catch same-language morphology (reembolso/reembolsos), the
    synonym map bridges languages (reembolso/refund), and multi-word keys
    (base de datos) match consecutive query tokens as phrases.
    Deterministic; reserved FTS operators are skipped in bare form.
    """
    terms: list = []
    seen: set = set()

    def push(term: str) -> None:
        if term not in seen:
            seen.add(term)
            terms.append(term)

    sequence = [token for token in tokens or []]
    for token in sequence:
        folded = fold_accents(token)
        for candidate in (fold_accents(_prefix_for(token)), fold_accents(_prefix_for(folded))):
            if not candidate or candidate.upper() in _FTS_RESERVED:
                continue
            push(f"{candidate}*")
        for synonym in (synonym_map or {}).get(folded, ()):
            push(f'"{synonym.replace(chr(34), chr(34) * 2)}"')
    # Multi-word synonym keys ("base de datos") over consecutive tokens.
    for width in (3, 2):
        for start in range(len(sequence) - width + 1):
            phrase = fold_accents(" ".join(sequence[start:start + width]))
            for synonym in (synonym_map or {}).get(phrase, ()):
                push(f'"{synonym.replace(chr(34), chr(34) * 2)}"')
    return terms[:48]


# Markers that flip meaning. The guard trips only on ASYMMETRY (one side has
# a marker, the other has none), so symmetric paraphrases such as
# "must not ..." vs "must never ..." still merge. Tripping never deletes or
# merges anything: it only routes the pair to the LLM reconcile path.
_NEGATION_MARKERS = frozenset({
    "não", "nao", "nunca", "jamais", "not", "never", "don", "sem",
})
_CONTRAST_MARKERS = frozenset({
    "instead", "vez", "mas", "but",
})
_NUMBER_PATTERN = re.compile(r"\d+")

# Minimal antonym seed list (exact-token, swapped-presence check). A miss only
# costs one bounded LLM call; the list stays small on purpose.
_ANTONYM_PAIRS = (
    ("allow", "forbid"),
    ("enable", "disable"),
    ("permit", "deny"),
    ("accept", "reject"),
    ("on", "off"),
    ("true", "false"),
    ("before", "after"),
    ("add", "remove"),
    ("start", "stop"),
)
# Timeframe words: "day" vs "month" flips meaning while keeping Jaccard high.
# Any asymmetric timeframe vocabulary across the two sides trips the guard.
_TIMEFRAME_WORDS = frozenset({
    "day", "days", "daily", "week", "weeks", "weekly",
    "month", "months", "monthly", "year", "years", "yearly",
    "hour", "hours", "hourly", "minute", "minutes", "second", "seconds",
})


def _raw_numbers(text: str) -> set:
    """Digit runs from the raw text (tokenizer drops short numbers)."""
    return set(_NUMBER_PATTERN.findall(text or ""))


def _content_tokens(text: str) -> set:
    """Raw casefolded tokens (no length/stopword filter) for guard checks."""
    return set(_TOKEN_PATTERN.findall((text or "").casefold()))


def contradiction_guard_tripped(a: str, b: str) -> bool:
    """Return True when a near-duplicate pair may be a contradiction.

    Two cheap deterministic checks, both fail-safe (a false trip only costs
    one bounded LLM call; a missed trip keeps today's behavior):
    1. Asymmetric negation/contrast markers: "Use X" vs "Don't use X".
       Swapped antonyms ("allow" vs "forbid") count as asymmetric too.
    2. Divergent raw numbers: "24 hours" vs "48 hours" (the tokenizer drops
       short numbers, so these look identical to Jaccard). Any difference
       trips when both sides carry numbers ("3.12" vs "3.13" share "3").
    """
    tokens_a = _content_tokens(a)
    tokens_b = _content_tokens(b)
    for markers in (_NEGATION_MARKERS, _CONTRAST_MARKERS):
        if bool(tokens_a & markers) != bool(tokens_b & markers):
            return True
    for word_a, word_b in _ANTONYM_PAIRS:
        a_has_a, a_has_b = word_a in tokens_a, word_b in tokens_a
        b_has_a, b_has_b = word_a in tokens_b, word_b in tokens_b
        if (a_has_a and not a_has_b and b_has_b and not b_has_a) or (
            b_has_a and not b_has_b and a_has_b and not a_has_a
        ):
            return True
    numbers_a = _raw_numbers(a)
    numbers_b = _raw_numbers(b)
    if numbers_a and numbers_b and numbers_a != numbers_b:
        return True
    timeframe_a = tokens_a & _TIMEFRAME_WORDS
    timeframe_b = tokens_b & _TIMEFRAME_WORDS
    if timeframe_a - timeframe_b and timeframe_b - timeframe_a:
        return True
    return False
