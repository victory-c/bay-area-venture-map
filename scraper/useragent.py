"""Single source of truth for the outbound User-Agent string.

Both the SEC (https://www.sec.gov/os/accessing-edgar-data) and the Nominatim
usage policy require a descriptive User-Agent carrying a contact that reaches
a human.

What SEC actually enforces
--------------------------
Measured against live SEC hosts on 2026-08-22, same request, only the UA
varying:

    UA                                                  efts    www.sec.gov
    "Sand Hill VC Map admin@example.dev"                 200        200
    "Sand Hill VC Map (admin@example.dev) - research"    200        200
    "Sand Hill VC Map"                        (no contact)          403
    "Sand Hill VC Map https://github.com/victory-c/..."  403        403
    "Mozilla/5.0 ... Chrome/120 ..."          (browser)   -         403

Two hard rules fall out: the UA **must** carry an email-shaped contact, and
it must **not** contain a URL or bare domain. An earlier version of this
module defaulted the contact to the repository URL, which 403s every
SEC-backed enricher — caught by probing before a bulk run, not in review.

About the default
-----------------
``DEFAULT_CONTACT`` is a placeholder on an IANA-reserved domain: it satisfies
SEC's format check but cannot receive mail, so it does not really discharge
the obligation. It stays only so an unconfigured checkout keeps working
rather than 403ing, and importing this module warns when it is in use.
Choosing the real address is the repo owner's call, not this module's, and
baking a personal address into every outbound request is not a default worth
guessing at.

Set a contact you actually read before any sizeable scrape:

    SCRAPER_CONTACT=you@example.org uv run python -m scraper.form_d

In CI, set it from a repository variable so scheduled refreshes are
attributable.
"""
from __future__ import annotations

import logging
import os
import re

log = logging.getLogger(__name__)

_TOOL = "Sand Hill VC Map"
_PURPOSE = "Bay Area VC research tool"

#: Format-valid but unreachable. See "About the default" above.
DEFAULT_CONTACT = "sandhillmap@example.com"

#: A bare domain or any scheme-prefixed URL. Both trip SEC's 403.
_URL_LIKE = re.compile(r"(://|\b[a-z0-9-]+\.(com|org|net|io|dev|co|gov|edu)\b)", re.I)


def build_user_agent(contact: str | None = None) -> str:
    """Assemble the UA. ``contact`` overrides ``SCRAPER_CONTACT``."""
    resolved = (contact or os.environ.get("SCRAPER_CONTACT") or "").strip()
    if not resolved:
        log.warning(
            "SCRAPER_CONTACT is not set — falling back to the placeholder "
            "contact %r, which cannot receive mail. SEC and Nominatim both "
            "ask for a reachable address; set SCRAPER_CONTACT before running "
            "a sizeable scrape.",
            DEFAULT_CONTACT,
        )
        resolved = DEFAULT_CONTACT
    elif "@" not in resolved and _URL_LIKE.search(resolved):
        # Honour the operator's choice, but say why the run will 403.
        log.warning(
            "SCRAPER_CONTACT=%r is a URL. SEC rejects a User-Agent containing "
            "a domain (403 on both efts.sec.gov and www.sec.gov); use an email.",
            resolved,
        )
    return f"{_TOOL} ({resolved}) - {_PURPOSE}"


USER_AGENT = build_user_agent()
