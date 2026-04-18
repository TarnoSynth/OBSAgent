"""Narzedzia write na vaulcie (Fazy 2-3 refaktoru agentic tool loop).

**Zasada:** narzedzia write NIE zapisuja do vaulta bezposrednio.
Rejestruja ``ProposedWrite`` w ``ToolExecutionContext.proposed_writes``,
a po wyjsciu z petli agent wykonuje je standardowym torem
``apply_pending`` -> preview -> ``finalize_pending`` / ``rollback_pending``.

Dzieki temu zachowane jest:

- diff-view w Obsidianie (czerwone ``[!failure]+`` + zielone ``[!tip]+``)
- user gating (``[T/n]``) zanim cokolwiek trafi do Gita
- spojnosc z istniejacym flow sprzed refactoru (tylko kanal komunikacji
  zmienil sie z ``submit_plan(actions=[...])`` na iteracyjne tool cally)

**Dwie warstwy narzedzi (Fazy 2 i 3):**

- **Faza 2 — caly plik:** ``CreateNoteTool`` / ``UpdateNoteTool`` /
  ``AppendToNoteTool``. Model produkuje pelny tekst notatki (wraz z
  frontmatterem) i rejestruje akcje ``create``/``update``/``append``.
- **Faza 3 — chirurgicznie:** ``AppendSectionTool`` / ``ReplaceSectionTool``
  / ``AddTableRowTool`` / ``AddMocLinkTool`` / ``UpdateFrontmatterTool``
  / ``AddRelatedLinkTool``. Modyfikuja pojedynczy fragment istniejacego
  pliku (sekcja / wiersz tabeli / pole YAML). Wewnetrznie rejestruja
  akcje ``update`` z pelnym nowym contentem — ale model nie musi jej
  budowac recznie.

**Koalescencja (Faza 3):**

Gdy model wola kilka granulowanych narzedzi pod rzad na ten sam plik
(np. ``update_frontmatter("updated", "2026-04-18")`` + ``append_section(...)``),
helper ``register_granular_update`` laczy je w jedna akcje ``update`` — zamiast
trzech update'ow nakladajacych sie na siebie. Detal: ``_granular.py``.

Eksporty:

- Faza 2 (pelny plik):
  - ``CreateNoteTool``      - tworzy nowa notatke
  - ``UpdateNoteTool``      - nadpisuje istniejaca
  - ``AppendToNoteTool``    - dopisuje na koncu
- Faza 3 (granulowane):
  - ``AppendSectionTool``      - dodaje nowa sekcje ## heading
  - ``ReplaceSectionTool``     - podmienia body istniejacej sekcji
  - ``AddTableRowTool``        - dopisuje wiersz do tabeli
  - ``AddMocLinkTool``         - dopisuje bullet ``- [[wikilink]]`` do MOC
  - ``UpdateFrontmatterTool``  - ustawia pole YAML frontmattera
  - ``AddRelatedLinkTool``     - dopisuje wikilink do related[] (idempotentnie)
"""

from src.agent.tools.vault_write.add_moc_link import AddMocLinkTool
from src.agent.tools.vault_write.add_related_link import AddRelatedLinkTool
from src.agent.tools.vault_write.add_table_row import AddTableRowTool
from src.agent.tools.vault_write.append_section import AppendSectionTool
from src.agent.tools.vault_write.append_to_note import AppendToNoteTool
from src.agent.tools.vault_write.create_changelog_entry import CreateChangelogEntryTool
from src.agent.tools.vault_write.create_concept import CreateConceptTool
from src.agent.tools.vault_write.create_decision import CreateDecisionTool
from src.agent.tools.vault_write.create_hub import CreateHubTool
from src.agent.tools.vault_write.create_module import CreateModuleTool
from src.agent.tools.vault_write.create_note import CreateNoteTool
from src.agent.tools.vault_write.create_technology import CreateTechnologyTool
from src.agent.tools.vault_write.register_pending_concept import (
    PENDING_CONCEPTS_PATH,
    PENDING_CONCEPTS_SECTION,
    RegisterPendingConceptTool,
)
from src.agent.tools.vault_write.replace_section import ReplaceSectionTool
from src.agent.tools.vault_write.update_frontmatter import UpdateFrontmatterTool
from src.agent.tools.vault_write.update_note import UpdateNoteTool

__all__ = [
    "PENDING_CONCEPTS_PATH",
    "PENDING_CONCEPTS_SECTION",
    "AddMocLinkTool",
    "AddRelatedLinkTool",
    "AddTableRowTool",
    "AppendSectionTool",
    "AppendToNoteTool",
    "CreateChangelogEntryTool",
    "CreateConceptTool",
    "CreateDecisionTool",
    "CreateHubTool",
    "CreateModuleTool",
    "CreateNoteTool",
    "CreateTechnologyTool",
    "RegisterPendingConceptTool",
    "ReplaceSectionTool",
    "UpdateFrontmatterTool",
    "UpdateNoteTool",
]
