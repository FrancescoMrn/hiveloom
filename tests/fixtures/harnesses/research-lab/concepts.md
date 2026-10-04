What we care about at the shipping desk:

- Every request gets a quoted price — never null, whatever unit the weight is
  written in.
- The answer is one JSON object with zone, weight_kg and price, and nothing
  else: billing parses it.
