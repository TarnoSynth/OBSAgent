"""Narzedzia dedykowane MOCAgentowi - budowa i utrzymanie MOC-ow.

Ta warstwa istnieje osobno od ``vault_read`` / ``vault_write`` bo MOCAgent
operuje na innym poziomie abstrakcji: interesuja go **struktury nawigacyjne**
(huby, grupy modulow, sekcje MOC, orphan wikilinks), nie pojedyncze pliki.

Typowy workflow MOCAgenta:

1. ``moc_audit(moc_path)`` -> raport luk (bez tego LLM nie wie co zrobic).
2. Na podstawie raportu: ``create_hub`` / ``create_technology`` / ``create_concept``
   + ``add_moc_link`` + ``add_related_link`` (narzedzia z ``vault_write``).
3. ``moc_set_intro(moc_path, intro)`` - uzupelnienie blurbu MOC-u.
4. ``submit_plan`` (to samo submit_plan co zwykly agent - nie ma osobnego).

Narzedzia tutaj sa **read-only** (audit) albo edytuja wylacznie MOC-a w
spojny sposob (set_intro). Tworzenie nowych notatek (hub/technology/concept)
leci przez istniejace ``vault_write.create_*`` - nie dublujemy kodu.
"""

from __future__ import annotations

from src.agent.tools.moc_ops.moc_audit import MocAuditTool
from src.agent.tools.moc_ops.moc_set_intro import MocSetIntroTool

__all__ = ["MocAuditTool", "MocSetIntroTool"]
