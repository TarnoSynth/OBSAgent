# Zadanie: audyt i uzupełnienie MOC-u vaulta

**Projekt:** {{project_name}}
**Vault:** `{{vault_path}}`
**Główny MOC:** `{{moc_path}}`
**Język:** {{language}}

---

## Kontekst

{{trigger_context}}

---

## Co masz zrobić

1. **Rozpocznij od `moc_audit`** — bez tego nie wiesz nic o stanie vaulta.
2. Na podstawie raportu zdecyduj, które luki są **naprawdę warte akcji**
   (patrz priorytety w prompcie systemowym).
3. Twórz huby/technologie/koncepty przez `create_hub` / `create_technology`
   / `create_concept`. Każda nowa notatka **musi mieć konkretną treść** —
   nie stwarzaj pustych szkieletów.
4. Dopinaj nowe notatki do odpowiednich sekcji MOC przez `add_moc_link`
   (`Huby`, `Technologie`, `Koncepty`, `Decyzje architektoniczne`).
5. Dolinkuj nowe notatki z powiązanych modułów przez `add_related_link`
   (zwłaszcza huby z "ich" modułami, technologie z modułami ich używającymi).
6. Na końcu wywołaj `moc_set_intro` z krótkim (2-4 akapity) wprowadzeniem
   do MOC: co to za vault, jak go czytać, gdzie szukać czego.
7. Zamknij sesję `submit_plan`.

Jeśli audyt pokaże, że wszystko jest w porządku (brak sugestii hubów,
zero orphanów, sekcje uzupełnione, intro istnieje) — **zwróć
`submit_plan(summary="MOC audyt czysty — brak zmian.")` w pierwszej
iteracji** i kończymy.
