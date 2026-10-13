# schemify

A single-file, dependency-free CLI that scans a messy CSV/TSV/JSON file, infers
a clean type for every field, and emits a typed schema (JSON Schema, Pydantic,
Python dataclass, or TypeScript interface) plus an optional cleaned copy of
the data.

## Requirements
Python 3.7+. No third-party packages needed (the generated Pydantic code
imports `pydantic`, but `schemify.py` itself does not).

## Usage
```bash
python3 schemify.py infer INPUT [options]
```

| Option | Description |
|---|---|
| `--name NAME` | Name for the generated schema/class/interface (default `Record`) |
| `--format {jsonschema,pydantic,dataclass,typescript,all}` | Output format (default `jsonschema`) |
| `--schema-out FILE` | Write the schema to a file instead of stdout |
| `--clean-out FILE` | Also write cleaned/typed records to FILE |
| `--clean-format {json,csv}` | Format for `--clean-out` (default `json`) |
| `--quiet` | Suppress the field-by-field report table |

## What it does
- Reads `.csv`, `.tsv`, or `.json` (a JSON array of objects, or a single object)
- Flattens nested JSON objects with dot notation (`meta.plan`)
- Normalizes common "missing value" spellings (`""`, `NA`, `N/A`, `null`,
  `None`, `NaN`, `-`, `?`, `unknown`, ...) to a real null
- Infers the tightest type each column supports: `bool`, `int`, `float`,
  `date`, `datetime`, `string`, `array`, or `object` — widening to a looser
  common type when a column mixes types (e.g. `int` + `float` → `float`)
- Reports nullability, % missing, unique-value count, and sample values per
  field
- Emits the schema in your format of choice, and can write out the cleaned,
  type-coerced records alongside it

## Example
```bash
python3 schemify.py infer customers.csv --format pydantic --schema-out models.py --clean-out clean.json
```

## Notes / limitations
- Type inference is heuristic (regex + date-format matching), not a full
  statistical model — always eyeball the report table on unfamiliar data.
- Boolean detection only recognizes `true/false/yes/no/y/n/t/f` (case
  insensitive); bare `0`/`1` are treated as integers, not booleans, since
  that's far more often correct for real-world columns (e.g. IDs).
- CSV/TSV values are always strings on disk, so numeric/boolean/date typing
  there is inferred from content, not a file-format hint.
- Rows with more columns than the header get the extras collected into an
  `_overflow` field; duplicate header names are auto-renamed (`id`, `id__2`)
  so no column is silently dropped. Both cases print a warning to stderr.
- If you pass `--name`, avoid TypeScript/JS built-ins (`Array`, `Map`, `Set`,
  etc.) when using `--format typescript` — the generated interface will
  collide with the built-in type. The default name (`Schema`) is safe.
