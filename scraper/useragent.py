"""Single source of truth for the outbound User-Agent string.

Both the SEC (https://www.sec.gov/os/accessing-edgar-data) and the Nominatim
usage policy require a descriptive User-Agent carrying a contact that actually
reaches a human. The previous per-module literal used
``sandhillmap@example.com`` — ``example.com`` is an IANA-reserved domain that
can never receive mail, so it satisfied the letter of the rule and none of its
purpose. Nominatim in particular blocks on this.

The default contact is the public repository, whose issue tracker is a real,
monitored channel and — unlike a personal address — is safe to broadcast in a
header. Override it with ``SCRAPER_CONTACT`` (an email or URL) when running a
fork or a high-volume job, so the upstream operator can reach whoever is
actually making the requests.
"""
from __future__ import annotations

import os

DEFAULT_CONTACT = "https://github.com/victory-c/sand-hill-vcs"

#: Descriptive UA: tool name, purpose, reachable contact.
USER_AGENT = (
    f"Sand Hill VC Map ({os.environ.get('SCRAPER_CONTACT') or DEFAULT_CONTACT}) "
    "- Bay Area VC research tool"
)
