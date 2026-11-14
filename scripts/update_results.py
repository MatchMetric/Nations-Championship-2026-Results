#!/usr/bin/env python3
"""
Nations Championship 2026 - automatic results updater.

Runs from a scheduled GitHub Action. For every fixture in results.json that
kicked off more than GRACE_MINUTES ago and still has no score, it asks Claude
(with the web search tool) for the final score and try counts.

Safety rules - a result is only written when ALL of these hold:
  * PASSES independent Claude runs return exactly the same score and tries
  * each run backs the result with >= 2 different websites that actually
    appeared in its own search results (guards against made-up sources)
  * the numbers are arithmetically possible in rugby union
    (score >= 5 x tries, no impossible leftover points, etc.)
  * the fixture was still empty - existing scores are NEVER overwritten,
    so hand corrections in results.json always win

Anything that fails is left as null and retried on the next scheduled run.

Environment variables
  ANTHROPIC_API_KEY  required (GitHub repo secret)
  CLAUDE_MODEL       default claude-sonnet-5-5
  PASSES             independent runs that must agree (default 2)
  GRACE_MINUTES      minutes after kick-off before looking (default 135)
  MAX_SEARCHES       web searches allowed per API call (default 8)
  BATCH_SIZE         matches looked up per API call (default 4)
  WRITE_REPORTS      1 = also store a one-line match report (default 1)
  DRY_RUN            1 = print what would change, don't write the file
  RESULTS_PATH       default results.json
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

RESULTS_PATH = Path(os.environ.get("RESULTS_PATH", "results.json"))
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")
PASSES = max(1, int(os.environ.get("PASSES", "2")))
GRACE_MINUTES = int(os.environ.get("GRACE_MINUTES", "135"))
MAX_SEARCHES = int(os.environ.get("MAX_SEARCHES", "8"))
BATCH_SIZE = max(1, int(os.environ.get("BATCH_SIZE", "4")))
WRITE_REPORTS = os.environ.get("WRITE_REPORTS", "1") == "1"
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"

FIELD_ORDER = ["id", "round", "date", "ko_utc", "match", "stage", "place", "north", "south",
               "a", "b", "ta", "tb", "report"]

NORTH = ["Ireland", "France", "England", "Scotland", "Wales", "Italy"]
SOUTH = ["South Africa", "New Zealand", "Argentina", "Australia", "Fiji", "Japan"]
# World Rugby ranking points (9 Nov 2026) - last-resort tie-breaker, same as the app
WR_PTS = {"South Africa": 93.09, "New Zealand": 92.65, "Ireland": 88.70, "France": 87.49, "England": 86.99,
          "Scotland": 84.40, "Australia": 83.12, "Argentina": 81.62, "Fiji": 77.79, "Wales": 77.37,
          "Italy": 76.30, "Japan": 75.25}
PLACE_NAMES = {1: "Grand Final", 2: "3rd-place play-off", 3: "5th-place play-off",
               4: "7th-place play-off", 5: "9th-place play-off", 6: "11th-place play-off"}

SYSTEM_PROMPT = """You are a meticulous rugby union results checker for the 2026 Nations Championship \
(the World Rugby competition between the Six Nations sides and the SANZAAR sides plus Fiji and Japan).

Use web search to find the OFFICIAL FINAL result of each match you are given. Rules:
- Only mark a match "final" if the full-time score appears in at least two independent, reputable \
sources (e.g. nationschampionshiprugby.com, world.rugby, national union sites, BBC, ESPN, RugbyPass, \
Rugby365, Planet Rugby, Wikipedia, major newspapers or wire services).
- Team order matters: "a" and "ta" are for the FIRST team listed (the home team), "b" and "tb" for the second.
- "ta"/"tb" are tries scored, counting penalty tries as tries. Do not count conversions or penalties.
- Never guess or estimate. If a match has not finished, was postponed/abandoned, or the score or try \
counts cannot be confirmed, return status "not_found" (and say why in "note").
- "report" is ONE factual sentence in your own words (max 30 words), e.g. the decisive moments or \
standout try scorers. Do not copy sentences from articles.
- "sources" lists the URLs you actually relied on (at least two different websites).

- FINALS WEEKEND matches (marked FINALS) are at Allianz Stadium, Twickenham, between the teams that \
finished in the same position in the Northern and Southern conference tables. For these also return \
"north_team" and "south_team" (Northern conference: Ireland, France, England, Scotland, Wales, Italy; \
Southern: South Africa, New Zealand, Argentina, Australia, Fiji, Japan), and give "a"/"ta" for the \
NORTHERN team and "b"/"tb" for the SOUTHERN team, whichever way round sources list them. Scores include \
any extra time.

Finish your reply with a single fenced ```json block, and nothing after it, in exactly this shape:
{"results": [{"id": 19, "status": "final", "a": 0, "b": 0, "ta": 0, "tb": 0,
  "report": "...", "sources": ["https://...", "https://..."], "note": ""}]}
(FINALS entries also carry "north_team" and "south_team".) Include every match id you were given, exactly once."""


# ----------------------------------------------------------------------------
# results.json I/O (keeps one fixture per line so git diffs stay readable)
# ----------------------------------------------------------------------------

def load_results(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _ordered(entry: dict) -> dict:
    out = {k: entry[k] for k in FIELD_ORDER if k in entry}
    out.update({k: v for k, v in entry.items() if k not in out})
    return out


def dump_results(data: dict) -> str:
    head = {k: v for k, v in data.items() if k != "results"}
    lines = ["{"]
    for k, v in head.items():
        lines.append(f"  {json.dumps(k)}: {json.dumps(v, ensure_ascii=False)},")
    lines.append('  "results": [')
    rows = [json.dumps(_ordered(r), ensure_ascii=False) for r in data["results"]]
    for i, row in enumerate(rows):
        lines.append("    " + row + ("," if i < len(rows) - 1 else ""))
    lines.append("  ]")
    lines.append("}")
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------------------
# Which fixtures need a result?
# ----------------------------------------------------------------------------

def kickoff(entry: dict) -> dt.datetime:
    ko = entry.get("ko_utc")
    if ko:
        return dt.datetime.strptime(ko, "%Y-%m-%dT%H:%MZ").replace(tzinfo=dt.timezone.utc)
    # No kick-off time recorded: assume a late kick-off on the listed date (conservative)
    d = dt.date.fromisoformat(entry["date"])
    return dt.datetime(d.year, d.month, d.day, 22, 0, tzinfo=dt.timezone.utc)


def is_empty(entry: dict) -> bool:
    return entry.get("a") in (None, "") or entry.get("b") in (None, "")


def due_fixtures(data: dict, now: dt.datetime) -> list[dict]:
    grace = dt.timedelta(minutes=GRACE_MINUTES)
    return [r for r in data["results"] if is_empty(r) and kickoff(r) + grace <= now]


# ----------------------------------------------------------------------------
# Validation
# ----------------------------------------------------------------------------

def domain(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return ""
    host = host.split(":")[0]
    return host[4:] if host.startswith("www.") else host


def score_is_possible(points: int, tries: int) -> bool:
    """Can a side score `points` with exactly `tries` tries (5 pts each, or 7 for a penalty try)?
    Leftover points come from conversions (2, at most one per try) and penalties/drop goals (3)."""
    left = points - 5 * tries
    if left < 0:
        return False
    max_conv = tries
    for conv in range(0, max_conv + 1):
        rest = left - 2 * conv
        if rest >= 0 and rest % 3 == 0:
            return True
    return False


def as_int(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.strip().isdigit():
        return int(v.strip())
    return None


def is_finals(entry: dict) -> bool:
    return entry.get("stage") == "finals" or int(entry.get("id", 0)) > 36


def team_name(value, allowed: list[str]) -> str | None:
    if not isinstance(value, str):
        return None
    v = " ".join(value.split()).lower()
    for t in allowed:
        if v == t.lower():
            return t
    return None


def conference_tables(data: dict) -> tuple[list[str], list[str]] | None:
    """Final group tables (same rules/tie-breakers as the app) once all 36 group results are in."""
    group = [r for r in data["results"] if int(r["id"]) <= 36]
    if len(group) < 36 or any(is_empty(r) for r in group):
        return None
    s = {t: {"pts": 0, "w": 0, "pd": 0, "tf": 0} for t in NORTH + SOUTH}
    for r in group:
        h, a = r["match"].split(" v ")
        pa, pb, ta, tb = int(r["a"]), int(r["b"]), int(r.get("ta") or 0), int(r.get("tb") or 0)
        x, y = s[h], s[a]
        x["pd"] += pa - pb; y["pd"] += pb - pa; x["tf"] += ta; y["tf"] += tb
        if pa > pb:
            x["pts"] += 4; x["w"] += 1; y["pts"] += 1 if pa - pb <= 7 else 0
        elif pb > pa:
            y["pts"] += 4; y["w"] += 1; x["pts"] += 1 if pb - pa <= 7 else 0
        else:
            x["pts"] += 2; y["pts"] += 2
        x["pts"] += 1 if ta >= 4 else 0
        y["pts"] += 1 if tb >= 4 else 0
    key = lambda t: (-s[t]["pts"], -s[t]["w"], -s[t]["pd"], -s[t]["tf"], -WR_PTS[t])
    return sorted(NORTH, key=key), sorted(SOUTH, key=key)


def validate(item: dict, searched_domains: set[str], entry: dict | None = None) -> tuple[dict | None, str]:
    if str(item.get("status", "")).lower() != "final":
        return None, f"not final ({item.get('note') or item.get('status')})"
    teams = {}
    if entry is not None and is_finals(entry):
        n, s = team_name(item.get("north_team"), NORTH), team_name(item.get("south_team"), SOUTH)
        if not n or not s:
            return None, f"finals teams not recognised ({item.get('north_team')} v {item.get('south_team')})"
        teams = {"north": n, "south": s}
    a, b, ta, tb = (as_int(item.get(k)) for k in ("a", "b", "ta", "tb"))
    if None in (a, b):
        return None, "missing score"
    if None in (ta, tb):
        return None, "missing try counts"
    if not (0 <= a <= 150 and 0 <= b <= 150 and 0 <= ta <= 25 and 0 <= tb <= 25):
        return None, f"numbers out of range {a}-{b} ({ta}-{tb} tries)"
    if not score_is_possible(a, ta) or not score_is_possible(b, tb):
        return None, f"impossible score/try combination {a}-{b} ({ta}-{tb} tries)"
    sources = [s for s in item.get("sources") or [] if isinstance(s, str)]
    verified = {domain(s) for s in sources if domain(s) in searched_domains}
    verified.discard("")
    if len(verified) < 2:
        return None, f"fewer than 2 verified source sites ({', '.join(sorted(verified)) or 'none'})"
    report = (item.get("report") or "").strip().replace("\n", " ")
    if len(report) > 300:
        report = report[:297].rstrip() + "..."
    return {"a": a, "b": b, "ta": ta, "tb": tb, "report": report, "sites": sorted(verified), **teams}, "ok"


def extract_json(text: str) -> dict | None:
    blocks = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S)
    candidates = list(reversed(blocks))
    if not candidates:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            candidates = [text[start:end + 1]]
    for c in candidates:
        try:
            obj = json.loads(c)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("results"), list):
            return obj
    return None


# ----------------------------------------------------------------------------
# Claude
# ----------------------------------------------------------------------------

def build_prompt(fixtures: list[dict], tables: tuple[list[str], list[str]] | None = None) -> str:
    lines = [
        "Find the final results of these 2026 Nations Championship matches "
        "(home team listed first; kick-off times are UTC):",
        "",
    ]
    for f in fixtures:
        ko = f.get("ko_utc") or f["date"]
        if is_finals(f):
            p = int(f.get("place") or 0)
            exp = ""
            if tables and 1 <= p <= 6:
                exp = (f" Our own table calculation expects {tables[0][p - 1]} (North) v {tables[1][p - 1]} (South)"
                       " - report the teams that actually played.")
            lines.append(f"- id {f['id']}: FINALS - Nations Championship {PLACE_NAMES.get(p, 'final')} "
                         f"(Northern conference #{p} v Southern conference #{p}), Allianz Stadium, Twickenham, "
                         f"kick-off {ko}.{exp}")
        else:
            lines.append(f"- id {f['id']}: {f['match']} - round {f.get('round')}, kick-off {ko}")
    lines += ["", f"Today is {dt.datetime.now(dt.timezone.utc):%d %B %Y} (UTC)."]
    return "\n".join(lines)


def ask_claude(client, fixtures: list[dict], tables=None) -> tuple[dict | None, set[str], dict]:
    """One independent research run. Returns (parsed json, searched domains, usage)."""
    messages = [{"role": "user", "content": build_prompt(fixtures, tables)}]
    tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": MAX_SEARCHES}]
    searched: set[str] = set()
    usage = {"input_tokens": 0, "output_tokens": 0, "web_searches": 0}
    text_parts: list[str] = []

    for _ in range(6):  # allow a few pause_turn continuations
        resp = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=messages,
            tools=tools,
        )
        u = resp.usage
        usage["input_tokens"] += getattr(u, "input_tokens", 0) or 0
        usage["output_tokens"] += getattr(u, "output_tokens", 0) or 0
        stu = getattr(u, "server_tool_use", None)
        usage["web_searches"] += (getattr(stu, "web_search_requests", 0) or 0) if stu else 0

        for block in resp.content:
            btype = getattr(block, "type", "")
            if btype == "web_search_tool_result":
                content = getattr(block, "content", None)
                if isinstance(content, list):
                    for res in content:
                        url = getattr(res, "url", None)
                        if url:
                            searched.add(domain(url))
            elif btype == "text":
                text_parts.append(block.text)
                for cit in getattr(block, "citations", None) or []:
                    url = getattr(cit, "url", None)
                    if url:
                        searched.add(domain(url))

        if resp.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": resp.content})
            continue
        break

    searched.discard("")
    return extract_json("\n".join(text_parts)), searched, usage


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def summary(lines: list[str]) -> None:
    text = "\n".join(lines)
    print(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")


def run(client=None, now: dt.datetime | None = None) -> int:
    now = now or dt.datetime.now(dt.timezone.utc)
    data = load_results(RESULTS_PATH)
    due = due_fixtures(data, now)
    if not due:
        summary(["### Nations Championship results", "Nothing due - no API call made."])
        return 0

    if client is None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            summary(["### Nations Championship results",
                     "ANTHROPIC_API_KEY is not set - add it under Settings > Secrets and variables > Actions."])
            return 1
        import anthropic  # imported here so offline runs/tests don't need the package
        client = anthropic.Anthropic()

    by_id = {f["id"]: f for f in due}
    # verdicts[fid] = one (validated result | None, reason) per pass
    verdicts: dict[int, list[tuple[dict | None, str]]] = {fid: [] for fid in by_id}
    total = {"input_tokens": 0, "output_tokens": 0, "web_searches": 0}
    tables = conference_tables(data)
    batches = [due[i:i + BATCH_SIZE] for i in range(0, len(due), BATCH_SIZE)]
    for batch in batches:
        batch_ids = {f["id"] for f in batch}
        for _ in range(PASSES):
            parsed, searched, usage = ask_claude(client, batch, tables)
            for k in total:
                total[k] += usage[k]
            seen: dict[int, tuple[dict | None, str]] = {}
            for item in (parsed or {}).get("results", []) if parsed else []:
                fid = as_int(item.get("id"))
                if fid in batch_ids and fid not in seen:
                    seen[fid] = validate(item, searched, by_id[fid])
            for fid in batch_ids:
                verdicts[fid].append(seen.get(
                    fid, (None, "no answer returned" if parsed else "reply had no readable JSON")))

    lines = ["### Nations Championship results", f"Model `{MODEL}`, {PASSES} independent pass(es).", "",
             "| Match | Outcome |", "|---|---|"]
    written = 0
    for fid, f in by_id.items():
        results = verdicts[fid]
        oks = [v for v, _ in results if v]
        if len(oks) < PASSES:
            reasons = "; ".join(f"pass {i + 1}: {why}" for i, (v, why) in enumerate(results) if not v)
            lines.append(f"| {f['match']} | skipped - {reasons} |")
            continue
        keys = {(v.get("north"), v.get("south"), v["a"], v["b"], v["ta"], v["tb"]) for v in oks}
        if len(keys) != 1:
            got = " vs ".join(f"{(n + ' v ' + s + ' ') if n else ''}{a}-{b} ({ta}-{tb}T)"
                              for n, s, a, b, ta, tb in sorted(keys, key=str))
            lines.append(f"| {f['match']} | skipped - passes disagree: {got} |")
            continue
        v = oks[0]
        entry = next(r for r in data["results"] if r["id"] == fid)
        if not is_empty(entry):  # defensive - never overwrite
            continue
        entry.update({"a": v["a"], "b": v["b"], "ta": v["ta"], "tb": v["tb"]})
        label = f["match"]
        if is_finals(entry):
            entry.update({"north": v["north"], "south": v["south"]})
            label = f"{f['match']} ({v['north']} v {v['south']})"
            p = int(entry.get("place") or 0)
            if tables and 1 <= p <= 6 and (tables[0][p - 1], tables[1][p - 1]) != (v["north"], v["south"]):
                lines.append(f"| note | our table expected {tables[0][p - 1]} v {tables[1][p - 1]} for "
                             f"{PLACE_NAMES[p]} - check the tie-breakers |")
        if WRITE_REPORTS and v["report"]:
            entry["report"] = v["report"]
        written += 1
        lines.append(f"| {label} | **{v['a']}-{v['b']}** (tries {v['ta']}-{v['tb']}) - "
                     f"sources: {', '.join(v['sites'])} |")

    lines += ["", f"Usage: {total['input_tokens']:,} input tokens, {total['output_tokens']:,} output tokens, "
                  f"{total['web_searches']} web searches."]

    if written:
        data["updated"] = now.strftime("%Y-%m-%d %H:%M UTC")
        if DRY_RUN:
            lines.append(f"\nDRY RUN - {written} result(s) found, results.json not written.")
        else:
            RESULTS_PATH.write_text(dump_results(data), encoding="utf-8")
            lines.append(f"\n{written} result(s) written to {RESULTS_PATH}.")
    else:
        lines.append("\nNo results written this run.")
    summary(lines)
    return 0


if __name__ == "__main__":
    sys.exit(run())
