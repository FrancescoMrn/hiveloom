# Results: article-digest (Opus 5 / Sonnet 5, output-heavy task)

All arms scored with the identical deterministic checks (`validators-src/digest_on_page.py:check`), one fence layer stripped for every arm. Success = source_url exact AND summary 100-260 original words (not verbatim page text) AND 5 distinct verbatim quotes (<=1 missing tolerated) AND outline verbatim (<=1 missing tolerated).

| Arm | n | Success | Halluc. quotes | Verbatim summary | Mean cost | Cost/success | p50 lat (s) | Mean output words |
|---|---|---|---|---|---|---|---|---|
| opus-harness | 5 | 100% | 0% | 0% | $0.0383 | $0.0383 | 14.9 | 353 |
| opus-raw | 5 | 80% | 0% | 0% | $0.0256 | $0.0320 | 14.3 | 370 |
| sonnet-harness | 5 | 100% | 0% | 0% | $0.0142 | $0.0142 | 11.7 | 318 |
| sonnet-raw | 5 | 80% | 0% | 0% | $0.0113 | $0.0141 | 11.1 | 300 |

## Failures by arm

### opus-raw
- s25 e1: output is not valid JSON

### sonnet-raw
- s29 e1: each quote must be 6-60 words; offending: ['You are not your code.'].

