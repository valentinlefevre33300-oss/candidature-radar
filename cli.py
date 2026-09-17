"""Interface en ligne de commande, pour scripter ou deboguer sans passer par le web.

Exemples :
    python cli.py secteurs
    python cli.py chercher --poste "developpeur python" --secteur tech --dept 33 \
                           --effectif 10:250 --limite 20
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from app import db
from app.models import SearchQuery
from app.naf import SECTORS, codes_for
from app.pipeline import run_search


def parse_headcount(raw: str | None) -> tuple[int | None, int | None]:
    """Analyse « 10:250 », « 10: », « :50 »."""
    if not raw:
        return None, None
    low, _, high = raw.partition(":")
    return (int(low) if low.strip() else None,
            int(high) if high.strip() else None)


async def command_search(args: argparse.Namespace) -> int:
    naf = codes_for(args.secteur) + list(args.naf or [])
    if not naf and not args.mots_cles:
        print("Precisez --secteur (voir « python cli.py secteurs ») ou --mots-cles.",
              file=sys.stderr)
        return 2

    min_h, max_h = parse_headcount(args.effectif)
    query = SearchQuery(
        job_title=args.poste,
        keywords=args.mots_cles or "",
        department=args.dept,
        postal_code=args.cp,
        naf_codes=list(dict.fromkeys(naf)),
        min_headcount=min_h,
        max_headcount=max_h,
        limit=args.limite,
    )

    db.init_db()
    run_id = db.start_run(args.poste, ",".join(args.secteur), args.dept, str(vars(args)))

    async def progress(event: dict) -> None:
        if event.get("event") == "etape":
            print(f"> {event['step']}")
        elif event.get("event") == "entreprise" and "contacts" in event:
            print(f"  {event['name'][:40]:42} {event['step']}")
        elif event.get("event") == "erreur":
            print(f"  ! {event['name']} : {event['detail']}", file=sys.stderr)

    companies, contacts = await run_search(
        query, use_search=not args.sans_moteur, verify_smtp=args.smtp, callback=progress)

    for company in companies:
        db.save_company(company)
    contacted = db.already_contacted()
    contacts = [c for c in contacts if c.email not in contacted]
    db.save_contacts(run_id, contacts)
    db.finish_run(run_id, len(companies), len(contacts))

    print(f"\n{len(contacts)} contact(s) — recherche #{run_id}\n")
    for contact in contacts[:args.afficher]:
        mark = " [deduite]" if contact.inferred else ""
        name = " ".join(filter(None, [contact.first_name, contact.last_name]))
        print(f"{contact.score:6.1f}  {contact.email:38} {contact.category:11} "
              f"{contact.company_name[:26]:28}{mark}")
        if name:
            print(f"        {name}{' — ' + contact.role_title[:50] if contact.role_title else ''}")

    if contacts:
        path = db.export_csv(run_id)
        print(f"\nCSV : {path}")
    return 0


def command_sectors(_args: argparse.Namespace) -> int:
    width = max(len(k) for k in SECTORS)
    for key, value in SECTORS.items():
        print(f"{key:{width}}  {value['label']}")
        print(f"{'':{width}}  {', '.join(value['codes'])}")
    return 0


def command_runs(_args: argparse.Namespace) -> int:
    db.init_db()
    for run in db.list_runs():
        print(f"#{run['id']:<4} {run['started_at'][:16]}  {run['status']:9} "
              f"{run['contacts'] or 0:>4} contacts  {run['job_title']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Candidature Radar")
    sub = parser.add_subparsers(dest="commande", required=True)

    search = sub.add_parser("chercher", help="lancer une recherche")
    search.add_argument("--poste", required=True, help="poste vise (sert au scoring)")
    search.add_argument("--secteur", action="append", default=[],
                        choices=sorted(SECTORS), help="repetable")
    search.add_argument("--naf", action="append", help="code NAF brut, repetable")
    search.add_argument("--mots-cles", dest="mots_cles",
                        help="mot present dans le NOM des entreprises visees")
    search.add_argument("--dept", help="departement, ex. 33")
    search.add_argument("--cp", help="code postal, ex. 33000")
    search.add_argument("--effectif", help="fourchette, ex. 10:250")
    search.add_argument("--limite", type=int, default=25)
    search.add_argument("--afficher", type=int, default=30)
    search.add_argument("--sans-moteur", action="store_true",
                        help="ne pas utiliser de moteur de recherche en repli")
    search.add_argument("--smtp", action="store_true",
                        help="sonder les adresses deduites en SMTP")
    search.set_defaults(func=lambda a: asyncio.run(command_search(a)))

    listing = sub.add_parser("secteurs", help="lister les secteurs disponibles")
    listing.set_defaults(func=command_sectors)

    history = sub.add_parser("historique", help="lister les recherches passees")
    history.set_defaults(func=command_runs)
    return parser


if __name__ == "__main__":
    parsed = build_parser().parse_args()
    raise SystemExit(parsed.func(parsed))
