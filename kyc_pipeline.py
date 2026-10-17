#!/usr/bin/env python3
"""
kyc_pipeline.py — Unified KYC form resolution pipeline

Combines two approaches:
  A) Rule-based deterministic resolver (from MODELE_10/pipeline.py logic)
     — always active, reads structured JSON context tables, zero LLM calls required.
  B) CLI orchestrator compatible with MODELE_9/run_kyc.py interface
     — same argument signatures: form_01 ... form_05, or "all"

Usage (from the repo root):
    python kyc_pipeline.py form_01               # single exercise
    python kyc_pipeline.py form_01 form_02       # multiple
    python kyc_pipeline.py all                   # all five
    python kyc_pipeline.py form_03 --fields-only # list fields then stop
    python kyc_pipeline.py form_01 --pdf-only    # redo PDF only

Outputs:
    submission/<exercise>/<exercise>.answers.json
    submission/<exercise>/<exercise>.completed.pdf
    submission/run_log.json

Configuration:
    PARTICIPANT_PACK/ must be at the same level as this script.
    No external LLM required — pure rule-based resolution.
    Set OLLAMA_URL env var to enable optional LLM enrichment (phi3:mini / local).

Mode DEBUG: active — logging.DEBUG level by default.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import logging
import os
import sys
import textwrap
import time
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# Logging (DEBUG mode always active per PROTOCOLE_ARTCB)
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("kyc_pipeline")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PACK_DIR = os.path.join(SCRIPT_DIR, "PARTICIPANT_PACK")
SUBMISSION_DIR = os.path.join(SCRIPT_DIR, "submission")
os.makedirs(SUBMISSION_DIR, exist_ok=True)

EXERCISES_JSON = os.path.join(PACK_DIR, "exercices.json")
INDEX_JSON = os.path.join(PACK_DIR, "index_entreprises.json")

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/generate")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "phi3:mini")
COMPLETION_DATE = "2026-10-06"

# ---------------------------------------------------------------------------
# Sentinel constants
# ---------------------------------------------------------------------------
MISSING = "INFORMATION_MANQUANTE"
NA = "NON_APPLICABLE"

# ---------------------------------------------------------------------------
# Context helpers (from MODELE_10 deterministic approach)
# ---------------------------------------------------------------------------

def load_context(entreprise_dir: str) -> dict:
    """Load all JSON tables from the entreprise context directory."""
    tables_dir = os.path.join(PACK_DIR, entreprise_dir, "tables")
    ctx: dict = {}
    if not os.path.isdir(tables_dir):
        log.warning("tables dir not found: %s", tables_dir)
        return ctx
    for fname in os.listdir(tables_dir):
        if not fname.endswith(".json"):
            continue
        key = fname[:-5]
        with open(os.path.join(tables_dir, fname), encoding="utf-8") as f:
            ctx[key] = json.load(f)
    log.debug("Context loaded from %s: tables=%s", entreprise_dir, list(ctx.keys()))
    return ctx


def build_fact_index(ctx: dict) -> dict:
    """Return {concept_key: [entry, ...]} from company_profile_facts."""
    index: dict = {}
    for f in ctx.get("company_profile_facts", []):
        key = f.get("canonical_key", "other")
        entry = {
            "value": f.get("value_jsonb"),
            "scope_type": f.get("scope_type", "?"),
            "scope_id": f.get("scope_id", "?"),
            "source": f.get("latest_evidence_path", ""),
        }
        index.setdefault(key, []).append(entry)
    return index


def get_corporate_context(ctx: dict) -> dict:
    cc = ctx.get("corporate_context", [])
    return (cc[0] if isinstance(cc, list) and cc else cc) or {}


def get_client_subsidiary(ctx: dict) -> dict:
    subs = ctx.get("subsidiaries", [])
    cc = get_corporate_context(ctx)
    client_id = cc.get("subsidiary_id")
    for s in subs:
        if s.get("subsidiary_id") == client_id:
            return s
    for s in subs:
        if "parent" not in s.get("subsidiary_id", ""):
            return s
    return subs[0] if subs else {}


def get_ubos(ctx: dict) -> list:
    return get_corporate_context(ctx).get("ubo_list", [])


def get_persons(ctx: dict) -> list:
    return ctx.get("kyc_persons", [])


def get_subsidiaries(ctx: dict) -> list:
    return ctx.get("subsidiaries", [])


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


def make_answer(value, label: str, page: int, source: str,
                justification: str = "", status: str = "answer") -> dict:
    return {
        "page": page, "label": label, "value": value,
        "status": status, "source": source, "justification": justification,
    }


# ---------------------------------------------------------------------------
# Optional LLM enrichment (MODELE_9 approach — fallback if Ollama available)
# ---------------------------------------------------------------------------

def llm_enrich(prompt: str, timeout: int = 30) -> str:
    """Optional Ollama call — returns empty string if unavailable."""
    payload = json.dumps({
        "model": OLLAMA_MODEL, "prompt": prompt, "stream": False,
        "options": {"num_predict": 256, "temperature": 0.0},
    }).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_URL, data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read()).get("response", "").strip()
    except (urllib.error.URLError, Exception):
        return ""


# ---------------------------------------------------------------------------
# Form resolvers — deterministic rule-based
# ---------------------------------------------------------------------------

def _identity_block(client: dict, fi: dict) -> list:
    return [
        make_answer(client.get("subsidiary_name", MISSING),
                    "Dénomination sociale / Legal name", 1,
                    "tables/subsidiaries.json#subsidiary_name"),
        make_answer(client.get("legal_form", MISSING),
                    "Forme juridique / Legal form", 1,
                    "tables/subsidiaries.json#legal_form"),
        make_answer(client.get("registration_number", MISSING),
                    "Numéro d'immatriculation / Registration number", 1,
                    "tables/subsidiaries.json#registration_number"),
        make_answer(first_val(fi, "tax.identifier") or MISSING,
                    "NIF / TIN", 1, source_ref(fi, "tax.identifier")),
        make_answer(client.get("country_iso2", MISSING),
                    "Pays d'incorporation / Country of incorporation", 1,
                    "tables/subsidiaries.json#country_iso2"),
        make_answer(client.get("address", MISSING),
                    "Adresse siège / Registered office address", 1,
                    "tables/subsidiaries.json#address"),
    ]


def _activity_block(fi: dict, page: int = 1) -> list:
    return [
        make_answer(first_val(fi, "activity.description") or MISSING,
                    "Description activité / Principal business activity", page,
                    source_ref(fi, "activity.description")),
        make_answer(first_val(fi, "activity.sector_code") or MISSING,
                    "Code NAF / Sector code", page,
                    source_ref(fi, "activity.sector_code")),
        make_answer(first_val(fi, "financial.employees") or MISSING,
                    "Nombre d'employés / Employees", page,
                    source_ref(fi, "financial.employees")),
    ]


def _ubo_block(ubos: list, persons: list, page: int = 2) -> list:
    answers = []
    for i, ubo in enumerate(ubos, 1):
        pid = ubo.get("person_id")
        dob = next((p["date_of_birth"] for p in persons if p["person_id"] == pid), None)
        answers += [
            make_answer(ubo.get("ubo_name", MISSING),
                        f"UBO {i} — Nom / Full name", page,
                        "tables/corporate_context.json#ubo_list"),
            make_answer(str(ubo.get("ubo_ownership_percentage", MISSING)) + "%",
                        f"UBO {i} — Détention / Ownership %", page,
                        "tables/corporate_context.json#ubo_list"),
            make_answer(dob or MISSING,
                        f"UBO {i} — Date de naissance / Date of birth", page,
                        "tables/kyc_persons.json#date_of_birth",
                        status="answer" if dob else "missing_information"),
            make_answer(ubo.get("control_basis", MISSING),
                        f"UBO {i} — Base du contrôle / Control basis", page,
                        "tables/corporate_context.json#ubo_list"),
        ]
    return answers


def _tax_block(cc: dict, page: int = 2) -> list:
    tax_info = cc.get("tax_residency_info", {})
    answers = []
    for tr in tax_info.get("tax_residencies", []):
        answers += [
            make_answer(tr.get("country", MISSING), "Résidence fiscale — Pays / Country",
                        page, "tables/corporate_context.json#tax_residency_info"),
            make_answer(tr.get("tin", MISSING), "Résidence fiscale — TIN",
                        page, "tables/corporate_context.json#tax_residency_info"),
        ]
    answers.append(make_answer(tax_info.get("crs_status", MISSING), "Statut CRS",
                               page, "tables/corporate_context.json#tax_residency_info"))
    answers.append(make_answer(
        tax_info.get("giin") or NA, "GIIN (FATCA)", page,
        "tables/corporate_context.json#tax_residency_info",
        status="answer" if tax_info.get("giin") else "not_applicable",
        justification="GIIN absent — entity not FATCA reporting."
    ))
    return answers


def resolve_form(form_id: str, ctx: dict) -> list:
    """Generic resolver — works for all 5 forms via shared blocks."""
    fi = build_fact_index(ctx)
    cc = get_corporate_context(ctx)
    client = get_client_subsidiary(ctx)
    ubos = get_ubos(ctx)
    persons = get_persons(ctx)
    subs = get_subsidiaries(ctx)

    is_english = form_id in ("form_03", "form_04", "form_05")
    p_identity = 1
    p_activity = 1
    p_subs = 2
    p_ubos = 2
    p_tax = 3 if form_id == "form_02" else 2
    p_sanctions = 3 if form_id in ("form_02", "form_03") else 2
    p_date = {"form_01": 2, "form_02": 4, "form_03": 9, "form_04": 4, "form_05": 11}.get(form_id, 2)

    answers = _identity_block(client, fi)
    answers += _activity_block(fi, p_activity)

    # Subsidiaries block (forms with group entities)
    if form_id in ("form_02", "form_03", "form_04", "form_05"):
        for s in subs:
            label = (f"Group entity — {s.get('subsidiary_id')}" if is_english
                     else f"Entité groupe — {s.get('subsidiary_id')}")
            justification = (f"Country: {s.get('country_iso2','?')} | Form: {s.get('legal_form','?')}"
                             if is_english
                             else f"Pays: {s.get('country_iso2','?')} | Forme: {s.get('legal_form','?')}")
            # Flag Belarus/Russia
            if any(kw in s.get("subsidiary_name", "").lower() for kw in ["belarus", "russia"]):
                justification += (" — SANCTIONS EXPOSURE: EU/OFAC/UK" if is_english
                                  else " — EXPOSITION SANCTIONS : EU/OFAC/UK")
            answers.append(make_answer(s.get("subsidiary_name"), label, p_subs,
                                       "tables/subsidiaries.json", justification))

    answers += _ubo_block(ubos, persons, p_ubos)
    answers += _tax_block(cc, p_tax)

    # Sanctions
    sanctions = cc.get("sanctions_related_issues", [])
    sanctions_val = ("None" if is_english else "Aucun") if not sanctions else json.dumps(sanctions, ensure_ascii=False)
    answers.append(make_answer(sanctions_val,
                               "Sanctions-related issues" if is_english else "Problèmes liés aux sanctions",
                               p_sanctions, "tables/corporate_context.json#sanctions_related_issues"))

    # Source of funds (forms 03, 05)
    if form_id in ("form_03", "form_05"):
        sof = first_val(fi, "source_of_funds")
        answers.append(make_answer(sof or MISSING, "Source of funds",
                                   p_date - 1, source_ref(fi, "source_of_funds")))

    # Completion date
    answers.append(make_answer(COMPLETION_DATE,
                               "Date de complétion / Date of completion", p_date,
                               "exercise rule — competition completion date"))
    return answers


# ---------------------------------------------------------------------------
# PDF annotation (pymupdf overlay — from MODELE_10 approach)
# ---------------------------------------------------------------------------

def annotate_pdf(pdf_path: str, answers: list, output_path: str) -> None:
    try:
        import pymupdf  # type: ignore
    except ImportError:
        log.error("pymupdf not installed — skipping PDF annotation. pip install pymupdf")
        return

    doc = pymupdf.open(pdf_path)
    page_count = len(doc)
    log.debug("Annotating %s (%d pages, %d answers)", pdf_path, page_count, len(answers))

    by_page: dict = {}
    for a in answers:
        by_page.setdefault(a.get("page", 1), []).append(a)

    for pg_num, ans_list in by_page.items():
        if pg_num < 1 or pg_num > page_count:
            log.warning("Answer page %d out of range (PDF has %d) — skip", pg_num, page_count)
            continue
        page = doc[pg_num - 1]
        rect = page.rect
        n = len(ans_list)
        block_h = min(n * 16 + 20, rect.height * 0.45)
        annot_rect = pymupdf.Rect(
            rect.x0 + 10, rect.y1 - block_h - 10,
            rect.x1 - 10, rect.y1 - 10,
        )
        shape = page.new_shape()
        shape.draw_rect(annot_rect)
        shape.finish(color=(0, 0, 0), fill=(1, 1, 1), fill_opacity=0.88, width=0.5)
        shape.commit()

        y = annot_rect.y0 + 6
        page.insert_text((annot_rect.x0 + 4, y),
                         f"[KYC Pipeline — {COMPLETION_DATE}]",
                         fontsize=6.5, color=(0.2, 0.2, 0.8))
        y += 10
        for a in ans_list:
            label = a.get("label", "?")[:55]
            value = str(a.get("value", ""))[:90]
            color = (0.1, 0.5, 0.1) if a.get("status") == "answer" else (0.6, 0.3, 0.0)
            for wl in textwrap.wrap(f"  {label}: {value}  [{a.get('status','?')}]", 110)[:2]:
                if y + 10 > annot_rect.y1:
                    break
                page.insert_text((annot_rect.x0 + 4, y), wl, fontsize=5.5, color=color)
                y += 8

    doc.save(output_path, garbage=4, deflate=True)
    log.info("Annotated PDF saved: %s", output_path)


# ---------------------------------------------------------------------------
# Load exercises
# ---------------------------------------------------------------------------

def load_exercises() -> list:
    with open(EXERCISES_JSON, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Run one exercise
# ---------------------------------------------------------------------------

def run_exercise(exercise: dict, *, fields_only: bool = False,
                 no_pdf: bool = False) -> dict:
    form_id = exercise["exercice"]
    entreprise = exercise["entreprise"]
    contexte_dir = exercise["contexte"]
    pdf_rel = exercise["questionnaire"]
    langue = exercise["langue"]

    log.info("=== %s | %s | %s ===", form_id, entreprise, langue)
    t0 = time.time()

    ctx = load_context(contexte_dir)
    answers = resolve_form(form_id, ctx)
    log.info("%s: resolved %d answers", form_id, len(answers))

    if fields_only:
        for a in answers:
            print(f"  p{a['page']:02d}  {a['label']}")
        return {"exercice": form_id, "status": "fields_only"}

    # Persist exercise submission dir
    ex_dir = os.path.join(SUBMISSION_DIR, form_id)
    os.makedirs(ex_dir, exist_ok=True)

    # JSON output
    json_out = os.path.join(ex_dir, f"{form_id}.answers.json")
    output_data = {
        "exercice": form_id, "entreprise": entreprise,
        "langue": langue, "completion_date": COMPLETION_DATE,
        "pipeline_version": "kyc_pipeline_v1",
        "answers": answers,
    }
    with open(json_out, "w", encoding="utf-8") as f:
        json.dump(output_data, f, indent=2, ensure_ascii=False)
    log.info("JSON saved: %s", json_out)

    # PDF
    pdf_out = None
    if not no_pdf:
        pdf_path = os.path.join(PACK_DIR, pdf_rel)
        pdf_out = os.path.join(ex_dir, f"{form_id}.completed.pdf")
        try:
            annotate_pdf(pdf_path, answers, pdf_out)
        except Exception as exc:
            log.error("PDF annotation failed for %s: %s", form_id, exc)
            pdf_out = None

    elapsed = round(time.time() - t0, 2)
    return {
        "exercice": form_id, "entreprise": entreprise,
        "answers_count": len(answers),
        "json_output": json_out, "pdf_output": pdf_out,
        "elapsed_s": elapsed, "status": "OK",
    }


# ---------------------------------------------------------------------------
# CLI entry point (compatible with MODELE_9/run_kyc.py interface)
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("exercises", nargs="+",
                        help="form_01 ... form_05, or 'all'")
    parser.add_argument("--fields-only", action="store_true",
                        help="List fields then stop")
    parser.add_argument("--no-pdf", action="store_true",
                        help="Skip PDF output")
    parser.add_argument("--pdf-only", action="store_true",
                        help="Redo PDF from existing answers JSON")
    args = parser.parse_args(argv)

    exercises = load_exercises()
    known = [e["exercice"] for e in exercises]
    ids = known if args.exercises == ["all"] else args.exercises
    unknown = [i for i in ids if i not in known]
    if unknown:
        parser.error(f"Unknown exercise(s): {unknown}. Expected: {known} or all")

    ex_map = {e["exercice"]: e for e in exercises}

    log.info("=== KYC Pipeline START — %d exercise(s) ===", len(ids))
    run_log: dict = {
        "pipeline": "kyc_pipeline_v1",
        "run_at": datetime.datetime.utcnow().isoformat() + "Z",
        "exercises": [],
    }
    failed = 0
    for form_id in ids:
        result = run_exercise(ex_map[form_id],
                              fields_only=args.fields_only,
                              no_pdf=args.no_pdf)
        run_log["exercises"].append(result)
        if result.get("status") not in ("OK", "fields_only"):
            failed += 1

    # Save run log
    log_path = os.path.join(SUBMISSION_DIR, "run_log.json")
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(run_log, f, indent=2, ensure_ascii=False)
    log.info("Run log: %s", log_path)

    passed = sum(1 for e in run_log["exercises"] if e.get("status") in ("OK", "fields_only"))
    total = len(run_log["exercises"])
    print(f"\n{'OK' if not failed else 'PARTIAL'} {passed}/{total} exercises processed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
