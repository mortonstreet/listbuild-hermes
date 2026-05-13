from __future__ import annotations

import re


BANNED_SINGLE_WORDS = {
    "get",
    "bank",
    "credit",
    "access",
    "open",
    "compare",
    "problem",
    "now",
    "billing",
    "deal",
    "finance",
    "financial",
    "claims",
    "insurance",
    "mortgage",
    "soon",
    "new",
    "performance",
    "freedom",
    "home",
    "sales",
    "medical",
    "urgent",
    "life",
    "marketing",
    "investment",
    "diagnostics",
    "friend",
    "cash",
    "invoice",
    "extra",
    "purchase",
}

BANNED_SHORT_PHRASES = {
    "off chance",
    "one time",
    "all good",
    "following up here",
    "last note from me here",
    "great fit",
    "bumping this once",
    "just following up once",
    "circle back",
    "one more quick follow-up",
    "keep this open",
    "compare notes",
    "compare notes live",
    "appreciate the reply",
}

HIGH_RISK_PROMO_PHRASES = {
    "$$$",
    "100% guaranteed",
    "100% free",
    "100% satisfied",
    "access now",
    "act fast",
    "act immediately",
    "act now",
    "action required",
    "amazing",
    "bonus",
    "buy now",
    "buy today",
    "call now",
    "claim now",
    "click",
    "discount",
    "don't delete",
    "earn",
    "exclusive deal",
    "expires today",
    "fantastic",
    "final call",
    "for free",
    "free access",
    "free consultation",
    "free gift",
    "free membership",
    "free money",
    "free quote",
    "free trial",
    "full refund",
    "get it now",
    "get started now",
    "giveaway",
    "great news",
    "guaranteed results",
    "hurry up",
    "important information",
    "immediately",
    "increase revenue",
    "increase sales",
    "limited time",
    "lowest price",
    "make money",
    "money-back guarantee",
    "must read",
    "no catch",
    "no cost",
    "no obligation",
    "no strings attached",
    "once in a lifetime",
    "only $",
    "order now",
    "order today",
    "please read",
    "profits",
    "quote",
    "risk-free",
    "save $",
    "save up to",
    "special invitation",
    "special offer",
    "special promotion",
    "supplies are limited",
    "take action now",
    "the best",
    "this won't last",
    "today",
    "trial",
    "unbelievable",
    "unlimited",
    "urgent",
    "what are you waiting for?",
    "while supplies last",
    "why pay more?",
    "winner announced",
    "wonderful",
}

PHISHING_STYLE_PHRASES = {
    "access your account",
    "account update",
    "activate now",
    "change password",
    "click to verify",
    "confirm your details",
    "confidential information",
    "data breach",
    "download now",
    "final notice",
    "important update",
    "immediate action required",
    "install now",
    "last warning",
    "log in now",
    "new login detected",
    "password reset",
    "payment details needed",
    "phishing alert",
    "security breach",
    "security update",
    "update account",
    "verify identity",
    "warning message",
}

SILENCE_CLOSEOUT_PATTERNS = {
    "i will take silence as a no",
    "read no reply as a pass",
    "assume no response means no interest",
    "if i don't hear back, i will leave it there",
    "if i don't hear back, i will let this one go",
    "if i don't hear back, i will leave you alone",
    "i am happy to stay out of the way",
}

COMPANY_NAME_REPLACEMENTS = {
    "accesshealth": "AH",
}

STATE_STYLE_COMPRESSIONS = {
    ("new", "york"): "NY",
    ("new", "jersey"): "NJ",
    ("new", "mexico"): "NM",
    ("new", "hampshire"): "NH",
}

HYPHEN_SUFFIX_HINTS = {
    "clinic",
    "center",
    "health",
    "care",
    "network",
    "alliance",
}

ACRONYM_SUFFIX_WORDS = {
    "affiliates",
    "communications",
    "network",
    "group",
    "cooperative",
    "coalition",
}

HEALTHCARE_SUFFIX_PATTERNS = [
    ("community", "health", "services", "center"),
    ("community", "health", "services"),
    ("community", "health", "center"),
    ("health", "services", "center"),
    ("health", "services"),
    ("health", "centers"),
    ("health", "center"),
]


def _initialism(words: list[str]) -> str:
    letters: list[str] = []
    for word in words:
        cleaned = _alpha_word(word)
        if not cleaned:
            continue
        letters.append(word[0].upper())
    return "".join(letters)


def _shorten_company_display(name: str) -> str:
    normalized = _normalize_text(name)
    words = normalized.split()
    lowered_words = tuple(_alpha_word(word) for word in words)

    if " - " in normalized:
        _, right = normalized.rsplit(" - ", 1)
        right_words = right.split()
        if 1 <= len(right_words) <= 3 and _alpha_word(right_words[-1]) in HYPHEN_SUFFIX_HINTS:
            return right

    for pattern in HEALTHCARE_SUFFIX_PATTERNS:
        if len(lowered_words) > len(pattern) and lowered_words[-len(pattern) :] == pattern:
            stripped_words = words[: -len(pattern)]
            stripped = " ".join(stripped_words).strip()
            if stripped:
                return stripped

    if len(normalized) <= 32 and len(normalized.split()) <= 4:
        return normalized

    if len(words) >= 4 and _alpha_word(words[-1]) in ACRONYM_SUFFIX_WORDS:
        acronym = _initialism(words[:-1])
        if len(acronym) >= 2:
            return f"{acronym} {words[-1]}"

    return normalized


def _normalize_text(text: str) -> str:
    return " ".join(text.replace("\u2013", "-").replace("\u2014", "-").split()).strip()


def _lower_text(text: str) -> str:
    return _normalize_text(text).casefold()


def _alpha_word(word: str) -> str:
    return re.sub(r"[^a-z0-9]", "", word.casefold())


def _contains_banned_phrase(text: str, phrase: str) -> bool:
    escaped = re.escape(phrase)
    pattern = escaped.replace(r"\ ", r"\s+")
    return re.search(rf"(^|[^a-z0-9]){pattern}($|[^a-z0-9])", text) is not None


def extract_spintax_variants(text: str) -> list[str]:
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}") and stripped.count("{") == 1 and stripped.count("}") == 1:
        inner = stripped[1:-1]
        return [_normalize_text(part) for part in inner.split("|") if _normalize_text(part)]
    return [_normalize_text(stripped)]


def find_spam_violations(text: str) -> list[str]:
    violations: list[str] = []
    normalized = _normalize_text(text)
    lowered = normalized.casefold()

    if "\u2013" in text or "\u2014" in text:
        violations.append("em dash")
    if "!!" in text:
        violations.append("multiple exclamation marks")
    if re.search(r"\b(hi|hello|hey)\s+\{\{first_name\}\}", lowered):
        violations.append("greeting prefix")

    words = re.findall(r"[a-z0-9']+", lowered)
    alpha_words = [_alpha_word(word) for word in words]
    alpha_word_set = set(alpha_words)
    for banned in BANNED_SINGLE_WORDS:
        if banned in alpha_word_set:
            violations.append(f"banned word:{banned}")

    for phrase_group in (
        BANNED_SHORT_PHRASES,
        HIGH_RISK_PROMO_PHRASES,
        PHISHING_STYLE_PHRASES,
        SILENCE_CLOSEOUT_PATTERNS,
    ):
        for phrase in phrase_group:
            if _contains_banned_phrase(lowered, phrase):
                violations.append(f"banned phrase:{phrase}")

    return sorted(set(violations))


def assert_spam_safe(text: str, *, field_name: str) -> None:
    violations = find_spam_violations(text)
    if violations:
        joined = ", ".join(violations)
        raise ValueError(f"{field_name} violates spam guardrails: {joined}")


def assert_safe_subject_spintax(text: str, *, field_name: str) -> None:
    variants = extract_spintax_variants(text)
    if not variants:
        raise ValueError(f"{field_name} has no subject variants")
    for variant in variants:
        words = re.findall(r"[a-z0-9']+", variant)
        if len(words) < 2 or len(words) > 4:
            raise ValueError(f"{field_name} subject must be 2 to 4 words: {variant}")
        if variant != variant.casefold():
            raise ValueError(f"{field_name} subject must be lowercase: {variant}")
        assert_spam_safe(variant, field_name=field_name)


def spam_safe_company_name(name: str) -> str:
    normalized = _normalize_text(name)
    if not normalized:
        return ""

    words = normalized.split()
    rebuilt_tokens: list[str] = []
    removed_leading_word: str | None = None
    index = 0
    while index < len(words):
        word = words[index]
        cleaned = _alpha_word(word)

        replacement = COMPANY_NAME_REPLACEMENTS.get(cleaned)
        if replacement:
            rebuilt_tokens.append(replacement)
            index += 1
            continue

        if index + 1 < len(words):
            pair = (cleaned, _alpha_word(words[index + 1]))
            compressed = STATE_STYLE_COMPRESSIONS.get(pair)
            if compressed:
                rebuilt_tokens.append(compressed)
                index += 2
                continue

        if cleaned in BANNED_SINGLE_WORDS:
            if removed_leading_word is None and not rebuilt_tokens:
                removed_leading_word = word
            index += 1
            continue

        rebuilt_tokens.append(word)
        index += 1

    if removed_leading_word and len(rebuilt_tokens) >= 2:
        first_kept = rebuilt_tokens[0]
        if first_kept and first_kept[0].isalnum() and removed_leading_word[0].isalnum():
            merged_initial = removed_leading_word[0].upper() + first_kept[0].upper()
            rebuilt_tokens = [merged_initial] + rebuilt_tokens[1:]

    rebuilt = " ".join(rebuilt_tokens).strip()
    rebuilt = re.sub(r"\s{2,}", " ", rebuilt).strip(" -")
    if not rebuilt:
        return normalized
    return _shorten_company_display(rebuilt)
