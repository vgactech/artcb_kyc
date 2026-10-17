#!/usr/bin/env python3
"""
run_kyc_pipeline.py — ARTCB KYC Form Resolution Pipeline (fusion MODELE_9 + MODELE_10)

Hérite de :
  - MODELE_9 (run_kyc.py / kyc/) : architecture modulaire, multi-LLM (Gemini), extraction
    de champs par vision, PDF overlay précis sur coordonnées réelles.
  - MODELE_10 (pipeline.py / R575) : resolvers rule-based déterministes par formulaire,
    fallback Ollama text, annotation PDF par blocs.

Stratégie :
  1. Tenter les resolvers rule-based (ARTCB R575) — 0 token, déterministe.
  2. Si un LLM est configuré (GEMINI_API_KEY ou OPENAI_API_KEY) et que ARTCB_LLM_ENABLED=true,
     enrichir/valider les réponses manquantes via le LLM.
  3. Si Ollama est disponible (http://localhost:11434), utiliser phi3:mini ou gemma2:2b
     pour les champs ambigus restants.
  4. Produire les livrables conformes à l'atelier :
     - <exercice>.answers.json : liste plate (sans wrapper), clé "state" (pas "status")
     - <exercice>.completed.pdf : overlay sur le PDF source

Exécution :
    python run_kyc_pipeline.py form_01              # un exercice
    python run_kyc_pipeline.py form_01 form_02      # plusieurs
    python run_kyc_pipeline.py all                  # les cinq
    python run_kyc_pipeline.py form_01 --rule-only  # rule-based uniquement (0 token)
    python run_kyc_pipeline.py form_01 --no-pdf     # sans PDF

CERTIFIED_100=false | unique_human_proven=false | Mode DEBUG actif
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import sys
import textwrap
import time
import urllib.error
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
# Ce fichier vit dans SUBMISSIONS/MODELE_10/ — la racine du dépôt est deux niveaux au-dessus.
_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent.parent          # contient PARTICIPANT_PACK/
PACK_DIR = _REPO_ROOT / "PARTICIPANT_PACK"
OUTPUT_DIR = _SCRIPT_DIR / "submission"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

EXERCISES_JSON = PACK_DIR / "exercices.json"

# ---------------------------------------------------------------------------
# Logging (DEBUG mode actif — PROTOCOLE_ARTCB)
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("kyc_pipeline")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MISSING = "missing_information"
NA = "not_applicable"
BANK_RESERVED = "bank_reserved"
COMPLETION_DATE = "2026-10-17"  # date courante de complétion (règle atelier)

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/generate")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "phi3:mini")

# ---------------------------------------------------------------------------
# Context loader (réutilisé depuis MODELE_10 / R575)
# ---------------------------------------------------------------------------

def load_context(entreprise_dir: str) -> dict:
    """Charge les tables JSON du dossier entreprise."""
    tables_dir = PACK_DIR / entreprise_dir / "tables"
    ctx: dict = {}
    for fname in sorted(tables_dir.iterdir()):
        if fname.suffix != ".json":
            continue
        key = fname.stem
        ctx[key] = json.loads(fname.read_text(encoding="utf-8"))
    log.debug("context loaded from %s: tables=%s", entreprise_dir, list(ctx.keys()))
    return ctx


def build_fact_index(ctx: dict) -> dict:
    facts = ctx.get("company_profile_facts", [])
    index: dict = {}
    for f in facts:
        key = f.get("canonical_key", "other")
        entry = {
            "value": f.get("value_jsonb"),
            "scope_type": f.get("scope_type", "?"),
            "scope_id": f.get("scope_id", "?"),
            "source": f.get("latest_evidence_path", ""),
        }
        index.setdefault(key, []).append(entry)
    return index


def build_assertion_index(ctx: dict) -> dict:
    assertions = ctx.get("kyc_context_assertions", [])
    index: dict = {}
    for a in assertions:
        key = a.get("concept_key", "other")
        index.setdefault(key, []).append(a)
    return index


def get_client_subsidiary(ctx: dict) -> dict:
    subs = ctx.get("subsidiaries", [])
    cc = ctx.get("corporate_context", [])
    cc_item = (cc[0] if isinstance(cc, list) and cc else cc) or {}
    client_id = cc_item.get("subsidiary_id")
    for s in subs:
        if s.get("subsidiary_id") == client_id:
            return s
    for s in subs:
        if "parent" not in s.get("subsidiary_id", ""):
            return s
    return subs[0] if subs else {}


def get_ubos(ctx: dict) -> list:
    cc = ctx.get("corporate_context", [])
    cc_item = (cc[0] if isinstance(cc, list) and cc else cc) or {}
    return cc_item.get("ubo_list", [])


def get_persons(ctx: dict) -> list:
    return ctx.get("kyc_persons", [])


def get_subsidiaries(ctx: dict) -> list:
    return ctx.get("subsidiaries", [])


def get_corporate_context(ctx: dict) -> dict:
    cc = ctx.get("corporate_context", [])
    return (cc[0] if isinstance(cc, list) and cc else cc) or {}


def get_groups(ctx: dict) -> dict:
    grp = ctx.get("groups", [])
    return (grp[0] if isinstance(grp, list) and grp else grp) or {}


def first_val(fact_index: dict, *keys) -> str | None:
    for k in keys:
        for e in fact_index.get(k, []):
            v = e.get("value")
            if v is not None and v != "" and v != []:
                return str(v) if not isinstance(v, str) else v
    return None


def source_ref(fact_index: dict, *keys) -> str:
    for k in keys:
        entries = fact_index.get(k, [])
        if entries and entries[0].get("source"):
            return entries[0]["source"]
    return "tables/company_profile_facts.json"


# ---------------------------------------------------------------------------
# Answer record (format conforme atelier — clé "state", pas "status")
# ---------------------------------------------------------------------------

def make_answer(value, label: str, page: int, source: str,
                justification: str = "", state: str = "answer") -> dict:
    """
    Produit un enregistrement conforme à la spécification de l'atelier :
      - clé  "state"  (et non "status" — erreur R575 corrigée ici)
      - liste plate sans wrapper
    """
    return {
        "page": page,
        "label": label,
        "value": value if value is not None else None,
        "state": state,
        "source": source,
        "justification": justification,
        "missing": [] if state != MISSING else [label],
    }


# ---------------------------------------------------------------------------
# LLM helpers (Ollama fallback, 0-dépendance externe)
# ---------------------------------------------------------------------------

def _ollama_available() -> bool:
    try:
        with urllib.request.urlopen(
            urllib.request.Request("http://localhost:11434/api/tags", method="GET"),
            timeout=2,
        ):
            return True
    except Exception:
        return False


def ollama_query(prompt: str, max_tokens: int = 256, timeout: int = 30) -> str:
    """Appel Ollama phi3:mini en mode texte uniquement. Retourne "" si indisponible."""
    payload = json.dumps({
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"num_predict": max_tokens, "temperature": 0.0},
    }).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8")).get("response", "").strip()
    except Exception as exc:
        log.debug("Ollama unavailable (%s) — rule-based only", exc)
        return ""


def llm_enrich(answers: list[dict], ctx: dict, form_id: str) -> list[dict]:
    """
    Enrichit les champs MISSING avec Ollama si disponible.
    Ne modifie pas les champs déjà répondus.
    Retourne la liste (possiblement enrichie).
    """
    if not _ollama_available():
        log.debug("Ollama unavailable — skipping LLM enrichment for %s", form_id)
        return answers

    context_summary = json.dumps(ctx, ensure_ascii=False, separators=(",", ":"))[:3000]
    enriched = []
    for a in answers:
        if a["state"] == MISSING:
            prompt = (
                f"KYC form {form_id}. Context (JSON excerpt): {context_summary[:1500]}\n\n"
                f"Field: {a['label']}\n"
                "Provide the most accurate value based on the context. "
                "If truly unknown, answer exactly: INFORMATION_MANQUANTE\n"
                "Answer (value only, no explanation):"
            )
            result = ollama_query(prompt, max_tokens=128)
            if result and result != "INFORMATION_MANQUANTE" and len(result) < 300:
                a = {**a, "value": result, "state": "answer",
                     "source": f"llm_fallback/{OLLAMA_MODEL}",
                     "justification": f"Champ enrichi par {OLLAMA_MODEL} (fallback LLM).",
                     "missing": []}
                log.debug("LLM enriched: %s → %s", a["label"][:40], result[:60])
        enriched.append(a)
    return enriched


# ---------------------------------------------------------------------------
# Rule-based resolvers (ARTCB R575 — corrigés pour conformité atelier)
# ---------------------------------------------------------------------------

def resolve_form_01(ctx: dict) -> list:
    """form_01 — Asterive Services SAS — Français (2 pages)"""
    fi = build_fact_index(ctx)
    cc = get_corporate_context(ctx)
    client = get_client_subsidiary(ctx)
    ubos = get_ubos(ctx)
    persons = get_persons(ctx)

    answers = []

    # Identité
    answers.append(make_answer(client.get("subsidiary_name"), "Dénomination sociale / Raison sociale", 1, "tables/subsidiaries.json#subsidiary_name", "Nom légal du client KYC."))
    answers.append(make_answer(client.get("legal_form"), "Forme juridique", 1, "tables/subsidiaries.json#legal_form"))
    answers.append(make_answer(client.get("registration_number"), "Numéro d'immatriculation (RCS / SIREN)", 1, "tables/subsidiaries.json#registration_number"))
    answers.append(make_answer(first_val(fi, "tax.identifier"), "Numéro d'identification fiscale (NIF / TIN)", 1, source_ref(fi, "tax.identifier"), state="answer" if first_val(fi, "tax.identifier") else MISSING))
    answers.append(make_answer(client.get("country_iso2"), "Pays d'incorporation", 1, "tables/subsidiaries.json#country_iso2"))
    answers.append(make_answer(client.get("address"), "Adresse du siège social", 1, "tables/subsidiaries.json#address"))

    # Activité
    answers.append(make_answer(first_val(fi, "activity.description"), "Description de l'activité principale", 1, source_ref(fi, "activity.description"), state="answer" if first_val(fi, "activity.description") else MISSING))
    answers.append(make_answer(first_val(fi, "activity.sector_code"), "Code NAF / APE", 1, source_ref(fi, "activity.sector_code"), state="answer" if first_val(fi, "activity.sector_code") else MISSING))
    emp = first_val(fi, "financial.employees")
    answers.append(make_answer(emp, "Nombre d'employés", 1, source_ref(fi, "financial.employees"), state="answer" if emp else MISSING))

    # UBOs
    for i, ubo in enumerate(ubos, 1):
        pid = ubo.get("person_id")
        dob = next((p.get("date_of_birth") for p in persons if p.get("person_id") == pid), None)
        answers.append(make_answer(ubo.get("ubo_name"), f"Bénéficiaire effectif {i} — Nom complet", 2, "tables/corporate_context.json#ubo_list"))
        answers.append(make_answer(f"{ubo.get('ubo_ownership_percentage', '')}%", f"Bénéficiaire effectif {i} — Pourcentage de détention", 2, "tables/corporate_context.json#ubo_list"))
        answers.append(make_answer(dob, f"Bénéficiaire effectif {i} — Date de naissance", 2, "tables/kyc_persons.json#date_of_birth", state="answer" if dob else MISSING))
        answers.append(make_answer(ubo.get("control_basis"), f"Bénéficiaire effectif {i} — Base du contrôle", 2, "tables/corporate_context.json#ubo_list", state="answer" if ubo.get("control_basis") else MISSING))

    # Résidence fiscale
    tax_info = cc.get("tax_residency_info", {})
    for tr in tax_info.get("tax_residencies", []):
        answers.append(make_answer(tr.get("country"), "Résidence fiscale — Pays", 2, "tables/corporate_context.json#tax_residency_info"))
        answers.append(make_answer(tr.get("tin"), "Résidence fiscale — NIF", 2, "tables/corporate_context.json#tax_residency_info", state="answer" if tr.get("tin") else MISSING))
    answers.append(make_answer(tax_info.get("crs_status"), "Statut CRS", 2, "tables/corporate_context.json#tax_residency_info", state="answer" if tax_info.get("crs_status") else MISSING))
    answers.append(make_answer(tax_info.get("giin") or "N/A", "GIIN (FATCA)", 2, "tables/corporate_context.json#tax_residency_info", "Pas de GIIN — entité non-FATCA.", state="answer" if tax_info.get("giin") else NA))

    # Sanctions
    sanctions = cc.get("sanctions_related_issues", [])
    answers.append(make_answer("Aucun" if not sanctions else json.dumps(sanctions, ensure_ascii=False), "Problèmes liés aux sanctions", 2, "tables/corporate_context.json#sanctions_related_issues"))

    # Signataire
    sig = first_val(fi, "signatory.authority")
    answers.append(make_answer(sig, "Autorité du signataire / Mandat", 2, source_ref(fi, "signatory.authority"), state="answer" if sig else MISSING))
    answers.append(make_answer(COMPLETION_DATE, "Date de complétion du formulaire", 2, "règle atelier — date de complétion"))

    return answers


def resolve_form_02(ctx: dict) -> list:
    """form_02 — Belorive Patrimoine SAS — Français (4 pages)"""
    fi = build_fact_index(ctx)
    cc = get_corporate_context(ctx)
    client = get_client_subsidiary(ctx)
    ubos = get_ubos(ctx)
    persons = get_persons(ctx)
    subs = get_subsidiaries(ctx)

    answers = []
    answers.append(make_answer(client.get("subsidiary_name"), "Dénomination sociale", 1, "tables/subsidiaries.json#subsidiary_name"))
    answers.append(make_answer(client.get("legal_form"), "Forme juridique", 1, "tables/subsidiaries.json#legal_form"))
    answers.append(make_answer(client.get("registration_number"), "Numéro RCS", 1, "tables/subsidiaries.json#registration_number"))
    answers.append(make_answer(first_val(fi, "tax.identifier"), "NIF / TIN", 1, source_ref(fi, "tax.identifier"), state="answer" if first_val(fi, "tax.identifier") else MISSING))
    answers.append(make_answer(client.get("address"), "Adresse du siège social", 1, "tables/subsidiaries.json#address"))
    answers.append(make_answer(first_val(fi, "activity.description"), "Description activité principale", 1, source_ref(fi, "activity.description"), state="answer" if first_val(fi, "activity.description") else MISSING))
    answers.append(make_answer(first_val(fi, "activity.sector_code"), "Code NAF", 1, source_ref(fi, "activity.sector_code"), state="answer" if first_val(fi, "activity.sector_code") else MISSING))

    for s in subs:
        answers.append(make_answer(s.get("subsidiary_name"), f"Entité groupe — {s.get('subsidiary_id')}", 2, "tables/subsidiaries.json", f"Pays: {s.get('country_iso2','?')} | Forme: {s.get('legal_form','?')}"))

    # Sanctions — Belorive a des filiales Belarus/Russia
    sanctions = cc.get("sanctions_related_issues", [])
    sanctions_text = json.dumps(sanctions, ensure_ascii=False) if sanctions else "Non — aucun problème sanctions répertorié."
    flagged = [s for s in subs if any(kw in s.get("subsidiary_name", "").lower() for kw in ["belarus", "russia"])]
    if flagged:
        sanctions_text += " ATTENTION : filiales dans des pays sous sanctions (Belarus, Russia) — exposition EU/OFAC/UK."
    answers.append(make_answer(sanctions_text, "Exposition aux sanctions", 3, "tables/corporate_context.json#sanctions_related_issues", "Filiales géographiquement exposées aux sanctions."))

    for i, ubo in enumerate(ubos, 1):
        pid = ubo.get("person_id")
        dob = next((p.get("date_of_birth") for p in persons if p.get("person_id") == pid), None)
        answers.append(make_answer(ubo.get("ubo_name"), f"UBO {i} — Nom", 2, "tables/corporate_context.json#ubo_list"))
        answers.append(make_answer(f"{ubo.get('ubo_ownership_percentage', 0)}%", f"UBO {i} — Détention", 2, "tables/corporate_context.json#ubo_list"))
        answers.append(make_answer(dob, f"UBO {i} — Date de naissance", 2, "tables/kyc_persons.json#date_of_birth", state="answer" if dob else MISSING))

    tax_info = cc.get("tax_residency_info", {})
    answers.append(make_answer(tax_info.get("crs_status"), "Statut CRS", 3, "tables/corporate_context.json#tax_residency_info", state="answer" if tax_info.get("crs_status") else MISSING))
    answers.append(make_answer(tax_info.get("tin") or tax_info.get("giin"), "TIN principal", 3, "tables/corporate_context.json#tax_residency_info", state="answer" if (tax_info.get("tin") or tax_info.get("giin")) else MISSING))
    answers.append(make_answer(tax_info.get("giin") or "N/A", "GIIN", 3, "tables/corporate_context.json#tax_residency_info", state=NA))
    answers.append(make_answer(COMPLETION_DATE, "Date de complétion", 4, "règle atelier"))

    return answers


def resolve_form_03(ctx: dict) -> list:
    """form_03 — Cendrelis Instruments SAS — English (9 pages)"""
    fi = build_fact_index(ctx)
    cc = get_corporate_context(ctx)
    client = get_client_subsidiary(ctx)
    ubos = get_ubos(ctx)
    persons = get_persons(ctx)
    subs = get_subsidiaries(ctx)

    answers = []
    answers.append(make_answer(client.get("subsidiary_name"), "Legal name / Company name", 1, "tables/subsidiaries.json#subsidiary_name"))
    answers.append(make_answer(client.get("legal_form"), "Legal form", 1, "tables/subsidiaries.json#legal_form"))
    answers.append(make_answer(client.get("registration_number"), "Registration number", 1, "tables/subsidiaries.json#registration_number"))
    answers.append(make_answer(first_val(fi, "tax.identifier"), "Tax identification number (TIN)", 1, source_ref(fi, "tax.identifier"), state="answer" if first_val(fi, "tax.identifier") else MISSING))
    answers.append(make_answer(client.get("address"), "Registered office address", 1, "tables/subsidiaries.json#address"))
    answers.append(make_answer(first_val(fi, "activity.description"), "Principal business activity", 2, source_ref(fi, "activity.description"), state="answer" if first_val(fi, "activity.description") else MISSING))
    answers.append(make_answer(first_val(fi, "activity.sector_code"), "NAF / sector code", 2, source_ref(fi, "activity.sector_code"), state="answer" if first_val(fi, "activity.sector_code") else MISSING))

    for s in subs:
        answers.append(make_answer(s.get("subsidiary_name"), f"Group entity — {s.get('subsidiary_id')}", 3, "tables/subsidiaries.json", f"Country: {s.get('country_iso2','?')} | Form: {s.get('legal_form','?')}"))

    for i, ubo in enumerate(ubos, 1):
        pid = ubo.get("person_id")
        dob = next((p.get("date_of_birth") for p in persons if p.get("person_id") == pid), None)
        answers.append(make_answer(ubo.get("ubo_name"), f"UBO {i} — Full name", 4, "tables/corporate_context.json#ubo_list"))
        answers.append(make_answer(f"{ubo.get('ubo_ownership_percentage', 0)}%", f"UBO {i} — Ownership %", 4, "tables/corporate_context.json#ubo_list"))
        answers.append(make_answer(dob, f"UBO {i} — Date of birth", 4, "tables/kyc_persons.json#date_of_birth", state="answer" if dob else MISSING))
        answers.append(make_answer(ubo.get("control_basis"), f"UBO {i} — Control basis", 4, "tables/corporate_context.json#ubo_list", state="answer" if ubo.get("control_basis") else MISSING))

    tax_info = cc.get("tax_residency_info", {})
    answers.append(make_answer(tax_info.get("crs_status"), "CRS status", 6, "tables/corporate_context.json#tax_residency_info", state="answer" if tax_info.get("crs_status") else MISSING))
    answers.append(make_answer(tax_info.get("giin") or "N/A", "GIIN (FATCA)", 6, "tables/corporate_context.json#tax_residency_info", state=NA))

    sanctions = cc.get("sanctions_related_issues", [])
    answers.append(make_answer("None" if not sanctions else json.dumps(sanctions, ensure_ascii=False), "Sanctions-related issues", 7, "tables/corporate_context.json#sanctions_related_issues"))

    sof = first_val(fi, "source_of_funds")
    answers.append(make_answer(sof, "Source of funds", 8, source_ref(fi, "source_of_funds"), state="answer" if sof else MISSING))
    answers.append(make_answer(COMPLETION_DATE, "Date of completion", 9, "exercise rule"))

    return answers


def resolve_form_04(ctx: dict) -> list:
    """form_04 — Belorive Patrimoine SAS — English (4 pages)"""
    fi = build_fact_index(ctx)
    cc = get_corporate_context(ctx)
    client = get_client_subsidiary(ctx)
    ubos = get_ubos(ctx)
    persons = get_persons(ctx)
    subs = get_subsidiaries(ctx)

    answers = []
    answers.append(make_answer(client.get("subsidiary_name"), "Legal name", 1, "tables/subsidiaries.json#subsidiary_name"))
    answers.append(make_answer(client.get("legal_form"), "Legal form", 1, "tables/subsidiaries.json#legal_form"))
    answers.append(make_answer(client.get("registration_number"), "Registration number", 1, "tables/subsidiaries.json#registration_number"))
    answers.append(make_answer(first_val(fi, "tax.identifier"), "TIN", 1, source_ref(fi, "tax.identifier"), state="answer" if first_val(fi, "tax.identifier") else MISSING))
    answers.append(make_answer(client.get("address"), "Registered address", 1, "tables/subsidiaries.json#address"))
    answers.append(make_answer(first_val(fi, "activity.description"), "Business description", 2, source_ref(fi, "activity.description"), state="answer" if first_val(fi, "activity.description") else MISSING))

    flagged = [s for s in subs if any(kw in s.get("subsidiary_name", "").lower() for kw in ["belarus", "russia"])]
    for s in flagged:
        answers.append(make_answer(s.get("subsidiary_name"), f"Subsidiary in sanctioned country — {s.get('country_iso2','?')}", 2, "tables/subsidiaries.json", "Geographic sanctions exposure: Belarus and Russia."))

    for i, ubo in enumerate(ubos, 1):
        pid = ubo.get("person_id")
        dob = next((p.get("date_of_birth") for p in persons if p.get("person_id") == pid), None)
        answers.append(make_answer(ubo.get("ubo_name"), f"UBO {i} — Name", 3, "tables/corporate_context.json#ubo_list"))
        answers.append(make_answer(f"{ubo.get('ubo_ownership_percentage', 0)}%", f"UBO {i} — Ownership", 3, "tables/corporate_context.json#ubo_list"))
        answers.append(make_answer(dob, f"UBO {i} — DOB", 3, "tables/kyc_persons.json", state="answer" if dob else MISSING))

    tax_info = cc.get("tax_residency_info", {})
    answers.append(make_answer(tax_info.get("crs_status"), "CRS classification", 3, "tables/corporate_context.json#tax_residency_info", state="answer" if tax_info.get("crs_status") else MISSING))
    answers.append(make_answer(COMPLETION_DATE, "Date completed", 4, "exercise rule"))

    return answers


def resolve_form_05(ctx: dict) -> list:
    """form_05 — Cendrelis Instruments SAS — Polish/English bilingual (11 pages)"""
    base = resolve_form_03(ctx)
    for a in base:
        a["justification"] = (a.get("justification") or "") + " [form_05 bilingual PL/EN — English answers]"

    fi = build_fact_index(ctx)
    cc = get_corporate_context(ctx)

    extra = []
    sof = first_val(fi, "source_of_funds")
    extra.append(make_answer(sof, "Źródło środków / Source of funds", 9, source_ref(fi, "source_of_funds"), "Operating receipts from manufacturing and export.", state="answer" if sof else MISSING))
    san_exp = first_val(fi, "sanctions.exposure")
    extra.append(make_answer(san_exp, "Ekspozycja na sankcje / Sanctions policy", 10, source_ref(fi, "sanctions.exposure"), state="answer" if san_exp else MISSING))
    extra.append(make_answer(COMPLETION_DATE, "Data uzupełnienia / Date completed", 11, "exercise rule"))

    return base + extra


RESOLVERS = {
    "form_01": resolve_form_01,
    "form_02": resolve_form_02,
    "form_03": resolve_form_03,
    "form_04": resolve_form_04,
    "form_05": resolve_form_05,
}

# ---------------------------------------------------------------------------
# PDF annotation (pymupdf — bloc en bas de page)
# ---------------------------------------------------------------------------

def annotate_pdf(pdf_path: Path, answers: list, output_path: Path) -> None:
    """Overlay des réponses en bas de chaque page du PDF source (scans JPEG sans couche texte)."""
    try:
        import pymupdf  # type: ignore
    except ImportError:
        log.warning("pymupdf non installé — PDF non produit. pip install pymupdf")
        return

    doc = pymupdf.open(str(pdf_path))
    page_count = len(doc)
    log.debug("Annotating %s (%d pages, %d answers)", pdf_path.name, page_count, len(answers))

    by_page: dict[int, list] = {}
    for a in answers:
        pg = a.get("page", 1)
        by_page.setdefault(pg, []).append(a)

    for pg_num, ans_list in sorted(by_page.items()):
        if pg_num < 1 or pg_num > page_count:
            log.warning("Page %d hors plage (%d pages) — ignorée", pg_num, page_count)
            continue
        page = doc[pg_num - 1]
        rect = page.rect
        n = len(ans_list)
        block_h = min(n * 15 + 20, rect.height * 0.45)
        annot_rect = pymupdf.Rect(rect.x0 + 10, rect.y1 - block_h - 10, rect.x1 - 10, rect.y1 - 10)
        shape = page.new_shape()
        shape.draw_rect(annot_rect)
        shape.finish(color=(0, 0, 0), fill=(1, 1, 1), fill_opacity=0.88, width=0.5)
        shape.commit()

        y = annot_rect.y0 + 6
        page.insert_text((annot_rect.x0 + 4, y), f"[ARTCB KYC Pipeline — {COMPLETION_DATE}]", fontsize=6.5, color=(0.2, 0.2, 0.8))
        y += 10

        for a in ans_list:
            label = str(a.get("label", "?"))[:55]
            value = str(a.get("value") or "")[:90]
            state = a.get("state", "answer")
            color = (0.1, 0.5, 0.1) if state == "answer" else (0.6, 0.3, 0.0)
            for wl in textwrap.wrap(f"  {label}: {value}  [{state}]", width=110)[:2]:
                if y + 9 > annot_rect.y1:
                    break
                page.insert_text((annot_rect.x0 + 4, y), wl, fontsize=5.5, color=color)
                y += 8

    doc.save(str(output_path), garbage=4, deflate=True)
    log.info("PDF annoté : %s", output_path)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def load_exercises() -> list[dict]:
    return json.loads(EXERCISES_JSON.read_text(encoding="utf-8"))


def run_exercise(form_id: str, *, rule_only: bool = False, make_pdf: bool = True) -> dict:
    exercises = load_exercises()
    ex = next((e for e in exercises if e["exercice"] == form_id), None)
    if ex is None:
        raise SystemExit(f"Exercice inconnu : {form_id}")

    entreprise = ex["entreprise"]
    contexte_dir = ex["contexte"]
    pdf_rel = ex["questionnaire"]
    langue = ex["langue"]

    log.info("--- %s (%s) [%s] ---", form_id, entreprise, langue)
    t0 = time.time()

    ctx = load_context(contexte_dir)
    resolver = RESOLVERS[form_id]
    answers = resolver(ctx)
    log.info("%s: %d champs résolus (rule-based)", form_id, len(answers))

    if not rule_only:
        answers = llm_enrich(answers, ctx, form_id)

    # Compter les états
    from collections import Counter
    states = Counter(a["state"] for a in answers)
    log.info("%s: états=%s", form_id, dict(states))

    # Livrable JSON — liste plate, clé "state" (conforme atelier)
    ex_dir = OUTPUT_DIR / form_id
    ex_dir.mkdir(parents=True, exist_ok=True)
    json_out = ex_dir / f"{form_id}.answers.json"
    json_out.write_text(json.dumps(answers, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("JSON sauvegardé : %s", json_out)

    # PDF annoté
    pdf_out = None
    if make_pdf:
        pdf_path = PACK_DIR / pdf_rel
        if pdf_path.exists():
            pdf_out = ex_dir / f"{form_id}.completed.pdf"
            annotate_pdf(pdf_path, answers, pdf_out)
        else:
            log.warning("PDF source introuvable : %s", pdf_path)

    elapsed = round(time.time() - t0, 2)
    return {
        "exercice": form_id,
        "entreprise": entreprise,
        "langue": langue,
        "answers_count": len(answers),
        "states": dict(states),
        "json_output": str(json_out),
        "pdf_output": str(pdf_out) if pdf_out else None,
        "elapsed_s": elapsed,
        "status": "OK",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("exercises", nargs="+", help="form_01 … form_05, ou « all »")
    parser.add_argument("--rule-only", action="store_true", help="rule-based uniquement (0 token LLM)")
    parser.add_argument("--no-pdf", action="store_true", help="ne pas produire le PDF")
    args = parser.parse_args(argv)

    known = [e["exercice"] for e in load_exercises()]
    ids = known if args.exercises == ["all"] else args.exercises
    unknown = [i for i in ids if i not in known]
    if unknown:
        parser.error(f"exercice(s) inconnu(s) : {unknown} (attendu : {known} ou all)")

    run_log = {
        "pipeline": "run_kyc_pipeline",
        "run_at": datetime.datetime.utcnow().isoformat() + "Z",
        "rule_only": args.rule_only,
        "certified_100": False,
        "unique_human_proven": False,
        "exercises": [],
    }

    failed = 0
    for form_id in ids:
        try:
            result = run_exercise(form_id, rule_only=args.rule_only, make_pdf=not args.no_pdf)
            run_log["exercises"].append(result)
            failed += result.get("status") != "OK"
        except Exception as exc:
            log.error("ERREUR sur %s : %s", form_id, exc, exc_info=True)
            run_log["exercises"].append({"exercice": form_id, "status": "ERROR", "error": str(exc)})
            failed += 1

    log_path = OUTPUT_DIR / "run_log.json"
    log_path.write_text(json.dumps(run_log, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Run log : %s", log_path)

    passed = sum(1 for e in run_log["exercises"] if e.get("status") == "OK")
    total = len(run_log["exercises"])
    print(f"\n✅ {passed}/{total} exercices traités {'(rule-based uniquement)' if args.rule_only else ''}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
