"""Narzedzia eksploracyjne (read-only) na vaulcie — Faza 4 refaktoru agentic tool loop.

**Cel warstwy:** odchudzic prompt. Zamiast wstrzykiwac do user-prompta caly
``VaultKnowledge`` (rosnie liniowo z vaultem), agent daje modelowi **tools
do eksploracji on-demand**. Do promptu trafia tylko mapa najwyzszego poziomu
(MOC-i + huby + liczniki per typ), a model sam wola:

- ``list_notes(type=..., tag=..., parent=..., path_prefix=...)``  — lista
  notatek przefiltrowana
- ``read_note(path, sections=?)``                                 — pelna
  tresc notatki (albo wskazane sekcje)
- ``find_related(topic, limit=?)``                                — fuzzy
  match po stem/tag/heading/wikilinki
- ``list_pending_concepts()``                                     — orphan
  wikilinki (placeholdery do wypelnienia)
- ``get_commit_context()``                                        — metadane
  commita (SHA, message, author, pliki)

**Cechy wspolne:**

- Zadne narzedzie nie zapisuje niczego — wszystkie sa idempotentne read-only.
- Kazde korzysta z ``ctx.ensure_vault_knowledge()`` (cache per sesja), wiec
  nawet 20 wywolan ``list_notes`` odpala ``scan_all`` tylko raz.
- ``get_commit_context`` NIE rusza vaulta — korzysta z ``ctx.commit_info``
  + ``ctx.git_reader`` (jesli dostepne).
- Odpowiedzi sa **strukturowane** (``structured=...``) i maja skrocony
  tekst dla modelu w ``content`` — model widzi zwarty JSON, agent ma pelne
  dane do logow.

**Zasada uzywania z promptu (patrz ``system_pl.md``):**

Model powinien zaczynac kazda sesja od zbadania terenu (1-3 wywolania
``list_notes`` / ``find_related`` / ``read_note``) **zanim** zaproponuje
zapis. Duplikacji i osieroconym wikilinkom zapobiega poznanie stanu vaulta,
nie tylko tresc promptu.
"""

from src.agent.tools.vault_read.find_related import FindRelatedTool
from src.agent.tools.vault_read.get_commit_context import GetCommitContextTool
from src.agent.tools.vault_read.list_notes import ListNotesTool
from src.agent.tools.vault_read.list_pending_concepts import ListPendingConceptsTool
from src.agent.tools.vault_read.read_note import ReadNoteTool

__all__ = [
    "FindRelatedTool",
    "GetCommitContextTool",
    "ListNotesTool",
    "ListPendingConceptsTool",
    "ReadNoteTool",
]
