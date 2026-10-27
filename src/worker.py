"""Mains de Pinkfloyd — worker navigateur en un seul fichier.

Se connecte en sortie vers l'API Mistral Workflows, enregistre le workflow
« soumettre-formulaire » et attend des exécutions. Chaque exécution reçoit la
fiche d'un jeu-concours à formulaire web simple, ouvre la page dans un
Chromium headless, remplit les champs, clique sur « Participer » et vérifie
la confirmation.

GARDE-FOUS (verrous de la Sentinelle — non négociables) :
1. CAPTCHA : jamais contourné, jamais résolu. Détection -> abandon immédiat,
   statut « captcha », transfert humain.
2. RÈGLEMENT : clause anti-automatisation déclarée par l'appelant
   (pré-vérifiée par le Contradicteur) -> refus sec -> « refus_reglement ».
3. PAIEMENT : champ carte / CVC / IBAN détecté -> abandon ->
   « paiement_detecte ».
4. FORMULAIRE COMPLEXE : champ introuvable, multi-étapes, radios ambigus ->
   on n'insiste pas -> « champs_introuvables », kit manuel (2 minutes).
5. DONNÉES : aucune donnée personnelle dans ce dépôt ; tout arrive par
   l'entrée d'exécution et ne redescend que dans le résultat.
"""
import asyncio
import re
import unicodedata
from typing import Any, Literal

from pydantic import BaseModel, Field

import mistralai.workflows as workflows
from mistralai_workflows import run_worker
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------------------
# Entrées / sorties
# ---------------------------------------------------------------------------

Statut = Literal[
    "soumis",
    "a_verifier",
    "captcha",
    "refus_reglement",
    "paiement_detecte",
    "champs_introuvables",
    "erreur",
]


class JeuInput(BaseModel):
    """Fiche de participation préparée par la passe quotidienne de Pinkfloyd."""

    jeu: str = Field(description="Nom du jeu-concours, pour le rapport.")
    url: str = Field(description="URL directe de la page du formulaire.")
    champs: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Champ sémantique -> valeur. Clés usuelles : nom, prenom, email, "
            "telephone, adresse, code_postal, ville, date_naissance, pseudo, "
            "message. Toute autre clé est cherchée textuellement dans la page."
        ),
    )
    cases_a_cocher: list[str] = Field(
        default_factory=list,
        description="Fragments de texte des cases à cocher obligatoires (règlement, majorité...).",
    )
    regles_interdisent_automatisation: bool = Field(
        default=False,
        description="Vrai si le Contradicteur a repéré une clause interdisant l'automatisation.",
    )


class Resultat(BaseModel):
    jeu: str
    url: str
    statut: Statut
    details: str = ""


def _resultat(jeu: str, url: str, statut: Statut, details: str = "") -> dict:
    return Resultat(jeu=jeu, url=url, statut=statut, details=details).model_dump()


# ---------------------------------------------------------------------------
# Correspondance de champs (tolérante aux accents, casse, libellés français)
# ---------------------------------------------------------------------------

def _norm(texte: Any) -> str:
    sans_accents = "".join(
        c
        for c in unicodedata.normalize("NFKD", str(texte or ""))
        if not unicodedata.combining(c)
    )
    return re.sub(r"[^a-z0-9]+", " ", sans_accents.lower()).strip()


def _contient_mot(cle_norm: str, meule: str) -> bool:
    """Vrai si la clé apparaît en bord de mot (évite « nom » dans « prenom »)."""
    if not cle_norm or not meule:
        return False
    motif = "(?<![a-z0-9])" + re.escape(cle_norm) + "(?![a-z0-9])"
    return re.search(motif, meule) is not None


SYNONYMES: dict[str, list[str]] = {
    "nom": ["nom", "last name", "nom de famille", "lastname", "surname"],
    "prenom": ["prenom", "first name", "firstname"],
    "email": ["email", "e mail", "mail", "courriel", "adresse mail", "adresse email"],
    "telephone": ["telephone", "tel", "mobile", "portable", "phone", "gsm"],
    "adresse": ["adresse", "rue", "voie", "address", "adresse postale"],
    "code_postal": ["code postal", "cp", "postal code", "zipcode", "zip"],
    "ville": ["ville", "city", "commune"],
    "date_naissance": ["date de naissance", "naissance", "birthday", "date of birth", "dob"],
    "pseudo": ["pseudo", "surnom", "nickname"],
    "message": ["message", "commentaire", "reponse", "pourquoi", "en quelques mots"],
}

# Ordre de remplissage : l'email d'abord pour qu'une clé « adresse »
# ne s'empare pas d'un champ « adresse email » encore libre.
ORDRE_CHAMPS = [
    "email", "prenom", "nom", "date_naissance", "telephone",
    "adresse", "code_postal", "ville", "pseudo", "message",
]

SELECTEURS_CHAMPS = (
    "input[type='text'], input[type='search'], input[type='email'], "
    "input[type='tel'], input[type='date'], input[type='number'], "
    "input[type='url'], input:not([type]), textarea, select"
)

CAPTCHA_SELECTEURS = [
    "iframe[src*='recaptcha']",
    "iframe[src*='hcaptcha']",
    "iframe[src*='turnstile']",
    "iframe[src*='captcha']",
    "iframe[title*='captcha' i]",
    ".g-recaptcha",
    ".h-captcha",
    ".cf-turnstile",
    "[data-sitekey]",
]

MOTS_PAIEMENT = [
    "cc number", "cc num", "cvc", "cvv", "cryptogramme", "carte bancaire",
    "numero de carte", "iban", "bic",
]

MOTS_CONFIRMATION = [
    "merci", "participation", "enregistre", "confirm", "bravo", "bien recu",
    "croise", "vous avez joue", "tire au sort", "thank you",
]

MOTS_BOUTON = re.compile(
    r"particip|valide|valid|envoi|jou|tent|confirm|soumet|inscri|jeu|submit",
    re.IGNORECASE,
)


def _metadonnees(element) -> str:
    """Concatène tous les indices textuels d'un champ, normalisés."""
    morceaux: list[str] = []
    try:
        for attr in ("placeholder", "aria-label", "aria-placeholder", "title", "name", "id", "autocomplete"):
            valeur = element.get_attribute(attr)
            if valeur:
                morceaux.append(valeur)
    except Exception:
        pass
    try:
        libelle = element.evaluate(
            "e => {"
            "  var l = e.closest('label');"
            "  if (!l && e.id) { l = document.querySelector(\"label[for='\" + e.id + \"']\"); }"
            "  return l ? l.textContent : '';"
            "}"
        )
        if libelle:
            morceaux.append(libelle)
    except Exception:
        pass
    return _norm(" ".join(morceaux))


def _deja_utilise(element) -> bool:
    try:
        return element.get_attribute("data-pf") == "1"
    except Exception:
        return True


def _trouver_champ(page, cle: str):
    cles = list(SYNONYMES.get(cle, []))
    if cle not in cles:
        cles.append(cle)
    cles_norm = [_norm(c) for c in cles if _norm(c)]
    meilleur = None
    meilleur_score = 0
    try:
        elements = page.locator(SELECTEURS_CHAMPS).all()
    except Exception:
        return None
    for element in elements:
        if _deja_utilise(element):
            continue
        meta = _metadonnees(element)
        if not meta:
            continue
        score = 0
        for cn in cles_norm:
            if cn == meta:
                score = max(score, 100)
            elif _contient_mot(cn, meta):
                score = max(score, 60 + len(cn))
        if score > meilleur_score:
            meilleur = element
            meilleur_score = score
    if meilleur_score >= 60:
        return meilleur
    return None


def _remplir(page, cle: str, valeur: str) -> bool:
    element = _trouver_champ(page, cle)
    if element is None:
        return _choisir_option_radio(page, cle, valeur)
    try:
        balise = element.evaluate("e => e.tagName.toLowerCase()")
        if balise == "select":
            options = element.locator("option").all()
            vnorm = _norm(valeur)
            cible = None
            premiere = None
            for option in options:
                texte = option.inner_text()
                val = option.get_attribute("value")
                if val is None:
                    val = texte
                if premiere is None and _norm(val) not in ("", "?", "0") and not texte.startswith("--"):
                    premiere = val
                if (vnorm and vnorm in _norm(texte)) or (vnorm and vnorm in _norm(val)):
                    cible = val
                    break
            if cible is None:
                cible = premiere
            if cible is None:
                return False
            element.select_option(value=cible)
        else:
            element.fill(str(valeur))
        element.evaluate("e => e.setAttribute('data-pf', '1')")
        return True
    except Exception:
        return False


def _choisir_option_radio(page, cle: str, valeur: str) -> bool:
    vnorm = _norm(valeur)
    if not vnorm:
        return False
    cles = list(SYNONYMES.get(cle, [])) + [cle]
    cles_norm = [_norm(c) for c in cles if _norm(c)]
    try:
        radios = page.locator("input[type='radio']").all()
    except Exception:
        return False
    # 1) la clé identifie le groupe, la valeur identifie l'option.
    for radio in radios:
        if _deja_utilise(radio):
            continue
        meta = _metadonnees(radio)
        if any(_contient_mot(cn, meta) for cn in cles_norm) and any(
            vnorm == t for t in meta.split()
        ):
            radio.check()
            return True
    # 2) clé inconnue : l'option dont le libellé porte la valeur en mot entier.
    for radio in radios:
        if _deja_utilise(radio):
            continue
        meta = _metadonnees(radio)
        if any(vnorm == t for t in meta.split()):
            radio.check()
            return True
    return False


def _cocher_case(page, fragment: str) -> bool:
    fnorm = _norm(fragment)
    if not fnorm:
        return False
    try:
        cases = page.locator("input[type='checkbox']").all()
    except Exception:
        return False
    for case in cases:
        meta = _metadonnees(case)
        if fnorm in meta or _contient_mot(fnorm, meta):
            try:
                if not case.is_checked():
                    case.check()
                case.evaluate("e => e.setAttribute('data-pf', '1')")
                return True
            except Exception:
                return False
    return False


# ---------------------------------------------------------------------------
# Garde-fous : détection CAPTCHA et paiement
# ---------------------------------------------------------------------------

def _captcha_present(page) -> bool:
    for selecteur in CAPTCHA_SELECTEURS:
        try:
            if page.locator(selecteur).count() > 0:
                return True
        except Exception:
            continue
    return False


def _paiement_detecte(page) -> bool:
    try:
        elements = page.locator("input, select").all()
    except Exception:
        return False
    for element in elements:
        if any(m in _metadonnees(element) for m in MOTS_PAIEMENT):
            return True
    return False


# ---------------------------------------------------------------------------
# Envoi et vérification
# ---------------------------------------------------------------------------

def _cliquer_participer(page) -> bool:
    try:
        boutons = page.locator("button, input[type='submit'], input[type='button']").all()
    except Exception:
        return False
    for bouton in boutons:
        try:
            texte = (bouton.inner_text() or bouton.get_attribute("value") or "").strip()
        except Exception:
            texte = ""
        if texte and MOTS_BOUTON.search(texte):
            try:
                bouton.click()
                return True
            except Exception:
                continue
    # Dernier recours : le dernier bouton submit du formulaire.
    try:
        soumissions = page.locator("button[type='submit'], input[type='submit']").all()
        if soumissions:
            soumissions[-1].click()
            return True
    except Exception:
        pass
    return False


def _nos_champs_encore_presents(page) -> bool:
    try:
        return page.locator("[data-pf]").count() > 0
    except Exception:
        return False


def _verifier(page) -> tuple[str, str]:
    if _captcha_present(page):
        return "captcha", "CAPTCHA apparu après envoi — transfert humain."
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    try:
        contenu = page.locator("body").inner_text()
    except Exception:
        contenu = ""
    plat = " ".join(contenu.split())
    bas = _norm(plat)
    confirme = any(m in bas for m in MOTS_CONFIRMATION)
    nos_champs = _nos_champs_encore_presents(page)
    try:
        un_formulaire = page.locator("form").count() > 0
    except Exception:
        un_formulaire = False
    if confirme and not nos_champs:
        return "soumis", plat[:250]
    if not confirme and not nos_champs and un_formulaire:
        return (
            "champs_introuvables",
            "Formulaire multi-étapes détecté après envoi — transfert humain. " + plat[:150],
        )
    if confirme:
        return "a_verifier", "Confirmation probable mais formulaire toujours affiché. " + plat[:150]
    return (
        "a_verifier",
        "Envoi cliqué sans confirmation détectée. URL finale : " + str(page.url),
    )


# ---------------------------------------------------------------------------
# Cœur du workflow
# ---------------------------------------------------------------------------

def _soumettre_sync(entree: JeuInput) -> dict:
    jeu, url = entree.jeu, entree.url
    if entree.regles_interdisent_automatisation:
        return _resultat(
            jeu, url, "refus_reglement",
            "Clause anti-automatisation signalée par le Contradicteur — jeu ignoré.",
        )
    manquants: list[str] = []
    manquantes_cases: list[str] = []
    with sync_playwright() as p:
        navigateur = p.chromium.launch(headless=True)
        try:
            page = navigateur.new_page()
            page.set_default_timeout(15000)
            try:
                page.goto(url, timeout=45000, wait_until="domcontentloaded")
            except PlaywrightError as err:
                return _resultat(jeu, url, "erreur", ("Page injoignable : " + str(err))[:300])
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            if _captcha_present(page):
                return _resultat(
                    jeu, url, "captcha",
                    "CAPTCHA détecté avant remplissage — jamais contourné, transfert humain.",
                )
            if _paiement_detecte(page):
                return _resultat(
                    jeu, url, "paiement_detecte",
                    "Champ de paiement détecté sur la page — abandon, transfert humain.",
                )
            for cle in ORDRE_CHAMPS:
                if cle in entree.champs and not _remplir(page, cle, entree.champs[cle]):
                    manquants.append(cle)
            for cle, valeur in entree.champs.items():
                if cle not in ORDRE_CHAMPS and not _remplir(page, cle, valeur):
                    manquants.append(cle)
            for fragment in entree.cases_a_cocher:
                if not _cocher_case(page, fragment):
                    manquantes_cases.append(fragment)
            if manquants or manquantes_cases:
                detail = ""
                if manquants:
                    detail += "Champs introuvables : " + ", ".join(manquants) + ". "
                if manquantes_cases:
                    detail += "Cases introuvables : " + ", ".join(manquantes_cases) + ". "
                return _resultat(
                    jeu, url, "champs_introuvables",
                    (detail + "Aucun envoi effectué — kit manuel proposé.").strip(),
                )
            if not _cliquer_participer(page):
                return _resultat(
                    jeu, url, "champs_introuvables",
                    "Bouton de participation introuvable — aucun envoi, transfert humain.",
                )
            statut, details = _verifier(page)
            return _resultat(jeu, url, statut, details)
        except PlaywrightError as err:
            return _resultat(jeu, url, "erreur", ("Erreur Playwright : " + str(err))[:300])
        except Exception as err:  # filet de sécurité : le worker ne doit jamais planter
            return _resultat(jeu, url, "erreur", ("Erreur inattendue : " + str(err))[:300])
        finally:
            navigateur.close()


@workflows.activity()
async def soumettre_formulaire(entree: JeuInput) -> dict:
    """Remplit et soumet le formulaire dans un Chromium headless."""
    return await asyncio.to_thread(_soumettre_sync, entree)


@workflows.workflow.define(
    name="soumettre-formulaire",
    workflow_display_name="Soumettre un formulaire",
    workflow_description=(
        "Les mains de Pinkfloyd : ouvre la page d'un jeu-concours à formulaire "
        "web simple, remplit les champs et participe. Abandon immédiat et "
        "transfert humain sur CAPTCHA, paiement, clause anti-automatisation "
        "ou formulaire complexe."
    ),
)
class SoumettreFormulaire:
    @workflows.workflow.entrypoint
    async def run(self, input: JeuInput) -> dict:
        return await soumettre_formulaire(input)


async def main() -> None:
    print("Mains de Pinkfloyd : démarrage du worker...")
    await run_worker([SoumettreFormulaire])


if __name__ == "__main__":
    asyncio.run(main())
