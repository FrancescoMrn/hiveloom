# Curation notes — article-extractor golden dataset

Method: for every URL below, `fetch_clean(url)` was called for real (via
`importlib` against `harnesses/article-extractor/tools/fetch_clean.py`, no
mocking) on 2026-07-29. Golden JSON was authored from the digest text alone —
title/description/headings were extracted programmatically from the exact
digest lines (no hand-retyping, to avoid corrupting unicode punctuation);
author/published_date were assigned by hand by applying the BUILD rules from
`harness.yaml` (lines 9-50) to the digest text. 32 of 74 tried candidate
URLs (across 4 rounds of fetching) made the final set — the other 42 are
listed below with the reason each was dropped.

**Independent cross-check:** this repo also has a concurrently-built
`inspect_ai` eval scaffold under `evals/article-extractor/` (harnesses,
scorer, `dataset/check_dataset_urls.py`) with its own independent
implementation of the same title/heading-extraction and fingerprint logic
(`inspect_evals/_shared.py`). Running that tool against this dataset live
re-fetches all 32 URLs and diffs their fingerprints — a genuinely independent
verification of this curation work. First run caught a real problem: **s02
(theregister.com/2024/10/04/office_2024/)** drifted within ~20 minutes of
being authored, because the article page carries a "MORE CONTEXT" sidebar of
related stories that changes as The Register publishes new pieces (a new H2
appeared between the two fetches). That's exactly the kind of instability
the news stratum's "won't be edited" guidance is meant to rule out, so it was
swapped for `lwn.net/Articles/990791/` (the Sept 26, 2024 Weekly Edition —
same domain/format as s08, already known stable, and cleanly null on both
author and published_date with no ambiguity). Re-running
`check_dataset_urls.py` after the swap reports 0/32 need triage. Recommend
re-running this check before any real eval sweep, since golden datasets built
from live URLs are inherently exposed to this kind of drift.

## Final set: 32 samples, `evals/article-extractor/dataset/samples.jsonl`

| category  | count |
|-----------|-------|
| news      | 8     |
| docs      | 8     |
| corporate | 8     |
| blog      | 6     |
| edge      | 2     |

## Dropped URLs (tried, not used) and why

### Hard fetch failures (`ERROR:` digest — 404 / 403 / DNS)
Mostly guessed article slugs that don't exist (I don't have live web
browsing built into the fetch tool, so slugs were reconstructed from
memory/search and many were wrong):

- `arstechnica.com/gadgets/2024/09/apple-releases-ios-18-with-a-huge-number-of-new-features/` — 404
- `arstechnica.com/information-technology/2024/06/microsoft-copilot-recall-ai-feature-under-fire-again-heres-why/` — 404
- `theregister.com/2024/06/07/microsoft_recall_delay/` — 404
- `theregister.com/2024/09/09/qualcomm_arm_lawsuit/` — 404
- `bbc.com/news/technology-67512245` — 404
- `bbc.com/news/articles/c4nnwp5nzeeo` — 404
- `zdnet.com/article/the-best-ai-for-coding-in-2024-including-2-new-favorites/` — 404
- `engadget.com/apple-releases-ios-18-with-a-revamped-control-center-...` — 404
- `thehackernews.com/2024/06/new-linux-malware-campaign-exploits.html` — 404
- `lwn.net/Articles/974356/` — 404
- `slashdot.org/story/24/06/07/1234567/...` — 404
- `krebsonsecurity.com/2024/06/whos-behind-the-8base-ransomware-website/` — 404
- `openai.com/index/hello-gpt-4o/` — HTTP 403 (bot-blocked; plain urllib can't get past it)
- `github.blog/2024-06-25-github-copilot-extensions/` — 404
- `blog.cloudflare.com/how-cloudflare-runs-more-javascript/` — 404
- `aws.amazon.com/blogs/aws/amazon-bedrock-now-provides-access-to-anthropics-claude-3-haiku-model/` — 404
- `slack.engineering/rethinking-the-slack-message-composer/` — 404
- `shopify.engineering/inside-shopify-testing` — 404
- `jvns.co/blog/2024/01/23/rust-borrow-checker/` — DNS error (wrong TLD; the real domain is `jvns.ca`, fixed in a later round and used as s25)
- `brendangregg.com/blog/2024-03-17/the-crisis-of-open-source.html` — 404
- `simonwillison.net/2024/Jun/17/aria-hidden-focusable/` — 404 (wrong slug; fixed in a later round and used as s28)
- `tbray.org/ongoing/When/202x/2024/07/10/Fixing-Twitter` — fetch error (wrong path)
- `www.anthropic.com/research/this-page-does-not-exist-9x7q` — 404 **(expected — this is the intentional edge-b candidate, kept as s32)**

### Fetched HTTP 200 but content didn't match what the URL implies (real trap for an extractor)
- `infoworld.com/article/2338131/python-adopts-new-governance-model.html` — the digest's TITLE/content is actually "GitHub begins 2FA rollout" (March 2023), an unrelated article. InfoWorld appears to reassign/redirect old numeric article IDs. Dropped — would produce a golden whose URL and content visibly disagree, which isn't a fair "article extraction" test.
- `engineering.fb.com/2024/06/12/data-infrastructure/` — resolves to the "Data Infrastructure" *category archive* page (a list of unrelated posts with their own dates), not a single dated article. Dropped as not a genuine single-article page.
- `blog.plan99.net/how-to-write-a-git-commit-message-38356f4b31c9` — the URL 404s inside Medium's own app shell, so `fetch_clean` gets a real HTTP 200 page whose title is literally "Medium" and whose only heading is "404". Dropped.

### Fetched fine, dropped as redundant/surplus or too messy for a clean golden
- `developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Global_Objects/Array` — clean digest, but its only date signal is a `<time>` tag reflecting MDN's "last modified" timestamp (confirmed by TAIL TEXT: "This page was last modified on Jul 28, 2026 by MDN contributors"), which churns on essentially every edit. Dropped for flakiness — a golden built today would likely mismatch a re-fetch next week purely because someone tweaked the page, independent of the model's extraction quality.
- `stripe.com/blog/rate-limiters` — real article, but the first 15 headings (max allowed) are almost entirely site-nav H1s (`Payments`, `Revenue`, `Money Management`, `Platforms and marketplaces`, `More`, repeated twice) before the real title heading appears at position 11; the real content headings past that get truncated out. Correct per the verbatim/in-order/max-15 rule, but a much noisier example than the alternatives kept for `corporate`.
- `arstechnica.com/...` (used 4 of these; no further ones tried)
- `krebsonsecurity.com/2024/07/microsoft-patch-tuesday-july-2024-edition/` — good, but redundant with the Chirp Systems article already covering this domain; not used.
- `www.theregister.com/2024/10/04/office_2024/` — **used initially as s02, then dropped**: the independent drift-check (see above) caught its headings changing within ~20 minutes because of a live "MORE CONTEXT" related-articles sidebar. Replaced by the LWN Sept 26, 2024 edition.
- `www.anthropic.com/news/claude-3-5-sonnet` — LEAD TEXT contains two different dates ("Jun 21, 2024" for the article itself and "Aug 28, 2025" for an unrelated adjacent teaser link in the same nav block) — ambiguous enough that I preferred the two cleaner Anthropic pages that made the final set.
- `blog.cloudflare.com/application-security-report-2024-update/` — real 3-author byline but mangled by HTML→text normalization into `"Michael Tremante , Sabina Zejnilovic , and Catherine Newcomb"` (stray spaces before commas). Dropped in favor of the single-author `pq-2024` post (s22), which is much less ambiguous to encode as one `author` string.
- `shopify.engineering/the-case-against-monkey-patching` — good (clean byline "Eileen Uchitelle"), but redundant with the multimodal-LLMs post already used for Shopify; not used.
- `slack.engineering/slacks-migration-to-a-cellular-architecture/` and `slack.engineering/how-we-design-our-apis-at-slack/` — both have a garbled, literally-duplicated `<title>` (e.g. `"... | Engineering at Slack Slack's Migration to a Cellular Architecture – Engineering at Slack"`) and LEAD TEXT polluted with a "Recommended Reading" widget showing unrelated 2026 dates. Dropped both — not clean enough for a golden.
- `huggingface.co/blog/gcp-partnership` — good page, but has a second, later "11/13/2025 Update:" note sitting right next to the article's real "Published January 25, 2024" date, the same kind of two-dates-in-view trap already covered by other flagged entries; skipped once the corporate quota was filled by cleaner picks.
- `danielmiessler.com/blog/we-are-all-building-single-digital-assistant` — uses `<h1>` for every section (18 H1 tags) with literal invisible zero-width-space characters trailing several of them, plus a second, differently-dated "Update:" note. Too noisy for a clean golden.
- `danielmiessler.com/blog/difference-existentialism-nihilism-absurdism` — cleaner alternate from the same domain, kept as a backup but not used (blog quota filled; author would also be flagged, since "Daniel Miessler" appearing at the top of the page is simultaneously the site's name and the byline).
- `doc.rust-lang.org/book/ch01-00-getting-started.html` — fetched fine but the digest is very thin (no meta at all, ~600 chars); kept as a backup, not used since the docs quota was filled with richer pages.
- `httpd.apache.org/docs/2.4/` — good, clean candidate; not used only because the docs quota (8) was filled with more diverse tooling (Python, git, Docker, Kubernetes, Postgres, Bash, Node, plus this dropped one).
- `www.rfc-editor.org/rfc/rfc2616` — good candidate (duplicate H1 quirk, no-confident-date quirk); redundant with other docs entries already covering "null date" and "duplicate heading" patterns (kubernetes, python docs). Not used.
- `motherfuckingwebsite.com/` — real page, real headings (8), but no author/byline of any kind and it's a joke/demo page rather than a genuine article; didn't fit the "blog with a byline" stratum. Not used.
- `example.com/` — trivial placeholder domain, 1 heading; not representative of a real article. Not used.

## Fields flagged for human review

18 of the 32 golden entries carry an explanatory note beyond a mechanical,
uncontested application of the BUILD rules (most are genuine ambiguities;
a few, like s02 and s16, are just documenting that a null is unambiguous).
Full detail is in-line above per drop, but the entries in `samples.jsonl`
that carry a note are: **s02, s08, s09, s10, s11, s12, s13, s14, s15, s16,
s19, s21, s23, s24, s27, s30, s31, s32**. Summary of the interesting ones:

1. **s08 (LWN Weekly Edition, April 18 2024)** — highest-ambiguity entry in
   the set. The only byline-shaped text in the digest ("By Joe Brockmeier
   April 16, 2024") belongs to one inner story, not the Weekly-Edition page
   itself, and its date doesn't match the edition's own date (Apr 18 vs Apr
   16). Golden follows the mechanical rule literally
   (`author="Joe Brockmeier"`, `published_date="2024-04-16"`); a human could
   reasonably override both to `null`.

2. **s09, s10, s13 (docs.python.org x2, kubernetes.io)** — all three show a
   "Last updated/modified on `<today's date>`" build-timestamp in TAIL TEXT.
   Since this is regenerated every time the docs are rebuilt (and would have
   read "today" no matter which day this dataset was built), I treated it as
   *not* a confident `published_date` and set `null`, even though a literal
   reading of "a date visible in LEAD/TAIL TEXT" could argue for extracting
   it. A model under eval that extracts today's date here is arguably not
   wrong per the letter of the system prompt — this is a genuine rule gap
   worth a human decision.

3. **s11 (git-scm.com/docs/git-commit)** — no single confident publish date
   (only a long version/date changelog table) → `null`. Also: the first 15
   headings (max allowed) are entirely left-nav category links; the real
   man-page sections (SYNOPSIS, DESCRIPTION, OPTIONS, ...) never make it into
   the truncated list. Correct per the verbatim/in-order/max-15 rule, but
   low-signal as a golden.

4. **s12, s14 (docs.docker.com, postgresql.org)** — both use
   `META article:published_time` (rule's #1 priority source for dates), but
   the values are recent/rebuild-adjacent enough (postgresql.org's especially,
   2026-05-14) that they may be doc-generation timestamps rather than true
   "publication" dates for content that's been stable for years. Kept per
   rule priority, flagged for review.

5. **s19 (netflixtechblog.com)** — digest has *both*
   `META article:author: https://netflixtechblog.medium.com` (a URL, appears
   first) and `META author: Netflix Technology Blog` (a name, appears
   second); the BUILD rule doesn't state precedence between the two when they
   disagree. Golden picks the readable name.

6. **s21 (aws.amazon.com)** — author identified from a TAIL-TEXT bio
   signature ("Neeraja Rentachintala is Director, Product Management...")
   rather than a `"by Jane Doe"`-style byline or any meta tag — a looser
   match than the system prompt's own example pattern.

7. **s23 (dropbox.tech)** — `META author` names one person ("Alexey Ivanov")
   but the visible byline names two ("By Alexey Ivanov and Oleg Guba"). Golden
   follows the meta tag per rule priority, silently dropping the co-author.

8. **s24 (shopify.engineering)** — page shows a 4-person "Presented by ..."
   credit line *and* a single-name "by Audrey-Anne Guindon" byline further
   down. Golden uses the single name (matches the system prompt's own
   example pattern) over the 4-person list.

9. **s27 (overreacted.io)** — LEAD TEXT literally reads "overreacted by
   February 2, 2019" — the word "by" is immediately followed by a date, not
   a name. A naive byline-pattern matcher could mis-extract "author =
   February 2, 2019" or otherwise get confused; golden correctly sets
   `author=null`. Flagged as a deliberate trap worth checking the model under
   eval against.

10. **s30 (danluu.com)**, **s16 (nodejs.org)** — genuinely minimal digests
    (no meta author/date/description signal at all); all nullable fields are
    `null` with no ambiguity, included to contrast with the flagged entries
    above.

11. **s31 (go.dev/doc/effective_go)** — the intentional "zero headings" edge
    case: real page, HTTP 200, valid TITLE, but zero H1/H2/H3 lines and no
    LEAD/TAIL TEXT at all in the digest (the whole digest is 148 chars).
    `headings=[]` is correct and unambiguous, flagged only for visibility
    since it's an unusually thin digest for a real, live page.

12. **s32 (anthropic.com/research/this-page-does-not-exist-9x7q)** —
    guaranteed-404 edge case, confirmed live (`HTTP Error 404`). Golden is
    the harness's documented failed-fetch fallback verbatim. Note this
    intentionally violates `schemas/output.json`'s strict requirement that
    `title` be a non-empty string — the JSON Schema and the system prompt's
    own documented fallback (`harness.yaml` line 22) disagree with each
    other on this point; the task spec asked for the fallback shape, which is
    what's in `samples.jsonl`.

No other entries required a judgment call: title always came from a literal
`TITLE:` digest line (or `null` only for s32), and `description` was derived
mechanically (`META description` else `META og:description` else `null`) for
all 32 entries with no manual overrides.

## Post-sweep amendment (2026-07-29)

s16 was `https://nodejs.org/api/fs.html` ("latest" docs): the title embeds the
Node version and drifted mid-sweep when v26.5.1 shipped (all arms had already
scored it 3/3 correct against the old golden — see RESULTS.md). Replaced with
the version-pinned `https://nodejs.org/docs/v22.0.0/api/fs.html`, identical
page shape, frozen forever. Lesson for future samples: never use
"latest"-aliased docs URLs.

## Live QA amendment (2026-07-30)

The next-day pre-flight found one drift: `s20` (GitHub Blog) retained the same
article title and first 14 headings, but its fifteenth heading changed from
"The cost of saying yes has changed" to "Tame Dependabot: Group your updates,
slow the cadence, keep security fast." Both are cards in the rotating
`Related posts` section, not article content. The historical `RESULTS.md`
remains tied to dataset/repo hash `30b99e5`; replace or re-verify `s20` before
the next scored sweep rather than silently rewriting that historical result.
