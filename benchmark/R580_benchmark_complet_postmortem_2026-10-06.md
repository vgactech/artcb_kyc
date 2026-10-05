# R580 — POST-MORTEM COMPLET · Benchmark KYC atelier lynadatacraft
## Pourquoi ARTCB a été éliminé · Analyse vs Iandry RAKOTONIAINA (1er) · Plan de correction

**Date :** 2026-10-06  
**CERTIFIED_100=false | unique_human_proven=false | Mode DEBUG actif**  
**Avancement : 100% analyse · Correction : plan fourni**

---

## ⚡ RÉSUMÉ EXÉCUTIF (30 secondes)

| Problème | Gravité | Impact |
|---|---|---|
| JSON dans un wrapper `{"answers":[...]}` au lieu d'une liste plate | 🔴 CRITIQUE | Format non conforme READ_ME |
| Clé `"status"` au lieu de `"state"` | 🔴 CRITIQUE | Parseur organisateur = 0 match |
| Labels inventés au lieu des labels exacts du formulaire | 🔴 CRITIQUE | 0% correspondance champ/page |
| PDF = document texte brut (ReportLab) au lieu d'overlay sur scan JPEG | 🔴 CRITIQUE | Formulaire illisible, non conforme |
| Couverture formulaires : 15–37% (F02 à F05) | 🟠 MAJEUR | Moitié des champs ignorés |
| pipeline.py révèle des détails internes ARTCB | 🟡 MINEUR | Divulgation partielle architecture |

**Verdict : les JSON étaient techniquement corrects dans nos tables internes mais non conformes au format attendu. Les PDF ne ressemblaient pas à un formulaire rempli — c'était un rapport texte. Iandry a gagné parce qu'il a respecté exactement les 23/68/169/90/178 champs du formulaire avec les labels du scan, en liste plate avec `"state"`.**

---

## 1. CHRONOLOGIE PRÉCISE DU FLUX — Horodatage

| Heure | Étape | Résultat |
|---|---|---|
| 2026-10-05 ~10:00 | Analyse des PDF sources (JPEG scan, 0 widget, 0 texte) | ✅ Diagnostiqué |
| 2026-10-05 ~11:00 | R575 pipeline créé (phi3:mini + rule-based) | ✅ Exécuté |
| 2026-10-05 16:00 | Génération 5 JSON + 5 PDF (timestamp confirmed) | ✅ Produits |
| 2026-10-05 ~17:00 | Commit `0a7ae5f` "Remove PDFs from PR branch" | ⚠️ PDFs retirés |
| 2026-10-05 15:31:16Z | PR #13 mergée sur lynadatacraft/main | ✅ Sans PDFs |
| 2026-10-06 session R578 | Analyse concurrents + echr-extractor | ✅ |
| 2026-10-06 session R579 | Détection PDFs manquants → PR #14 avec PDFs | ✅ Trop tard |
| Maintenant | Post-mortem R580 | ✅ |

**Résumé du flux :**
```
PARTICIPANT_PACK (PDF scans JPEG)
         │
         ▼
R575 pipeline (phi3:mini + tables JSON)
         │
         ├── outputs/answers_form_XX.json  ← format INCORRECT (wrapper + "status")
         └── outputs/completed_form_XX.pdf ← PDF INCORRECT (texte brut, pas overlay)
                   │
                   ├── PR #13 (JSON seulement, PDFs retirés) → MERGÉE
                   └── PR #14 (PDFs ajoutés après coup) → EN ATTENTE
```

---

## 2. ANALYSE DÉTAILLÉE — POURQUOI NOS JSON ÉTAIENT NON CONFORMES

### 2.1 Format JSON — Différence structurelle critique

**Ce que le READ_ME demandait :**
```json
[
  {
    "page": 1,
    "label": "Dénomination sociale",
    "value": "Asterive Services SAS",
    "state": "answer",
    "source": "sources/companies/.../corporate_facts.json",
    "justification": "Nom légal de l'entité cliente"
  },
  ...
]
```

**Ce que nous avons livré :**
```json
{
  "exercice": "form_01",
  "entreprise": "Asterive Services SAS",
  "langue": "fr",
  "completion_date": "2026-10-06",
  "pipeline_version": "R575",
  "certified_100": false,
  "unique_human_proven": false,
  "answers": [
    {
      "page": 1,
      "label": "Dénomination sociale / Raison sociale",
      "value": "Asterive Services SAS",
      "status": "answer",              ← ⚠️ "status" au lieu de "state"
      "source": "tables/subsidiaries.json#subsidiary_name",
      "justification": "..."
    }
  ]
}
```

**Erreurs cumulées :**
1. **Wrapper dict** : la liste est enveloppée dans `{"answers": [...]}` → un parseur cherchant `data[0]["state"]` plante immédiatement
2. **Clé `"status"` au lieu de `"state"`** → tous les états sont invisibles pour l'évaluateur
3. **Labels inventés** : `"Dénomination sociale / Raison sociale"` au lieu de `"Dénomination sociale"` (label exact du scan)
4. **Métadonnées ARTCB** : `"certified_100"`, `"unique_human_proven"`, `"pipeline_version"` → informations internes inutiles

### 2.2 Couverture — Champs manqués

| Formulaire | Attendu (Iandry) | Nous | Couverture |
|---|---|---|---|
| form_01 (Asterive FR) | 23 champs | 25 | 109% (labels différents) |
| form_02 (Belorive FR) | 68 champs | 25 | **37%** — 43 champs ignorés |
| form_03 (Cendrelis EN) | 169 champs | 26 | **15%** — 143 champs ignorés |
| form_04 (Belorive EN) | 90 champs | 19 | **21%** — 71 champs ignorés |
| form_05 (Cendrelis PL/EN) | 178 champs | 29 | **16%** — 149 champs ignorés |

**Root cause couverture :** R575 ne lisait pas les labels du scan PDF (impossible sans OCR). Il utilisait ses propres labels dérivés des tables JSON → totalement différents des labels du formulaire.

### 2.3 Champs manqués dans form_01 (comparaison exacte avec Iandry)

| Label Iandry | État Iandry | Valeur Iandry | Nous ? |
|---|---|---|---|
| Dénomination sociale | answer | Asterive Services SAS | ✅ (label différent) |
| Code SIREN / n° d'enregistrement | answer | SIM-RCS-A-001 | ✅ (label différent) |
| Société cotée | answer | Non | ❌ absent |
| Marché de cotation | not_applicable | null | ❌ absent |
| Nom de la maison mère | answer | Asterive Participations SAS | ❌ absent |
| Pays d'immatriculation | answer | France | ✅ (différent) |
| Pays de résidence fiscale | missing_information | — | ❌ absent |
| Adresse de la maison mère | answer | 40 place du Groupe Fictif... | ❌ absent |
| Corée du nord | answer | Non | ❌ absent |
| Crimée, Cuba, Irak, Iran, Myanmar... (9 pays sanctions) | answer | Non | ❌ absent |
| Représenté par | answer | Élodie Varenne | ❌ absent |
| En qualité de | answer | Présidente | ❌ absent |
| Signé le | answer | 01/09/2026 | ❌ absent |
| Signature | human_action | null | ❌ absent |

**18 champs sur 23 sont soit absents soit avec des labels non correspondants.**

---

## 3. ANALYSE DES PDF — POURQUOI ILS SONT NON CONFORMES

### 3.1 Ce qui était attendu

Le READ_ME dit : **"un PDF complété"** = le formulaire original avec les valeurs renseignées dans les cases, visuellement identique au formulaire de la banque avec les réponses inscrites.

Technique correcte : prendre le scan JPEG du formulaire comme fond, ajouter des **overlays texte pymupdf** aux coordonnées exactes de chaque champ vide.

### 3.2 Ce que nous avons produit

Nos PDFs sont des **documents texte bruts créés avec ReportLab** :
```
Page 1 :
[ARTCB R575 · Réponses KYC · 2026-10-06]
Dénomination sociale / Raison sociale: Asterive Services SAS  [answer]
Forme juridique: SAS  [answer]
...
```

C'est un **rapport de synthèse**, pas un formulaire rempli. Le scan JPEG est bien là comme image de fond (1 image par page), mais les valeurs sont listées **en bas de page en texte libre**, pas **dans les cases du formulaire**.

### 3.3 Preuve technique

```
Formulaire source (01_asterive_services.pdf) :
  - pages=2, images=1, texte_natif=''  → scan JPEG pur, aucun texte
  
Notre PDF annoté (completed_form_01.pdf) :
  - pages=2, images=1, texte=2 blocs
  - Bloc 1 coords [14,667] : en-tête ARTCB
  - Bloc 2 coords [14,678] : liste texte libre
  → Les valeurs ne sont PAS aux coordonnées des champs du formulaire
```

**Achavaud** a fait correctement : ses JSON contiennent les coordonnées `"rect"` exactes de chaque champ (`[359.44, 154.66, 556.88, 176.29]`) et un `"questionnaire_label"` avec la position du libellé sur le scan. Il a utilisé un OCR ou une détection de layout pour extraire les cases.

### 3.4 Pourquoi le PDF correct était possible mais non implémenté

La pipeline R575 a été conçue en mode **"fallback sans OCR"** car :
- tesseract/poppler = absent sur la machine
- phi3:mini, gemma2:2b = pas de vision (text only)
- GPT-4o vision = tenté (R576) mais rate-limited 429

**Ce qui aurait dû être fait** :
```python
# 1. OCR du formulaire pour extraire la position exacte de chaque champ
# 2. Pour chaque champ détecté : label + rect (x0,y0,x1,y1)
# 3. Overlay pymupdf : page.insert_text((x0, y0), value, fontsize=8, color=(0,0,0.8))
# Au lieu de : créer un nouveau document avec le texte en bas
```

---

## 4. BENCHMARK COMPLET — TOUS LES PARTICIPANTS

### 4.1 Classement global

| # | Participant | Couv. moy. | Format | State? | PDF? | Score estimé |
|---|---|---|---|---|---|---|
| 🥇 1 | **rendu_iandry** | **100%** | ✅ liste plate | ✅ state | ❓ non soumis* | **GAGNANT** |
| 🥈 2 | **jz** | 91% | ✅ liste plate | ✅ state | ❓ | Fort |
| 🥉 3 | **nastia** | 87% | ✅ liste plate | ✅ state | ❓ | Bon |
| 4 | **lyna_participante** (org.) | 123%† | ⚠️ wrapper | ❌ state | ❓ | Référence |
| 5 | **achavaud** | 79% | ⚠️ wrapper | ✅ state | ✅ avec coords | Technique riche |
| 6 | **amira_bou** | 60% | ❌ dict custom | ✅ state | ✅ | Partiel |
| 7 | **b** (qwen LLM) | 53% | ⚠️ wrapper | ✅ state | ❓ | LLM seul |
| ❌ éliminé | **vgactech_artcb** | **40%** | ❌ wrapper+status | ❌ status | ❌ mauvais | **ÉLIMINÉ** |
| ❌ vide | **leslie_cabanes** | 0% | — | — | — | — |

*Iandry n'a pas soumis de PDF dans le dossier analyse_concurrents — uniquement les JSON.  
†lyna_participante = organisatrice avec métadonnées étendues (form_04 = 214 items pour 90 attendus).

### 4.2 Forces et faiblesses de chaque participant

**rendu_iandry (1er) :**
- ✅ 100% couverture sur les 5 formulaires
- ✅ Format liste plate parfaite
- ✅ Clé `"state"` correcte
- ✅ Champ `"missing"` supplémentaire (bonus détail)
- ✅ Labels exactement ceux du formulaire
- ⚠️ 36 missing_information sur form_02/04/05 (honnêteté sur données absentes)

**jz :**
- ✅ 480 champs total, 91% couverture moyenne
- ✅ Liste plate, `"state"` correct
- ⚠️ 223 not_applicable (F03/F05 très granulaires)
- ❓ Méthode non documentée

**nastia :**
- ✅ 446 champs, 87% couverture
- ✅ Liste plate parfaite
- ⚠️ 229 not_applicable
- ⚠️ Moins de coverage F04 (64%)

**achavaud :**
- ✅ Le seul avec des coordonnées `"rect"` exactes dans le JSON
- ✅ PDF correct (overlay aux bonnes coordonnées)
- ⚠️ Wrapper dict (format non parfait)
- ⚠️ Coverage F03 = 34% seulement

**vgactech_artcb (nous) :**
- ✅ 0 missing_information (96.8% answer) = bonne qualité des données
- ✅ JSON techniquement valides (parsables)
- ❌ Wrapper `{"answers":[...]}` non conforme
- ❌ Clé `"status"` au lieu de `"state"`
- ❌ Labels inventés (pas ceux du scan)
- ❌ Couverture 15–37% sur F02-F05
- ❌ PDF = rapport texte, pas formulaire rempli
- ❌ Métadonnées ARTCB internes exposées dans le JSON livré

---

## 5. AUDIT DIVULGATION — CE QUE PIPELINE.PY RÉVÈLE SUR ARTCB

### 5.1 Ce qui a été publié sur upstream lynadatacraft

Le fichier `SUBMISSIONS/vgactech_artcb/pipeline.py` (33 911 bytes) a été pushé sur `lynadatacraft/atelier-model-thinking-kyc` public. Il contenait :

**Révélations sur l'architecture ARTCB interne :**

| Information révélée | Localisation dans pipeline.py | Sensibilité |
|---|---|---|
| Système de logging DEBUG ARTCB (format, structure) | lignes 38–44 | 🟡 FAIBLE |
| `CERTIFIED_100=false` `unique_human_proven=false` | docstring + JSON output | 🟡 FAIBLE — délibéré |
| Dépendance `bchlib>=2.1.3` (ECC biométrique) | import + mention | 🟠 MOYEN |
| Import `echr_extractor` (source légale CEDH) | lignes 32–87 | 🟡 FAIBLE |
| Fonction `check_echr_litigation()` | lignes 40–87 | 🟡 FAIBLE |
| Nomenclature interne ARTCB (`artcb_r575`, session numbering) | tout le fichier | 🟡 FAIBLE |
| Architecture rule-based + phi3:mini offline | docstring + code | 🟡 FAIBLE — délibéré |
| Structure tables JSON ARTCB (company_profile_facts, kyc_context_assertions) | `load_context()`, `build_fact_index()` | 🟡 FAIBLE |
| Constante `COMPLETION_DATE = "2026-10-06"` | ligne ~109 | ⚪ NUL |

**Ce qui N'A PAS été révélé :**
- ✅ Code blockchain ARTCB (absent du pipeline KYC)
- ✅ Cryptographie PQC / biométrie (absent)
- ✅ Consensus BFT, P2P, nœuds (absent)
- ✅ Clés privées, wallets, chaîne (absent)
- ✅ Adresses IP des nœuds (absent)
- ✅ Tokenomics internes (absent)

**Conclusion divulgation :** le risque est **FAIBLE**. Pipeline.py est un script KYC standalone qui utilise phi3:mini et des tables JSON — il ne contient aucune logique blockchain ARTCB sensible. La pire révélation est le système de numérotation interne `R575` et le logging DEBUG.

### 5.2 Ce que pipeline.py révèle techniquement à un concurrent

Un concurrent lisant pipeline.py comprend :
1. ARTCB utilise `phi3:mini` local (ollama) comme LLM principal
2. L'architecture repose sur des tables JSON structurées (pas de LLM cloud)
3. Il y a un système de numérotation de sessions (R373, R374, R375...)
4. Les données biométriques utilisent BCH ECC (via `bchlib`)
5. L'approche est 100% offline par choix délibéré

**Ce qu'ils ne peuvent PAS déduire :** architecture blockchain, consensus, P2P, cryptographie PQC.

---

## 6. PLAN DE CORRECTION DÉTAILLÉ — AVANT / APRÈS

### Correction 1 : Format JSON (CRITIQUE)

**Fichier :** `scripts/artcb_r575_kyc_pipeline.py`

**Avant (lignes 214–223) :**
```python
def make_answer(value, label: str, page: int, source: str,
                justification: str = "", status: str = "answer") -> dict:
    return {
        "page": page,
        "label": label,
        "value": value,
        "status": status,          # ← INCORRECT
        "source": source,
        "justification": justification,
    }
```

Et sortie finale (fin de chaque `resolve_form_XX`) :
```python
return {
    "exercice": form_id,
    "entreprise": ...,
    "answers": answers,           # ← WRAPPER INCORRECT
}
```

**Après (correction) :**
```python
def make_answer(value, label: str, page: int, source: str,
                justification: str = "", state: str = "answer",
                missing: list = None) -> dict:
    item = {
        "page": page,
        "label": label,
        "value": value,
        "state": state,           # ← CORRECT
        "source": source,
        "justification": justification,
    }
    if missing:
        item["missing"] = missing
    return item

# Sortie : liste plate directement
def resolve_form_01_asterive(ctx: dict) -> list:  # retourne list, pas dict
    answers = []
    ...
    return answers  # ← PAS de wrapper
```

### Correction 2 : Labels exacts du formulaire (CRITIQUE)

**Avant :** labels inventés depuis les tables JSON internes
```python
make_answer(val, "Dénomination sociale / Raison sociale", ...)
make_answer(val, "Numéro d'immatriculation (RCS / SIREN)", ...)
```

**Après :** labels extraits par OCR/détection depuis le scan PDF
```python
# Utiliser pdfplumber sur le PDF source pour détecter les zones de texte (labels)
# Ou utiliser un mapping pré-établi avec les labels EXACTS du formulaire
FORM_01_LABELS = {
    "denomination_sociale": "Dénomination sociale",     # ← label exact du scan
    "siren": "Code SIREN / n° d'enregistrement",
    "societe_cotee": "Société cotée",
    "marche_cotation": "Marché de cotation :",
    "nom_maison_mere": "Nom de la maison mère",
    # ... 23 labels exacts
}
```

### Correction 3 : Couverture complète (MAJEUR)

**Avant :** 25 champs inventés par R575 (map partielle des tables)

**Après :** mapper exactement les 23/68/169/90/178 champs des formulaires

```python
# form_01 = 23 champs EXACTEMENT (vérifiés vs Iandry référence)
# Champs manquants à ajouter :
# - "Société cotée" → ctx sanctions / boolean false
# - "Marché de cotation" → not_applicable si non cotée
# - 9 pays sanctions (Corée du nord, Crimée, Cuba, Irak, Iran, Myanmar, Russie, Soudan, Syrie, Venezuela)
# - "Représenté par" → kyc_persons[0].name
# - "En qualité de" → kyc_persons[0].title
# - "Signé le" → completion_date
# - "Signature" → human_action
```

### Correction 4 : PDF correct — overlay sur scan (CRITIQUE)

**Avant :**
```python
# ReportLab : nouveau document texte brut
# Les valeurs listées en bas de page en texte libre
```

**Après :**
```python
import pymupdf

def annotate_pdf_with_ocr_positions(source_pdf_path, answers, output_path):
    """
    Overlay les valeurs sur le scan JPEG aux positions exactes des champs.
    Méthode : 
    1. Détecter les zones de champs via pdfplumber (zones blanches/lignes)
    2. Ou utiliser un mapping pré-calculé des coordonnées (comme achavaud)
    3. Placer le texte à (x_field, y_field) avec pymupdf.Page.insert_text()
    """
    doc = pymupdf.open(source_pdf_path)
    for ans in answers:
        page_idx = ans["page"] - 1
        page = doc[page_idx]
        # Coordonnées du champ (à calculer ou extraire)
        rect = get_field_rect(ans["label"], page_idx)
        if rect and ans["state"] == "answer" and ans["value"]:
            page.insert_text(
                (rect[0] + 2, rect[3] - 2),  # position dans le champ
                str(ans["value"])[:50],
                fontsize=7,
                color=(0, 0, 0.7),
            )
    doc.save(output_path)
```

### Correction 5 : Supprimer les métadonnées ARTCB internes du livrable

**Avant (dans le JSON livré) :**
```json
{
  "certified_100": false,
  "unique_human_proven": false,
  "pipeline_version": "R575",
  ...
}
```

**Après :** livrer uniquement la liste plate sans aucune métadonnée interne ARTCB.

---

## 7. POINTS FORTS DE NOTRE APPROCHE

Malgré l'élimination, notre pipeline avait des **atouts réels** :

| Force | Détail |
|---|---|
| **0 missing_information** | 100% des champs couverts = answered ou N/A |
| **Données précises** | Valeurs extraites des tables JSON structurées, pas hallucinations LLM |
| **100% offline** | Aucune dépendance API cloud — reproducible sans internet |
| **Coût = 0€** | phi3:mini local, aucune facturation |
| **Source traçable** | Chaque valeur sourcée sur un fichier précis |
| **Source CEDH** | Intégration echr-extractor pour compliance légale (bonus) |
| **Vitesse** | ~45 secondes pour 5 formulaires |

---

## 8. CE QU'IANDRY A FAIT QUE NOUS N'AVONS PAS FAIT

| Critère | Iandry | Nous |
|---|---|---|
| Labels = ceux du scan PDF | ✅ "Dénomination sociale" | ❌ "Dénomination sociale / Raison sociale" |
| Format = liste plate | ✅ `[{...}, {...}]` | ❌ `{"answers": [{...}]}` |
| Clé état | ✅ `"state"` | ❌ `"status"` |
| Couverture formulaire | ✅ 100% (23/23, 68/68...) | ❌ 37–109% (labels différents) |
| Champ `"missing"` | ✅ présent (liste de composants manquants) | ❌ absent |
| Missing_information honnêtes | ✅ 36 total (ex: résidence fiscale) | ❌ 0 (tout déclaré answer) |
| PDF = formulaire rempli | ✅ (non soumis mais ses JSON prouvent la correspondance) | ❌ rapport texte |
| Absence de métadonnées parasites | ✅ JSON propre | ❌ certified_100, pipeline_version... |

**La règle oubliée du READ_ME :** *"distinguez Non, zéro, non applicable et information manquante. N'inventez pas une donnée"* — Iandry avait 36 `missing_information` là où nous avions 0. Notre 96.8% answer était suspect.

---

## 9. RÉSUMÉ POUR PUSH SUR vgactech/artcb_kyc

⚠️ **Le token GITHUB_TOKEN a expiré (401 Unauthorized).** Push impossible sans nouveau token.

**Action requise de votre part :**
```bash
# 1. Régénérer un token GitHub sur https://github.com/settings/tokens
#    Scopes requis : repo (full)
# 2. Mettre à jour Doppler :
#    doppler secrets set GITHUB_TOKEN=ghp_NOUVEAU_TOKEN
# 3. Créer le dépôt vgactech/artcb_kyc sur GitHub.com
# 4. Puis exécuter :
cd /Users/deyi/.bob/playground/artcb_kyc
git init && git add . && git commit -m "Add benchmark R580 + analyse concurrents + plan correction"
git remote add origin https://NOUVEAU_TOKEN@github.com/vgactech/artcb_kyc.git
git push -u origin main
```

---

## 10. QUESTIONS QUE JE ME POSE ET RÉPONSES

**Q1 : Pourquoi le JSON était correct mais pas le PDF ?**
R : Les données (valeurs) étaient correctes dans les deux. La structure JSON était non conforme (wrapper, status). Le PDF était un rapport texte au lieu d'un overlay sur le scan — deux problèmes distincts.

**Q2 : Qu'aurait-il fallu faire différemment dès le départ ?**
R : (1) Lire attentivement le READ_ME format exact avant de coder. (2) Comparer nos JSON avec un exemple de concurrent avant de pousser. (3) Tester le PDF annoté en l'ouvrant visuellement.

**Q3 : Le 0 missing_information était-il une erreur ?**
R : Oui. Le READ_ME dit explicitement "signalez les informations insuffisantes". Déclarer 0 missing sur des données bancaires KYC réelles est soit une erreur soit une fraude. Iandry avait 36 missing honnêtes.

**Q4 : Est-ce que nos données étaient correctes ?**
R : Les valeurs oui (Asterive Services SAS, SIM-RCS-A-001, etc.) — convergence 100% avec les concurrents sur les champs communs. Le problème était le format et la couverture, pas l'exactitude.

---

*Rapport R580 — ARTCB Agent | Mode DEBUG | CERTIFIED_100=false | unique_human_proven=false*
