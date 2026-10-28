# Job-board hostname research

Researched on 2026-10-19. The configuration is [jobboards.json](jobboards.json).
Values are hostnames without schemes or paths, matching the requested JSON format.
Provider keys follow the supplied names, with `lever` representing jobs.lever.co.

## Scope and interpretation

This is a researched list of provider-owned public job-listing hostname families,
including documented legacy hosts and verified redirects. It is not an inventory
of every employer's custom career domain or every third-party site to which these
services syndicate ads. Those sets change and cannot be exhaustively enumerated
from the providers' public documentation.

Entries containing `*` are hostname patterns, not literal URLs or DNS queries:

- `*.jobs.personio.de` and `*.jobs.personio.com` represent employer-specific
  hostnames such as `personio-gmbh.jobs.personio.de`.
- `*-jobs.personio.de` represents Personio's documented legacy naming convention,
  such as `company-jobs.personio.de`. It does not mean `company.jobs.personio.de`.
- `*.workable.com` represents employer-specific Workable subdomains. This is a
  broad hostname family that also contains non-listing services: use it with a
  listing path such as `/jobs/` or `/j/`, or inspect the page, rather than treating
  every Workable subdomain as a job advertisement. The explicit `apply`, `jobs`,
  and `careers` entries are retained for consumers that distinguish known hosts
  from employer-specific patterns.

A consumer must implement wildcard matching explicitly; JSON itself does not.
For these patterns, `*` stands for a non-empty part of a single hostname label.
Match against the parsed URL hostname, not arbitrary substrings in the full URL.

## Provider sources and observations

| Provider | Evidence and observations |
| --- | --- |
| Ashby | The [official public job-postings API documentation](https://developers.ashbyhq.com/docs/public-job-posting-api) identifies `jobs.ashbyhq.com/{board}` as the hosted job board. Employer-owned custom domains are also possible, so this one provider-owned host is not every Ashby-powered career site. |
| Greenhouse | [Official support documentation](https://support.greenhouse.io/hc/en-us/articles/360020776251-Job-board-URL-for-Greenhouse-hosted-job-board) documents `boards.greenhouse.io`. A [live EU job board](https://job-boards.eu.greenhouse.io/bitpanda) confirms the current EU hostname; `boards.eu.greenhouse.io/bitpanda` redirects to it. HTTPS header checks of `/greenhouse` confirmed that `job-boards.greenhouse.io`, `job-boards.us.greenhouse.io`, `boards.greenhouse.io`, and `boards.us.greenhouse.io` all redirect to Greenhouse's own career page. This establishes the US aliases as usable public entry points; it does not mean every board serves its HTML directly from each alias. |
| JOIN | The [live JOIN company board](https://join.com/companies/join) serves listings directly under the apex domain. `https://www.join.com/companies/join` returned a redirect to the same page. No separate `jobs.join.com` listing host was established. |
| Lever | Lever's [official postings API repository](https://github.com/lever/postings-api) documents both global and EU hosted job sites. Examples: [global](https://jobs.lever.co/lever) and [EU](https://jobs.eu.lever.co/lever). API hosts are deliberately excluded from the listing-host configuration. |
| Workable | [Official API/career-page documentation](https://help.workable.com/hc/en-us/articles/115012771647-Using-the-Workable-API-to-create-a-careers-page) documents `apply.workable.com` and employer-specific `{subdomain}.workable.com/jobs/...` URLs. The [official job-seeker guide](https://jobseekers.workable.com/hc/en-us/articles/5115854604695-Find-a-job-with-Workable) also documents `jobs.workable.com`; its root and `/view/` returned HTTP 404 during direct checks, so it is retained as a documented legacy host, not claimed to be currently functional. `careers.workable.com` returned a redirect to `https://apply.workable.com/careers/`; `https://apply.workable.com/workable/` returned HTTP 200. |
| Personio | [Official career-domain documentation](https://support.personio.de/hc/en-us/articles/4403382315153-Change-your-Career-Page-Domain-Name-Extension) specifies `{company}.jobs.personio.de` and `{company}.jobs.personio.com`. Both `https://personio-gmbh.jobs.personio.de/` and the `.com` equivalent returned HTTP 200. The [official administrator handbook](https://support.personio.de/hc/en-us/article_attachments/18118980989213) documents the older `{company}-jobs.personio.de` pattern; this is included for legacy links, without claiming every old tenant still works. |

`job.greenhouse.io`, used in the request as an example, did not resolve in a
direct HTTPS check and had no corroborating listing evidence. It was not added.
API, support, login-only, CDN, and tracking-only domains are outside this list.

## Adzuna country sites

Adzuna uses separate country domains, rather than one country subdomain family.
The following 19 public listing hosts were confirmed through indexed first-party
pages. [Adzuna's own country selector](https://www.adzuna.com/browse/accounting-finance-jobs/chartered-accountant)
lists these markets. The configuration includes their observed `www` listing
hostnames; it does not assume every bare-domain or other subdomain is a listing
host. Direct requests returned HTTP 403, so this research does not claim a
successful live content fetch for these sites.

| Market | First-party source / listing hostname |
| --- | --- |
| United States | [www.adzuna.com](https://www.adzuna.com/) |
| United Kingdom | [www.adzuna.co.uk](https://www.adzuna.co.uk/jobs/east-grinstead/flooring) |
| Australia | [www.adzuna.com.au](https://www.adzuna.com.au/) |
| Austria | [www.adzuna.at](https://www.adzuna.at/n%C3%BCziders) |
| Belgium | [www.adzuna.be](https://www.adzuna.be/niel/planner) |
| Brazil | [www.adzuna.com.br](https://www.adzuna.com.br/details/5435206982) |
| Canada | [www.adzuna.ca](https://www.adzuna.ca/) |
| France | [www.adzuna.fr](https://www.adzuna.fr/h%C3%A9ricourt-est/emplois-industrie-et-construction) |
| Germany | [www.adzuna.de](https://www.adzuna.de/) |
| India | [www.adzuna.in](https://www.adzuna.in/) |
| Italy | [www.adzuna.it](https://www.adzuna.it/) |
| Mexico | [www.adzuna.com.mx](https://www.adzuna.com.mx/) |
| Netherlands | [www.adzuna.nl](https://www.adzuna.nl/elsloo/logistiek) |
| New Zealand | [www.adzuna.co.nz](https://www.adzuna.co.nz/) |
| Poland | [www.adzuna.pl](https://www.adzuna.pl/) |
| Singapore | [www.adzuna.sg](https://www.adzuna.sg/) |
| South Africa | [www.adzuna.co.za](https://www.adzuna.co.za/) |
| Spain | [www.adzuna.es](https://www.adzuna.es/) |
| Switzerland | [www.adzuna.ch](https://www.adzuna.ch/) |

An [older Adzuna announcement](https://www.adzuna.com/blog/adzuna-is-live-across-20-countries-globally/)
mentions 20 markets. It was not used to invent a twentieth current hostname:
no current Russian listing site was established, and Russia is absent from the
observed country selector.
