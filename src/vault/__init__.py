"""Publiczne API warstwy vault — semantyka Obsidiana.

Warstwa jest **agnostyczna wzgledem Gita**: dziala na dowolnym folderze z plikami
``.md`` (nawet jesli to nie jest repo). Historie zmian ("co sie zmienilo w vaulcie")
agent pobiera z warstwy git (``GitReader`` na sciezce vaulta), a potem laduje
aktualna tresc przez ``VaultManager.read_note``.

Decyzje ("dopisz link do MOC", "aktualizuj _index.md") nie nalezy do tej warstwy —
zostaw je agentowi (lub dedykowanej klasie ``MOCManager`` w przyszlej Fazie 4b).
"""

from src.vault.consistency import (
    KNOWN_TYPES,
    ConsistencyReport,
    TagInconsistency,
    analyze,
    is_known_type,
)
from src.vault.manager import VaultManager
from src.vault.models import VaultKnowledge, VaultNote
from src.vault.moc import (
    DEFAULT_INDEX_PATH,
    DEFAULT_MOC_PATTERN,
    LEGACY_MOC_PREFIX,
    MOC_SECTION_TITLES,
    BootstrapMocOutcome,
    IndexUpdateOutcome,
    MOCLinkOutcome,
    MOCManager,
    insert_into_moc_section,
    moc_contains_link,
    moc_section_for_type,
    render_bootstrap_moc,
)

__all__ = [
    "BootstrapMocOutcome",
    "ConsistencyReport",
    "DEFAULT_INDEX_PATH",
    "DEFAULT_MOC_PATTERN",
    "IndexUpdateOutcome",
    "KNOWN_TYPES",
    "LEGACY_MOC_PREFIX",
    "MOC_SECTION_TITLES",
    "MOCLinkOutcome",
    "MOCManager",
    "TagInconsistency",
    "VaultKnowledge",
    "VaultManager",
    "VaultNote",
    "analyze",
    "insert_into_moc_section",
    "is_known_type",
    "moc_contains_link",
    "moc_section_for_type",
    "render_bootstrap_moc",
]
