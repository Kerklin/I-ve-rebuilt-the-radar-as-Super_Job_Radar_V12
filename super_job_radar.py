"""Super Job Radar V12 - finds vacancies that fit Damir's profile and publishes them as one web page.

One file, Python standard library only: no AI, no packages, no paid services.

  python super_job_radar.py              read all sources -> site/index.html, jobs.json, summary.md
  python super_job_radar.py --check      read all sources, print a source health table with repair hints (writes nothing)
  python super_job_radar.py --check WFP  same, only sources whose name contains "WFP"
  python super_job_radar.py --test       offline self-tests + test_cases.json (must be 100 %)
  python super_job_radar.py --why "Job title" ["description"]   explain one score
  python super_job_radar.py --audit      list obsolete GitHub actions / moving runner labels in .github/workflows
  python super_job_radar.py --install    create local .github/workflows copy + Claude Code settings
"""
import concurrent.futures as cf, datetime as dt, html, http.cookiejar, json, os, re, shutil, subprocess, sys, time
import urllib.error, urllib.parse, urllib.request, urllib.robotparser
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))


def load(name):
    return json.load(open(os.path.join(HERE, name), encoding="utf-8"))


PROFILE, SOURCES = load("profile.json"), load("sources.json")["sources"]
TODAY = dt.date.today()
USER_AGENT = "SuperJobRadar/12 (personal job search; 4 runs per day)"
MAX_BYTES = 40_000_000                      # UNICEF's RSS is larger than 3 MB
RELIEFWEB_APPNAME = os.environ.get("RELIEFWEB_APPNAME", "").strip()
BIH = re.compile(r"bosnia|herzegovina|\bbih\b|sarajevo|mostar|banja luka|tuzla|zenica|bihać|bihac|brčko|brcko", re.I)


# ============================================================ 1. SCORE: does the job fit the profile?
def word_pattern(words):
    """Whole words; a word ending in * matches word beginnings. ('intern' must not hit 'Internal'.)"""
    parts = sorted((re.escape(w[:-1]) + r"\w*" if w.endswith("*") else re.escape(w) + r"(?!\w)" for w in words),
                   key=len, reverse=True)
    return re.compile(r"(?<!\w)(" + "|".join(parts) + r")", re.I)


TRACKS = {name: word_pattern(words) for name, words in PROFILE["tracks"].items()}
HARD_NO, SOFT_NO = word_pattern(PROFILE["hard_negative"]), word_pattern(PROFILE["soft_negative"])
BONUSES = [(k, v, word_pattern([k])) for k, v in {**PROFILE["location_bonus"], **PROFILE["level_bonus"]}.items() if v]


def score(title, text=""):
    """-> (0-100, best track, reasons)."""
    weight, points, reasons, best, best_hits = PROFILE["title_weight"], 0, [], "-", 0
    for name, pattern in TRACKS.items():
        in_title = {m.lower() for m in pattern.findall(title)}
        in_text = {m.lower() for m in pattern.findall(text)} - in_title
        points += 15 * weight * len(in_title) + 10 * len(in_text)
        reasons += sorted(in_title) + sorted(in_text)
        if weight * len(in_title) + len(in_text) > best_hits:
            best, best_hits = name, weight * len(in_title) + len(in_text)
    s = min(points, 80)
    for word, value, pattern in BONUSES:
        if pattern.search(f"{title} {text}"):
            s += value; reasons.append("+" + word)
    if HARD_NO.search(title) or (HARD_NO.search(text) and not best_hits):
        s = 0; reasons.append("not my field")
    elif SOFT_NO.search(title):
        s //= 3; reasons.append("weak fit")
    return min(s, 100), best, reasons


# ============================================================ 2. ELIGIBILITY: can Damir apply?
INTERNAL_ONLY = re.compile(r"internal (candidates|applicants) only|only (open to )?internal (candidates|applicants)"
                           r"|tier\s*[0-2](\s*/\s*[0-2])*\s+(candidates\s+)?only", re.I)
NATIONALS_ONLY = re.compile(r"\b([A-Z][a-zA-Z]+(?:\s[A-Z][a-zA-Z]+)?)\s+nationals\s+only|"
                            r"only\s+(?:open\s+to\s+)?nationals\s+of\s+([A-Z][a-zA-Z]+(?:\s[A-Z][a-zA-Z]+)?)", re.I)
EU_ONLY = re.compile(r"\bEU(?:/EEA)?\s+(nationals|citizens)\s+only|nationals?\s+of\s+(an?\s+)?EU\s+member\s+state", re.I)
LOCAL_POST = re.compile(r"\b(NO-?[A-D]|NO[A-D]-?\d?|G-?[1-7]|GS-?[1-7]|NPSA|SB-?[1-5]|LICA|national officer|"
                        r"national (individual )?consultan\w*|locally recruited|local recruitment)\b", re.I)


def eligibility(title, text, deadline):
    """-> (OK | CHECK | NOT ELIGIBLE, reason). Only explicit blockers make a job NOT ELIGIBLE."""
    blob = f"{title} {text}"
    if INTERNAL_ONLY.search(blob):
        return "NOT ELIGIBLE", "internal candidates only"
    for m in NATIONALS_ONLY.finditer(blob):
        who = (m.group(1) or m.group(2) or "").strip()
        if not BIH.search(who) and not re.search(r"bosnian|herzegovinian", who, re.I):
            return "NOT ELIGIBLE", f"{who} nationals only"
    if EU_ONLY.search(blob):
        return "NOT ELIGIBLE", "EU nationals only"
    if LOCAL_POST.search(blob) and not BIH.search(blob):
        return "NOT ELIGIBLE", "local post outside BiH"
    if deadline and deadline < TODAY:
        return "NOT ELIGIBLE", "deadline passed"
    if not deadline:
        return "CHECK", "deadline not found - open the vacancy"
    return "OK", "no blocker found"


# ============================================================ 3. DATES
MONTHS = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}
DATE_FORMATS = [
    (r"(\d{4})-(\d{2})-(\d{2})", lambda m: (m[1], m[2], m[3])),                                   # 2026-11-30
    (r"(\d{1,2})[ -]([A-Za-z]{3})[a-z]*[ ,-]+(\d{4})", lambda m: (m[3], MONTHS.get(m[2][:3].lower()), m[1])),  # 30 Nov 2026
    (r"([A-Za-z]{3})[a-z]*\.? (\d{1,2}),? (\d{4})", lambda m: (m[3], MONTHS.get(m[1][:3].lower()), m[2])),     # Nov 30, 2026
    (r"(\d{1,2})/(\d{1,2})/(\d{4})", lambda m: (m[3], m[1], m[2])),                               # 11/30/2026 (US)
    (r"(\d{1,2})\.\s?(\d{1,2})\.\s?(\d{4})", lambda m: (m[3], m[2], m[1])),                       # 30. 11. 2026.
]
DATE_FORMATS = [(re.compile(p), f) for p, f in DATE_FORMATS]
DEADLINE_WORDS = re.compile(r"(deadline|closing date|closes|apply before|end date|application deadline|"
                            r"bewerbungsfrist|prijava do|rok za prijav\w*|rok prijave|konkurs traje do)\W{0,4}(.{0,40})", re.I)


def to_date(y, m, d):
    try:
        return dt.date(int(y), int(m), int(d))
    except (TypeError, ValueError):
        return None


def dates_in(text):
    return [d for pattern, parts in DATE_FORMATS for m in pattern.finditer(text or "") if (d := to_date(*parts(m)))]


def find_deadline(text, last_date_is_deadline=False):
    for m in DEADLINE_WORDS.finditer(text or ""):
        found = dates_in(m.group(2))
        if found:
            return found[0]
    if last_date_is_deadline:                  # Klix: "09.11. - 09.12.2026" -> the full date is the closing date
        found = dates_in(text)
        return max(found) if found else None
    return None


# ============================================================ 4. FETCH: polite web requests
_robots = {}


def robots_allow(url):
    base = "{0.scheme}://{0.netloc}".format(urllib.parse.urlsplit(url))
    if base not in _robots:
        rules = urllib.robotparser.RobotFileParser()
        try:
            req = urllib.request.Request(base + "/robots.txt", headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=15) as r:
                rules.parse(r.read(500_000).decode("utf-8", "replace").splitlines())
        except Exception:
            rules.parse([])
        _robots[base] = rules
    return _robots[base].can_fetch(USER_AGENT, url)


def fetch(url, accept="*/*", json_body=None, headers=None, session=None):
    """GET, or POST when json_body is given. Never bypasses robots.txt or anti-bot pages. One retry on 5xx/network."""
    if not robots_allow(url):
        raise PermissionError("robots.txt forbids automated reading")
    h = {"User-Agent": USER_AGENT, "Accept": accept, "Accept-Language": "en,bs;q=0.8", **(headers or {})}
    data = None
    if json_body is not None:
        data, h["Content-Type"] = json.dumps(json_body).encode("utf-8"), "application/json"
    opener = session or urllib.request.build_opener()
    for attempt in (1, 2):
        try:
            with opener.open(urllib.request.Request(url, data=data, headers=h), timeout=40) as r:
                body = r.read(MAX_BYTES + 1)
                if len(body) > MAX_BYTES:
                    raise ValueError("response larger than 40 MB")
                if not body.strip():
                    raise ConnectionRefusedError(f"HTTP {r.status} with an empty page - the site blocks automated readers")
                return body.decode(r.headers.get_content_charset() or "utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == 2:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
        time.sleep(5)


def new_session():
    """Keeps cookies between requests (Cornerstone/World Bank needs the cookie its home page sets)."""
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def plain(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def is_web_page(body):
    head = body.lstrip()[:500].lower()
    return head.startswith(("<!doctype html", "<html")) or "<body" in head


# ============================================================ 5. READERS: one per kind of career site
def read_feed(body):
    """RSS or Atom. Repairs bare '&' and HTML entities such as &nbsp; (UNOPS)."""
    if is_web_page(body):
        raise ValueError("got a web page instead of a feed")
    body = re.sub(r"&(?!(?:[a-zA-Z]+|#\d+|#x[0-9a-fA-F]+);)", "&amp;", body)
    body = re.sub(r"&([a-zA-Z]+);", lambda m: m[0] if m[1] in ("amp", "lt", "gt", "quot", "apos") else
                  ("".join(f"&#{ord(c)};" for c in html.unescape(m[0])) if html.unescape(m[0]) != m[0] else "&amp;" + m[1] + ";"), body)
    root = ET.fromstring(re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", body).lstrip("\ufeff \r\n\t").encode("utf-8"))
    jobs = []
    for item in root.iter():
        if item.tag.split("}")[-1] not in ("item", "entry"):
            continue
        f = {c.tag.split("}")[-1]: c for c in item}
        link = f.get("link")
        link = ((link.text or "").strip() or link.get("href", "")) if link is not None else ""
        desc = f.get("description", f.get("summary", f.get("content")))
        jobs.append({"title": plain(f["title"].text if "title" in f else ""), "link": link.strip(),
                     "text": plain(desc.text if desc is not None else "")[:4000]})
    return jobs


LINK = re.compile(r'<a\b[^>]*href=["\']([^"\'#]+)["\'][^>]*>(.*?)</a>', re.I | re.S)


def read_links(body, page_url, contains="", same_site_only=False, min_len=15):
    """Job links on a web page. Without 'contains', a job link needs a 3+ digit ID (menu links have none)."""
    host, jobs = urllib.parse.urlsplit(page_url).netloc, []
    for href, inner in LINK.findall(body):
        title, url = plain(inner), urllib.parse.urljoin(page_url, html.unescape(href))
        parts = urllib.parse.urlsplit(url)
        if len(title) < min_len or contains not in url or not url.startswith("http"):
            continue
        if same_site_only and parts.netloc != host:
            continue
        if not contains and not re.search(r"\d{3,}", parts.path + "?" + parts.query):
            continue
        jobs.append({"title": title[:200], "link": url, "text": title})
    return jobs


def reader_feed_or_page(src):
    errors = []
    for url in src["urls"]:
        try:
            body = fetch(url)
            jobs = read_links(body, url, src.get("job_link_contains", ""), src.get("same_site_only", False)) \
                if is_web_page(body) else read_feed(body)
            if jobs:
                return jobs, url
            errors.append(f"{url}: 0 vacancies recognised")
        except Exception as e:
            errors.append(f"{url}: {type(e).__name__}: {str(e)[:120]}")
    raise RuntimeError(" | ".join(errors))


def reader_oracle(src):
    """Oracle Recruiting Cloud (UNDP, UNFPA, UN Women, IOM, Save the Children): the site's public JSON."""
    api = f"https://{src['host']}/hcmRestApi/resources/latest/"
    jobs, offset = [], 0
    while offset < 1000:
        data = json.loads(fetch(api + "recruitingCEJobRequisitions?onlyData=true&expand=requisitionList.secondaryLocations"
                                f"&finder=findReqs;siteNumber={src['site']},limit=100,offset={offset},sortBy=POSTING_DATES_DESC",
                                "application/json"))
        rows = (data.get("items") or [{}])[0].get("requisitionList") or []
        for r in rows:
            jobs.append({"title": r.get("Title", ""), "id": str(r.get("Id", "")),
                         "link": f"https://{src['host']}/hcmUI/CandidateExperience/en/sites/{src['site']}/job/{r.get('Id')}/",
                         "text": plain(f"{r.get('PrimaryLocation', '')} {r.get('ShortDescriptionStr', '')}")})
        if len(rows) < 100:
            break
        offset += 100
    return jobs, src["host"]


def details_oracle(src, job):
    d = (json.loads(fetch(f"https://{src['host']}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
                          f"?expand=all&onlyData=true&finder=ById;Id=%22{job['id']}%22,siteNumber={src['site']}",
                          "application/json")).get("items") or [{}])[0]
    job["text"] = plain(" ".join(str(d.get(k) or "") for k in ("PrimaryLocation", "ExternalDescriptionStr",
                                                              "ExternalQualificationsStr", "ExternalResponsibilitiesStr")))[:6000]
    end = d.get("ExternalPostedEndDate") or ""
    job["deadline"] = dates_in(end[:10])[0] if dates_in(end[:10]) else None


def reader_workday(src):
    """Workday (WFP): the site's public JSON."""
    base = f"https://{src['host']}/wday/cxs/{src['tenant']}/{src['site']}"
    jobs, offset = [], 0
    while offset < 400:
        rows = json.loads(fetch(base + "/jobs", "application/json",
                                {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": ""})).get("jobPostings") or []
        for p in rows:
            jobs.append({"title": p.get("title", ""), "path": p.get("externalPath", ""),
                         "link": f"https://{src['host']}/en-US/{src['site']}{p.get('externalPath', '')}",
                         "text": plain(f"{p.get('locationsText', '')} {' '.join(p.get('bulletFields') or [])}")})
        if len(rows) < 20:
            break
        offset += 20
    return jobs, src["host"]


def details_workday(src, job):
    info = json.loads(fetch(f"https://{src['host']}/wday/cxs/{src['tenant']}/{src['site']}{job['path']}",
                            "application/json")).get("jobPostingInfo") or {}
    job["text"] = plain(f"{info.get('location', '')} {info.get('jobDescription', '')}")[:6000]
    end = info.get("endDate") or ""
    job["deadline"] = (dates_in(end[:10]) or [None])[0] if end else find_deadline(job["text"])


def reader_cornerstone(src):
    """Cornerstone (World Bank). V11 got HTTP 401 because the home page's session cookie was not sent back.
    V12 keeps cookies: open home page -> read its public token -> search with token AND cookie."""
    session = new_session()
    home = f"https://{src['host']}/ux/ats/careersite/{src['site']}/home?c={src['company']}"
    m = re.search(r'"token"\s*:\s*"([^"]+)"', fetch(home, "text/html", session=session))
    if not m:
        raise ValueError("token not found on the career home page - layout changed")
    body = {"careerSiteId": int(src["site"]), "careerSitePageId": int(src["site"]), "pageNumber": 1, "pageSize": 200,
            "cultureId": 1, "cultureName": "en-US", "searchText": "", "states": [], "countryCodes": [], "cities": [],
            "placeID": "", "radius": None, "postingsWithinDays": None, "customFieldCheckboxKeys": [],
            "customFieldDropdowns": [], "customFieldRadios": []}
    data = json.loads(fetch(f"https://{src['host']}/services/x/career-site/v1/search", "application/json", body,
                            {"Authorization": "Bearer " + m.group(1), "csod-accept-language": "en-US",
                             "Origin": f"https://{src['host']}", "Referer": home}, session))
    jobs = []
    for r in (data.get("data") or {}).get("requisitions") or []:
        where = "; ".join(f"{l.get('city') or ''} {l.get('country') or ''}".strip() for l in r.get("locations") or [])
        jobs.append({"title": r.get("displayJobTitle", ""), "text": where,
                     "link": f"https://{src['host']}/ux/ats/careersite/{src['site']}/home/requisition/{r.get('requisitionId')}?c={src['company']}"})
    return jobs, src["host"]


EIB_ROW = re.compile(r"Job Title (.+?) Job ID (\d+) Location (.+?) Job Function .+?Close Date "
                     r"(\d{2}/\d{2}/\d{4}|Open Until Filled)", re.S)


def reader_eib(src):
    """EIB: the result list is plain text on the page. EIB dates are DAY/MONTH/YEAR."""
    jobs = []
    for title, job_id, place, close in EIB_ROW.findall(plain(fetch(src["urls"][0], "text/html"))):
        d = to_date(close[6:], close[3:5], close[:2]) if close[0].isdigit() else None
        jobs.append({"title": title.strip(), "text": place, "deadline": d, "link": src["job_link"].format(id=job_id)})
    return jobs, "erecruitment.eib.org"


GIZ_ID = re.compile(r"ac=jobad(?:&amp;|&#0?38;|&)id=(\d+)", re.I)


def reader_giz(src):
    """GIZ (rexx portal). V11 found 0 links because job titles are not plain <a> texts.
    V12 collects every job ID anywhere in the list pages, then reads each job page (title + deadline)."""
    ids, errors = [], []
    for url in src["urls"]:
        try:
            ids += [i for i in GIZ_ID.findall(fetch(url, "text/html")) if i not in ids]
        except Exception as e:
            errors.append(f"{url}: {type(e).__name__}: {str(e)[:100]}")
    if not ids:
        raise RuntimeError("0 job IDs on the list pages (layout changed or built by JavaScript) " + " | ".join(errors))
    jobs = []
    for job_id in ids[:60]:
        url = src["job_page"].format(id=job_id)
        try:
            page = fetch(url, "text/html")
        except Exception:
            continue
        h1 = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S | re.I)
        title = plain(h1.group(1)) if h1 else plain((re.search(r"<title>(.*?)</title>", page, re.S | re.I) or [""])[0])
        title = re.sub(r"^(Stellenanzeige|Job offer|Job ad)\s*:\s*|\s*\|.*$", "", title).strip()
        text = plain(page)[:6000]
        jobs.append({"title": title, "link": url, "text": text, "deadline": find_deadline(text)})
    return jobs, f"{len(ids)} job IDs"


class NeedsSetup(Exception):
    pass


def reader_reliefweb(src):
    """ReliefWeb official API v2. The website/RSS answer robots with an empty page, so they are not used.
    Needs an approved appname (free) in the secret RELIEFWEB_APPNAME."""
    if not RELIEFWEB_APPNAME:
        raise NeedsSetup("add the repository secret RELIEFWEB_APPNAME (see README, section ReliefWeb)")
    request = {"limit": 200, "profile": "full", "sort": ["date.created:desc"],
               "fields": {"include": ["title", "url_alias", "url", "date.closing", "country.name", "source.name", "body"]}}
    if src.get("query"):
        request["query"] = {"value": src["query"], "operator": "OR"}
    if src.get("countries"):
        request["filter"] = {"field": "country.exact", "value": src["countries"], "operator": "OR"}
    url = "https://api.reliefweb.int/v2/jobs?appname=" + urllib.parse.quote(RELIEFWEB_APPNAME)
    jobs = []
    for row in json.loads(fetch(url, "application/json", request)).get("data", []):
        f = row.get("fields", {})
        closing = (f.get("date") or {}).get("closing", "")
        jobs.append({"title": f.get("title", ""), "link": f.get("url_alias") or f.get("url", ""),
                     "text": plain(" ".join(c.get("name", "") for c in (f.get("country") or []) + (f.get("source") or []))
                                   + " " + f.get("body", ""))[:5000],
                     "deadline": (dates_in(closing[:10]) or [None])[0] if closing else None})
    return jobs, "api.reliefweb.int/v2/jobs"


READERS = {"feed_or_page": reader_feed_or_page, "oracle": reader_oracle, "workday": reader_workday,
           "cornerstone": reader_cornerstone, "eib": reader_eib, "giz": reader_giz, "reliefweb": reader_reliefweb}
DETAILS = {"oracle": details_oracle, "workday": details_workday}


# ============================================================ 6. REPAIR HINTS for the source health table
def repair_hint(src, error):
    e = error.lower()
    if src["reader"] == "reliefweb" and ("403" in e or "appname" in e):
        return "Appname not approved yet or mistyped: wait for ReliefWeb's email, then check the secret value."
    if "robots.txt" in e:
        return "Site forbids automated reading. Do not bypass: keep it manual (\"off\": true)."
    if "empty page" in e or "202" in e:
        return "Anti-bot page. Do not bypass: use an official API/feed or keep it manual."
    if "401" in e:
        return "Login/token rejected. Retry next run; if it fails 3 runs in a row set \"off\": true and use Manual."
    if "403" in e:
        return "Site refuses automated readers from this network. If it repeats 3 runs in a row, set \"off\": true and use the Manual link."
    if "404" in e or "410" in e:
        return "Address changed. Open Manual, find the new list/feed address, update urls in sources.json."
    if "0 vacancies" in e or "0 job ids" in e or "token not found" in e:
        return "Page layout changed or needs JavaScript. Open Manual; update job_link_contains or set \"off\": true."
    if "web page instead of a feed" in e or "parseerror" in e:
        return "Feed address now returns a page. Put the list-page address first in urls."
    if "timed out" in e or "urlerror" in e or re.search(r"error 5\d\d", e):
        return "Temporary network/server problem. Repair only if it fails 3 runs in a row."
    return "Open Manual to confirm the site works in a browser, then send this line for repair."


# ============================================================ 7. RUN: read -> score -> eligibility -> remember -> publish
def norm(t):
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


def read_source(src):
    jobs, used = READERS[src["reader"]](src)
    matches = []
    for j in jobs:
        if not j.get("title") or not j.get("link"):
            continue
        s, track, reasons = score(j["title"], j["text"])
        if s < PROFILE["min_score"]:
            continue
        if src["reader"] in DETAILS and len(matches) < 40:     # full text + closing date, only for matches
            try:
                DETAILS[src["reader"]](src, j)
                s, track, reasons = score(j["title"], j["text"])
            except Exception:
                j["deadline"] = None
        deadline = j.get("deadline") or find_deadline(j["text"], src.get("last_date_is_deadline")) or find_deadline(j["title"])
        status, why = eligibility(j["title"], j["text"], deadline)
        label = src["name"]
        for org, marker in (src.get("tag") or {}).items():
            if marker.lower() in f"{j['title']} {j['text']}".lower():
                label = f"{src['name']} · {org}"
        matches.append({"title": j["title"][:200], "link": j["link"], "source": label, "origin": src["name"],
                        "score": s, "track": track, "reasons": reasons[:8], "deadline": deadline.isoformat() if deadline else "",
                        "status": status, "why": why, "snippet": j["text"][:300]})
    return len(jobs), used, matches


def read_all(only=""):
    chosen = [s for s in SOURCES if only.lower() in s["name"].lower() and not s.get("off")]
    results = {}
    with cf.ThreadPoolExecutor(8) as pool:
        futures = {pool.submit(read_source, s): s for s in chosen}
        for f in cf.as_completed(futures):
            src = futures[f]
            try:
                results[src["name"]] = ("OK", f.result(), "")
            except NeedsSetup as e:
                results[src["name"]] = ("SETUP", None, str(e))
            except Exception as e:
                msg = f"{type(e).__name__}: {str(e)[:300]}"
                results[src["name"]] = ("FAIL", None, msg.replace(RELIEFWEB_APPNAME, "***") if RELIEFWEB_APPNAME else msg)
    return chosen, results


def run():
    try:
        memory = json.load(open(os.path.join(HERE, "jobs.json"), encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        memory = {"jobs": [], "sources": {}}
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    chosen, results = read_all()
    seen_before = {j["link"]: j for j in memory["jobs"]}
    health, fresh, failed = {}, [], set()
    for src in chosen:
        status, result, error = results[src["name"]]
        before = memory["sources"].get(src["name"], {})
        row = {"status": status, "items": 0, "matches": 0, "last_ok": before.get("last_ok", "never"),
               "fails_in_a_row": 0, "detail": error, "fix": "", "manual": src.get("manual", "")}
        if status == "OK":
            n, used, matches = result
            fresh += matches
            row.update(items=n, matches=len(matches), last_ok=now, detail=used)
        else:
            row["fails_in_a_row"] = before.get("fails_in_a_row", 0) + 1
            row["fix"] = repair_hint(src, error) if status == "FAIL" else "See README > ReliefWeb (5 minutes, once)."
            if status == "FAIL":
                failed.add(src["name"])
        health[src["name"]] = row
    jobs, links, keys = [], set(), set()
    for j in sorted(fresh, key=lambda j: -j["score"]):
        key = norm(j["title"]) + "|" + j["deadline"]
        if j["link"] in links or (j["deadline"] and key in keys):
            continue                                            # same vacancy on two sites
        links.add(j["link"]); keys.add(key)
        j["first_seen"], j["old_data"] = seen_before.get(j["link"], {}).get("first_seen", now[:10]), False
        jobs.append(j)
    for j in memory["jobs"]:                                    # a failed source keeps its jobs up to 7 days
        age = (TODAY - dt.date.fromisoformat(j.get("first_seen", TODAY.isoformat()))).days
        if j.get("origin", j["source"]) in failed and j["link"] not in links and age <= 7 \
                and (not j["deadline"] or dt.date.fromisoformat(j["deadline"]) >= TODAY):
            j["old_data"] = True
            jobs.append(j)
    order = {"OK": 0, "CHECK": 1, "NOT ELIGIBLE": 2}
    jobs.sort(key=lambda j: (order[j["status"]], -j["score"], j["deadline"] or "9999"))
    state = {"updated": now, "jobs": jobs, "sources": health}
    json.dump(state, open(os.path.join(HERE, "jobs.json"), "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    write_web_page(state)
    write_summary(state)
    ok = sum(h["status"] == "OK" for h in health.values())
    print(f"{now}: {sum(j['status'] != 'NOT ELIGIBLE' for j in jobs)} vacancies to review · {ok}/{len(health)} sources OK")
    for name, h in sorted(health.items()):
        if h["status"] != "OK":
            print(f"  {h['status']:5} {name}: {h['fix']}")


def check(only=""):
    """Live source verification: reads sources, prints status + repair hint, writes nothing."""
    chosen, results = read_all(only)
    print(f"{'Source':22} {'Status':6} {'Items':>5} {'Match':>5}  Detail / repair hint")
    for src in chosen:
        status, result, error = results[src["name"]]
        if status == "OK":
            n, used, matches = result
            note = "OK but 0 items - check Manual" if n == 0 else used
            print(f"{src['name']:22} {status:6} {n:5} {len(matches):5}  {note[:90]}")
        else:
            print(f"{src['name']:22} {status:6} {0:5} {0:5}  {error[:90]}")
            print(f"{'':42}-> {repair_hint(src, error) if status == 'FAIL' else 'Add RELIEFWEB_APPNAME (README)'}")


# ============================================================ 8. OUTPUT: web page + run summary
def write_web_page(st):
    e = html.escape
    rows = []
    for j in st["jobs"]:
        days = (dt.date.fromisoformat(j["deadline"]) - TODAY).days if j["deadline"] else None
        new = j["first_seen"] >= (TODAY - dt.timedelta(days=1)).isoformat()
        cls = j["status"].replace(" ", "-")
        rows.append(f'<tr class="{cls}{" old" if j.get("old_data") else ""}"><td><b class="b {cls}">{j["status"]}</b>'
                    f'{" <i>NEW</i>" if new else ""}{" <i>old data</i>" if j.get("old_data") else ""}</td><td>{j["score"]}</td>'
                    f'<td><a href="{e(j["link"])}" target="_blank" rel="noopener">{e(j["title"])}</a><details><summary>why</summary>'
                    f'{e(j["why"])} · {e(", ".join(j["reasons"]))}<br>{e(j["snippet"])}</details></td><td>{e(j["source"])}</td>'
                    f'<td>{e(j["deadline"] or "?")}{f" ({days} d)" if days is not None else ""}</td><td>{e(j["first_seen"])}</td></tr>')

    def manual(h):
        return f'<a href="{e(h["manual"])}" target="_blank" rel="noopener">open</a>' if h["manual"] else ""
    health = "".join(f'<tr><td>{e(n)}</td><td class="{h["status"]}">{h["status"]}</td><td>{h["items"]}</td><td>{h["matches"]}</td>'
                     f'<td>{e(h["last_ok"])}</td><td>{e(h["detail"][:200])}<br><b>{e(h["fix"])}</b></td><td>{manual(h)}</td></tr>'
                     for n, h in sorted(st["sources"].items()))
    page = f"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Super Job Radar</title><style>body{{font:14px system-ui,sans-serif;margin:16px;color:#222}}table{{border-collapse:collapse;width:100%}}
td,th{{border-bottom:1px solid #ddd;padding:6px;text-align:left;vertical-align:top}}th{{background:#f3f3f3;position:sticky;top:0}}
.b{{padding:2px 6px;border-radius:4px;color:#fff;font-size:12px}}.b.OK{{background:#1a7f37}}.b.CHECK{{background:#bf8700}}.b.NOT-ELIGIBLE{{background:#888}}
tr.NOT-ELIGIBLE{{opacity:.55}}tr.old{{background:#fff8e1}}i{{color:#0969da;font-size:12px}}td.OK{{color:#1a7f37}}td.FAIL{{color:#cf222e;font-weight:bold}}
td.SETUP{{color:#bf8700;font-weight:bold}}input{{padding:6px;width:280px}}summary{{cursor:pointer;color:#666}}</style>
<h1>Super Job Radar</h1><p>Updated <b>{e(st["updated"])}</b> · every 6 hours · OK = no blocker found · CHECK = open the vacancy ·
NOT ELIGIBLE = explicit blocker (greyed). <b>Always confirm deadline and eligibility on the vacancy page.</b></p>
<p><input id="q" placeholder="filter: engineer, sarajevo, WFP..." oninput="f()"> <label><input type="checkbox" id="h" onchange="f()" checked style="width:auto"> hide NOT ELIGIBLE</label></p>
<table id="t"><tr><th>Status</th><th>Fit</th><th>Vacancy</th><th>Source</th><th>Deadline</th><th>Found</th></tr>
{"".join(rows) or "<tr><td colspan=6>No matching vacancies right now.</td></tr>"}</table>
<h2>Source health</h2><table><tr><th>Source</th><th>Status</th><th>Items</th><th>Matches</th><th>Last OK</th><th>Detail / how to fix</th><th>Manual</th></tr>{health}</table>
<script>function f(){{const q=document.getElementById('q').value.toLowerCase(),h=document.getElementById('h').checked;
document.querySelectorAll('#t tr:not(:first-child)').forEach(r=>{{r.style.display=(r.innerText.toLowerCase().includes(q)&&!(h&&r.classList.contains('NOT-ELIGIBLE')))?'':'none'}})}}f()</script></html>"""
    os.makedirs(os.path.join(HERE, "site"), exist_ok=True)
    open(os.path.join(HERE, "site", "index.html"), "w", encoding="utf-8").write(page)


def write_summary(st):
    good = [j for j in st["jobs"] if j["status"] != "NOT ELIGIBLE"]
    out = [f"# Super Job Radar - {st['updated']}", "", f"**{len(good)} vacancies to review**", "",
           "| Status | Fit | Vacancy | Source | Deadline |", "|---|---:|---|---|---|"]
    out += [f"| {j['status']} | {j['score']} | [{j['title'].replace('|', '/')}]({j['link']}) | {j['source']} | {j['deadline'] or '?'} |"
            for j in good[:60]]
    out += ["", "## Source health", "", "| Source | Status | Items | Matches | How to fix |", "|---|---|---:|---:|---|"]
    out += [f"| {n} | {h['status']} | {h['items']} | {h['matches']} | {(h['fix'] or '-').replace('|', '/')} |"
            for n, h in sorted(st["sources"].items())]
    open(os.path.join(HERE, "summary.md"), "w", encoding="utf-8").write("\n".join(out) + "\n")


# ============================================================ 9. AUDIT of GitHub workflow files
NODE24_FROM = {"actions/checkout": 5, "actions/setup-python": 6, "actions/setup-node": 5, "actions/cache": 5,
               "actions/upload-artifact": 6, "actions/download-artifact": 7, "actions/upload-pages-artifact": 5,
               "actions/deploy-pages": 5}


def audit(folder=None):
    folder = folder or os.path.join(HERE, ".github", "workflows")
    files = sorted(f for f in os.listdir(folder) if f.endswith((".yml", ".yaml"))) if os.path.isdir(folder) else []
    issues = []
    for f in files:
        for n, line in enumerate(open(os.path.join(folder, f), encoding="utf-8"), 1):
            code = line.split("#")[0]
            m = re.search(r"uses:\s*['\"]?([\w.-]+/[\w.-]+)@v?(\d+)", code)
            if m and m.group(1) in NODE24_FROM and int(m.group(2)) < NODE24_FROM[m.group(1)]:
                issues.append(f"{f}:{n}: {m.group(1)}@v{m.group(2)} runs on Node 20 -> use v{NODE24_FROM[m.group(1)]}+")
            if "ubuntu-latest" in code and "runs-on" in code:
                issues.append(f"{f}:{n}: ubuntu-latest moves to Ubuntu 26 -> use ubuntu-24.04")
            if "ACTIONS_ALLOW_USE_UNSECURE_NODE_VERSION" in code:
                issues.append(f"{f}:{n}: Node 20 opt-out no longer works -> delete it")
        if f != "super_job_radar.yml":
            issues.append(f"{f}: extra workflow file - delete it unless you need it")
    print(f"Workflow audit: {len(files)} file(s): {', '.join(files) or 'none'}")
    print("\n".join("  " + i for i in issues) if issues else "  clean: no obsolete actions, no moving runner labels")
    return issues


# ============================================================ 10. TESTS (offline, run before every collection)
def test():
    import contextlib, io, tempfile
    fails, count = [], [0]

    def expect(name, ok):
        count[0] += 1
        if not ok:
            fails.append(name)

    j = read_feed('<rss><channel><item><title>Civil Engineer & Site Supervisor</title><link>https://x/1</link>'
                  '<description>Deadline: Nov 30, 2026 &nbsp;</description></item></channel></rss>')
    expect("feed with bare & and &nbsp;", j and j[0]["title"] == "Civil Engineer & Site Supervisor")
    expect("deadline in feed", find_deadline(j[0]["text"]) == dt.date(2026, 11, 30))
    expect("atom", read_feed('<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>P</title><link href="https://y/1"/></entry></feed>')[0]["link"] == "https://y/1")
    try:
        read_feed("<!DOCTYPE html><html><body>x</body></html>"); expect("web page is not a feed", False)
    except ValueError:
        pass
    menu = '<a href="/kategorija/gradjevinarstvo">Građevinarstvo i geodezija</a><a href="/oglas/inzenjer-12345">Diplomirani inženjer građevine</a>'
    expect("menu links ignored", [x["title"] for x in read_links(menu, "https://k.ba/oglasi", "", True)] == ["Diplomirani inženjer građevine"])
    expect("klix last date", find_deadline("Sarajevo 09.11. – 09.12.2026", True) == dt.date(2026, 12, 9))
    for text, d in [("Apply Before 11/18/2026", (2026, 11, 18)), ("End date: 19-Nov-2026", (2026, 11, 19)),
                    ("Closing date: 30 November 2026", (2026, 11, 30)), ("deadline 2026-12-01", (2026, 12, 1)),
                    ("Prijava do 21. 11. 2026.", (2026, 11, 21)), ("Bewerbungsfrist: 29.11.2026", (2026, 11, 29)),
                    ("DEADLINE FOR APPLICATIONS 15 November 2026-23:59", (2026, 11, 15))]:
        expect("date " + text, find_deadline(text) == dt.date(*d))
    soon = TODAY + dt.timedelta(days=10)
    for t, x, want in [("Senior Project Manager", "Internal candidates only", "NOT ELIGIBLE"),
                       ("Logistics Officer (Ukrainian Nationals only)", "", "NOT ELIGIBLE"),
                       ("Engineer (Bosnian nationals only)", "Sarajevo", "OK"),
                       ("National Project Officer (NOB)", "Conakry, Guinea", "NOT ELIGIBLE"),
                       ("National Consultant engineer", "Sarajevo, Bosnia and Herzegovina", "OK"),
                       ("Construction Specialist P-3", "open to internal and external candidates", "OK")]:
        expect("eligibility " + t, eligibility(t, x, soon)[0] == want)
    expect("no deadline -> CHECK", eligibility("Engineer", "", None)[0] == "CHECK")
    expect("past deadline", eligibility("Engineer", "", TODAY - dt.timedelta(days=1))[0] == "NOT ELIGIBLE")
    expect("'intern' is not 'internal'", "weak fit" not in score("Civil Engineer - internal candidates")[2])
    expect("'bih' is a whole word", "+bih" not in score("Civil Engineer", "smoking prohibited")[2])
    rows = EIB_ROW.findall("Job Title Water Specialist Job ID 111775 Location LU Job Function M Posted Date 23/10/2026 "
                           "Close Date 16/11/2026 Job Title X Job ID 111803 Location LU Job Function M Close Date Open Until Filled")
    expect("EIB list", len(rows) == 2 and rows[0][1] == "111775" and rows[0][3] == "16/11/2026")
    expect("GIZ ids in any markup", GIZ_ID.findall('href="index.php?ac=jobad&amp;id=79519" data-u="?ac=jobad&id=79520"') == ["79519", "79520"])
    expect("repair hint 401", "3 runs" in repair_hint({"reader": "cornerstone"}, "HTTPError: HTTP Error 401"))
    expect("repair hint 503", "Temporary" in repair_hint({"reader": "oracle"}, "HTTPError: HTTP Error 503: x"))
    expect("repair hint reliefweb 403", "approved" in repair_hint({"reader": "reliefweb"}, "HTTPError: HTTP Error 403"))
    with tempfile.TemporaryDirectory() as d:
        open(os.path.join(d, "super_job_radar.yml"), "w").write("    runs-on: ubuntu-24.04\n      - uses: actions/deploy-pages@v5\n")
        open(os.path.join(d, "old.yml"), "w").write("    runs-on: ubuntu-latest\n      - uses: actions/upload-artifact@v4\n")
        with contextlib.redirect_stdout(io.StringIO()):
            found = audit(d)
        expect("audit finds the 3 old-file problems", len(found) == 3 and all(i.startswith("old.yml") for i in found))
    names = [s["name"] for s in SOURCES]
    expect("source names unique", len(names) == len(set(names)))
    expect("every source has a reader + manual link", all(s["reader"] in READERS and s.get("manual") for s in SOURCES))
    expect("no forbidden sources (UNjobs, ReliefWeb RSS)", not any(("unjobs.org" in json.dumps(s) or "reliefweb.int/jobs/rss" in json.dumps(s)) for s in SOURCES))
    cases = load("test_cases.json")["cases"]
    wrong = [c["title"] for c in cases if (score(c["title"], c["text"])[0] >= PROFILE["min_score"]) != c["show"]]
    print(f"Self-tests {count[0] - len(fails)}/{count[0]} · Test cases {len(cases) - len(wrong)}/{len(cases)}")
    for x in fails + wrong:
        print("  FAIL", x)
    sys.exit(1 if fails or wrong else 0)


# ============================================================ 11. Claude Code helpers (optional)
CLAUDE_SETTINGS = {
    "permissions": {"allow": ["Read", "Bash(python super_job_radar.py:*)", "Bash(git status:*)", "Bash(git diff:*)",
                              "Bash(git log:*)", "Bash(git add:*)", "Bash(git commit:*)", "Bash(git pull:*)"],
                    "ask": ["Bash(git push:*)", "Edit(super_job_radar.yml)", "Edit(.github/**)", "Edit(test_cases.json)"],
                    "deny": ["Read(./.env)", "Read(./.env.*)", "Read(**/*.key)", "Bash(git push --force:*)",
                             "Bash(git push -f:*)", "Bash(rm -rf:*)"]},
    "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "python super_job_radar.py --test || exit 2"}]}]}}


def install():
    os.makedirs(os.path.join(HERE, ".github", "workflows"), exist_ok=True)
    shutil.copy(os.path.join(HERE, "super_job_radar.yml"), os.path.join(HERE, ".github", "workflows", "super_job_radar.yml"))
    os.makedirs(os.path.join(HERE, ".claude"), exist_ok=True)
    json.dump(CLAUDE_SETTINGS, open(os.path.join(HERE, ".claude", "settings.json"), "w"), indent=2)
    print("Wrote .github/workflows/super_job_radar.yml and .claude/settings.json")


if __name__ == "__main__":
    a = sys.argv[1:] + ["", "", ""]
    commands = {"--test": test, "--check": lambda: check(a[1]), "--why": lambda: print(score(a[1], a[2])),
                "--audit": lambda: sys.exit(1 if audit() else 0), "--install": install}
    commands.get(a[0], run)()
