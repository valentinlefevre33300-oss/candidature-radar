# -*- coding: utf-8 -*-
"""Test d'intégration réseau : crawl réel de quelques sites."""
import asyncio, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from bs4 import BeautifulSoup
from app.crawl.fetcher import PoliteFetcher, build_client
from app.crawl.spider import crawl_site
from app.extract.emails import extract_emails

SITES = sys.argv[1:] or ["scalian.com", "chapsvision.com", "octo.com"]

async def main():
    async with build_client() as client:
        f = PoliteFetcher(client, delay=1.0)
        for site in SITES:
            print(f"\n=== {site} ===")
            pages = await crawl_site(f, site, max_pages=8)
            print(f"  {len(pages)} pages recuperees")
            allmails = {}
            for url, html in pages:
                soup = BeautifulSoup(html, "lxml")
                got = extract_emails(soup, html)
                path = url.replace("https://", "")[:58]
                print(f"    - {path:60} -> {len(got)} email(s)")
                for e, obf in got.items():
                    allmails.setdefault(e, (obf, url))
            print(f"  TOTAL UNIQUE: {len(allmails)}")
            for e, (obf, url) in sorted(allmails.items()):
                print(f"      {e:38} {'[obfusque]' if obf else ''}")

asyncio.run(main())
