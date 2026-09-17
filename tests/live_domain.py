# -*- coding: utf-8 -*-
import asyncio, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from app.crawl.fetcher import PoliteFetcher, build_client
from app.models import SearchQuery
from app.resolve.domain import resolve_domain, candidate_domains
from app.sources.sirene import search_companies

async def main():
    q = SearchQuery(job_title="dev", naf_codes=["62.01Z","62.02A"], department="33",
                    min_headcount=10, max_headcount=250, limit=8)
    async with build_client() as client:
        f = PoliteFetcher(client, delay=0.8)
        companies = await search_companies(client, q)
        print(f"{len(companies)} entreprises a resoudre\n")
        for c in companies:
            cands = candidate_domains(c.name)[:3]
            c = await resolve_domain(f, client, c)
            flag = "OK " if c.domain else "-- "
            print(f"{flag}{c.name[:32]:32} -> {str(c.domain):28} "
                  f"conf={c.domain_confidence:.2f} via={c.domain_method}")
            print(f"     candidats testes: {cands}")
asyncio.run(main())
