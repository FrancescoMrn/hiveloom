# Harness code assets

Python files that `../build_harnesses.py` writes into the demo harnesses.

They live here rather than inline in the builder because they are real modules
— a `@tool` function, two validators, a provider — and keeping them as files
means they are linted, imported and read like the code they are, instead of as
string literals nothing checks.

| file | goes to | is |
|---|---|---|
| `fetch_clean.py` | `article-extractor/tools/` | the custom `@tool` that fetches a page and distills it to a bounded digest |
| `article_on_page.py` | `article-extractor/validators/` | the validator that re-fetches the page to catch invented headings |
| `qa_provider.py` | `routing-lab/extensions/` | the scripted, offline provider that makes the fork/evolve walkthrough reproducible |

Edit here, then rebuild:

```bash
python scripts/build_harnesses.py --only article-extractor
```

Editing the copy inside a harness works until the next rebuild overwrites it.
