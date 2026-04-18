"""Narzedzia sesji (Faza 2 refaktoru agentic tool loop).

Narzedzia, ktore nie dotykaja vaulta, ale sterują zyciem sesji tool-use:

- ``submit_plan`` - terminator sesji. Model woła to gdy uzna, ze wszystkie
  propozycje zostaly juz zarejestrowane przez tools write. Ustawia
  ``ctx.finalized = True``, agent wychodzi z petli i przetwarza
  ``ctx.proposed_writes`` przez ``apply_pending``.
"""

from src.agent.tools.session.submit_plan import SubmitPlanTool

__all__ = ["SubmitPlanTool"]
