"""Normalize Shopify's uninstall reasons into canonical buckets.

Shopify serves the uninstall pick-list in the merchant's own admin language, so
the same reason arrives as "Testing multiple apps", "Testen mehrerer Apps", or
"現在アプリを使用していない" depending on who uninstalled. Grouping on the raw
string produces a long tail of one-off bars that says nothing.

Every string below is Shopify's own wording, collected from a live uninstall
feed, so the table is worth having whatever your app is. Unknown strings are
kept verbatim under UNCLASSIFIED and logged, so a new Shopify wording shows up
as a visible gap rather than silently disappearing into "Other".
"""

import logging
from datetime import date

logger = logging.getLogger(__name__)

UNCLASSIFIED = "Unclassified"

# Shopify turned the uninstall question from optional into required during 2026.
# Coverage before and after is wildly different, so any figure that spans the
# boundary is the average of two different questions and describes neither.
# There was no announcement to cite: read the boundary off your own feed (the
# last uninstall with an empty reason) and set REASON_MANDATORY_FROM to it.

# raw reason string -> (canonical bucket, language of the string)
CANONICAL: dict[str, tuple[str, str]] = {
    # Not using the app
    "Not using app now": ("Not using app now", "en"),
    "App wird derzeit nicht genutzt": ("Not using app now", "de"),
    "現在アプリを使用していない": ("Not using app now", "ja"),
    "Bruger ikke appen i øjeblikket": ("Not using app now", "da"),
    "Não estou usando o app no momento": ("Not using app now", "pt"),
    # Broken / incompatible. Shopify has shipped two wordings for this; they
    # mean the same thing and are merged so the bar isn't split in half.
    "Not working properly with store": ("Not working with store", "en"),
    "Not working or compatible with store": ("Not working with store", "en"),
    "Funktioniert nicht richtig mit dem Shop": ("Not working with store", "de"),
    # Comparison shopping
    "Testing multiple apps": ("Testing multiple apps", "en"),
    "Testen mehrerer Apps": ("Testing multiple apps", "de"),
    # Free-text escape hatch
    "Other (please specify)": ("Other", "en"),
    "Outro (especifique)": ("Other", "pt"),
    # Feature gaps. "Limited or missing features" (we don't have it) and "Not
    # satisfied with app features" (we have it, it's not good enough) are
    # different product signals, so they stay separate.
    "Limited or missing features": ("Limited or missing features", "en"),
    "Begrænsede eller manglende funktioner": ("Limited or missing features", "da"),
    "Not satisfied with app features": ("Not satisfied with features", "en"),
    "アプリの機能に満足できなかった": ("Not satisfied with features", "ja"),
    # Long tail
    "Store is closing or pausing": ("Store closing or pausing", "en"),
    "Too expensive": ("Too expensive", "en"),
    "Hard to set up or use": ("Hard to set up or use", "en"),
    "Not satisfied with support": ("Not satisfied with support", "en"),
}


def split_reasons(raw: str | None) -> list[str]:
    """Shopify sends a comma-separated list; merchants can pick more than one."""
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def classify(reason: str) -> tuple[str, str | None]:
    """Return (canonical bucket, language) for one raw reason string."""
    known = CANONICAL.get(reason)
    if known is None:
        logger.info("unmapped uninstall reason %r -- add it to CANONICAL", reason)
        return UNCLASSIFIED, None
    return known


def bucket_counts(raw_reasons) -> dict[str, int]:
    """Count canonical buckets across many raw reason strings.

    A multi-reason uninstall contributes to each bucket it names, so the counts
    sum to more than the number of uninstalls. Callers must label the chart with
    the number of uninstalls, not the bucket total.
    """
    counts: dict[str, int] = {}
    for raw in raw_reasons:
        for reason in split_reasons(raw):
            bucket, _ = classify(reason)
            counts[bucket] = counts.get(bucket, 0) + 1
    return counts


def language_counts(raw_reasons) -> dict[str, int]:
    """Count merchants by the language Shopify served their pick-list in.

    This is the only per-merchant language signal available anywhere: the
    Partner API's Shop type has no locale field, and neither does the CSV
    export. It covers churned shops that gave a reason, and nothing else.
    """
    counts: dict[str, int] = {}
    for raw in raw_reasons:
        langs = {lang for r in split_reasons(raw) if (lang := classify(r)[1])}
        for lang in langs:
            counts[lang] = counts.get(lang, 0) + 1
    return counts
