#!/usr/bin/env python3
"""
schemify — turn messy CSV/JSON into clean, typed schemas.

Usage:
    python schemify.py infer INPUT [options]

Examples:
    python schemify.py infer data.csv
    python schemify.py infer data.json --format pydantic --schema-out schema.py
    python schemify.py infer data.csv --format all --clean-out clean.json
"""

import argparse
import csv
import json
import re
import sys
from collections import Counter, OrderedDict
from datetime import datetime
from pathlib import Path

NULL_TOKENS = {"", "na", "n/a", "null", "none", "nan", "nil", "-", "--", "?", "unknown", "undefined"}
BOOL_TRUE = {"true", "yes", "y", "t"}
BOOL_FALSE = {"false", "no", "n", "f"}

DATE_FORMATS = [
    ("%Y-%m-%dT%H:%M:%S", "datetime"),
    ("%Y-%m-%d %H:%M:%S", "datetime"),
    ("%Y-%m-%d", "date"),
    ("%Y/%m/%d", "date"),
    ("%m/%d/%Y", "date"),
    ("%d/%m/%Y", "date"),
    ("%m-%d-%Y", "date"),
]

INT_RE = re.compile(r"^[+-]?\d+$")
FLOAT_RE = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$")


def is_null_token(value):
    if value is None:
        return True
    return str(value).strip().lower() in NULL_TOKENS


def classify_scalar(raw):
    """Return (type_name, cleaned_value) for a single raw value."""
    if isinstance(raw, list):
        return "array", raw
    if isinstance(raw, dict):
        return "object", raw
    if is_null_token(raw):
        return "null", None
    if isinstance(raw, bool):
        return "bool", raw
    if isinstance(raw, int):
        return "int", raw
    if isinstance(raw, float):
        return "float", raw
    s = str(raw).strip()
    low = s.lower()

    if low in BOOL_TRUE:
        return "bool", True
    if low in BOOL_FALSE:
        return "bool", False

    if INT_RE.match(s):
        try:
            return "int", int(s)
        except ValueError:
            pass

    if FLOAT_RE.match(s):
        try:
            return "float", float(s)
        except ValueError:
            pass

    for fmt, kind in DATE_FORMATS:
        try:
            dt = datetime.strptime(s, fmt)
            return kind, dt.isoformat()
        except ValueError:
            continue

    return "string", s


def widen(t1, t2):
    """Combine two inferred types into the loosest common type that fits both."""
    if t1 == t2:
        return t1
    types = {t1, t2}
    if "null" in types:
        other = types - {"null"}
        return other.pop() if other else "null"
    if types == {"int", "float"}:
        return "float"
    if types == {"date", "datetime"}:
        return "datetime"
    if types == {"array"} or types == {"object"}:
        return types.pop()
    return "string"


def flatten(obj, prefix=""):
    """Flatten nested dicts using dot notation. Lists/arrays are kept as-is."""
    items = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, dict):
                items.update(flatten(v, key))
            else:
                items[key] = v
    else:
        items[prefix or "value"] = obj
    return items


def dedupe_fieldnames(names):
    """Rename duplicate CSV header names (id, id -> id, id__2) so no column is
    silently overwritten/lost."""
    seen = {}
    result = []
    for n in names:
        if n not in seen:
            seen[n] = 1
            result.append(n)
        else:
            seen[n] += 1
            result.append(f"{n}__{seen[n]}")
    return result


def load_delimited(p, delim):
    with open(p, "r", encoding="utf-8", newline="") as f:
        raw_reader = csv.reader(f, delimiter=delim)
        try:
            header = next(raw_reader)
        except StopIteration:
            return []
        fieldnames = dedupe_fieldnames(header)
        if fieldnames != header:
            print(f"warning: {p.name} has duplicate column headers; duplicates were "
                  f"renamed ({', '.join(f'{a} -> {b}' for a, b in zip(header, fieldnames) if a != b)})",
                  file=sys.stderr)

    with open(p, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f, fieldnames=fieldnames, delimiter=delim, restkey="_overflow")
        next(reader)  # skip the header line itself, already captured above
        rows = []
        saw_overflow = False
        for row in reader:
            if "_overflow" in row:
                saw_overflow = True
            rows.append(dict(row))
        if saw_overflow:
            print(f"warning: some rows in {p.name} have more columns than the header; "
                  f"extra values were collected into an '_overflow' field", file=sys.stderr)
        return rows


def load_records(path):
    p = Path(path)
    suffix = p.suffix.lower()

    if suffix == ".json":
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data = [data]
        return [flatten(rec) if isinstance(rec, dict) else {"value": rec} for rec in data]

    if suffix in (".csv", ".tsv"):
        delim = "\t" if suffix == ".tsv" else ","
        return load_delimited(p, delim)

    # Fall back: sniff content
    with open(p, "r", encoding="utf-8") as f:
        head = f.read(2048)
    if head.strip().startswith("{") or head.strip().startswith("["):
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data = [data]
        return [flatten(rec) if isinstance(rec, dict) else {"value": rec} for rec in data]

    raise ValueError(f"Unrecognized file type '{suffix}'. Use .csv, .tsv, or .json")


class FieldStats:
    def __init__(self, name):
        self.name = name
        self.inferred_type = None
        self.n_total = 0
        self.n_null = 0
        self.samples = []
        self.uniques = set()

    def observe(self, raw):
        self.n_total += 1
        t, cleaned = classify_scalar(raw)
        self.inferred_type = t if self.inferred_type is None else widen(self.inferred_type, t)
        if t == "null":
            self.n_null += 1
        else:
            if len(self.samples) < 3:
                self.samples.append(cleaned)
            if len(self.uniques) < 5000:
                key = json.dumps(cleaned, default=str) if isinstance(cleaned, (list, dict)) else cleaned
                self.uniques.add(key)
        return cleaned

    @property
    def nullable(self):
        return self.n_null > 0

    @property
    def pct_missing(self):
        return 100.0 * self.n_null / self.n_total if self.n_total else 0.0


def infer_schema(records):
    fields = OrderedDict()
    for rec in records:
        for k in rec.keys():
            if k not in fields:
                fields[k] = FieldStats(k)

    cleaned_records = []
    for rec in records:
        cleaned = {}
        for k, stat in fields.items():
            cleaned[k] = stat.observe(rec.get(k, None))
        cleaned_records.append(cleaned)

    for stat in fields.values():
        if stat.inferred_type is None:
            stat.inferred_type = "string"

    return fields, cleaned_records


PY_TYPE = {"int": "int", "float": "float", "bool": "bool", "date": "str", "datetime": "str",
           "string": "str", "array": "list", "object": "dict"}
TS_TYPE = {"int": "number", "float": "number", "bool": "boolean", "date": "string", "datetime": "string",
           "string": "string", "array": "any[]", "object": "Record<string, any>"}
JSONSCHEMA_TYPE = {"int": "integer", "float": "number", "bool": "boolean", "date": "string", "datetime": "string",
                    "string": "string", "array": "array", "object": "object"}


def to_json_schema(fields, title="Schema"):
    props, required = {}, []
    for name, stat in fields.items():
        t = JSONSCHEMA_TYPE.get(stat.inferred_type, "string")
        prop = {"type": [t, "null"] if stat.nullable else t}
        if stat.inferred_type in ("date", "datetime"):
            prop["format"] = stat.inferred_type
        props[name] = prop
        if not stat.nullable:
            required.append(name)
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": title,
        "type": "object",
        "properties": props,
        "required": required,
    }
    return json.dumps(schema, indent=2, ensure_ascii=False)


def sanitize_identifier(name):
    ident = re.sub(r"[^0-9a-zA-Z_]", "_", name)
    if ident and ident[0].isdigit():
        ident = f"f_{ident}"
    return ident or "field"


def to_pydantic(fields, class_name="Schema"):
    needs_optional = any(s.nullable for s in fields.values())
    lines = ["from pydantic import BaseModel"]
    if needs_optional:
        lines.append("from typing import Optional")
    lines += ["", "", f"class {class_name}(BaseModel):"]
    if not fields:
        lines.append("    pass")
    for name, stat in fields.items():
        ident = sanitize_identifier(name)
        py_t = PY_TYPE.get(stat.inferred_type, "str")
        line = f"    {ident}: Optional[{py_t}] = None" if stat.nullable else f"    {ident}: {py_t}"
        if ident != name:
            line += f'  # original field: "{name}"'
        lines.append(line)
    return "\n".join(lines) + "\n"


def to_dataclass(fields, class_name="Schema"):
    needs_optional = any(s.nullable for s in fields.values())
    lines = ["from dataclasses import dataclass"]
    if needs_optional:
        lines.append("from typing import Optional")
    lines += ["", "", "@dataclass", f"class {class_name}:"]
    if not fields:
        lines.append("    pass")
    # required (non-nullable) fields first, then optional ones, since dataclasses
    # require defaults to come last
    required_items = [(n, s) for n, s in fields.items() if not s.nullable]
    optional_items = [(n, s) for n, s in fields.items() if s.nullable]
    for name, stat in required_items:
        ident = sanitize_identifier(name)
        py_t = PY_TYPE.get(stat.inferred_type, "str")
        lines.append(f"    {ident}: {py_t}")
    for name, stat in optional_items:
        ident = sanitize_identifier(name)
        py_t = PY_TYPE.get(stat.inferred_type, "str")
        lines.append(f"    {ident}: Optional[{py_t}] = None")
    return "\n".join(lines) + "\n"


def to_typescript(fields, interface_name="Schema"):
    lines = [f"interface {interface_name} {{"]
    for name, stat in fields.items():
        ts_t = TS_TYPE.get(stat.inferred_type, "string")
        opt = "?" if stat.nullable else ""
        key = name if re.match(r"^[A-Za-z_$][A-Za-z0-9_$]*$", name) else f'"{name}"'
        lines.append(f"  {key}{opt}: {ts_t};")
    lines.append("}")
    return "\n".join(lines) + "\n"


def print_report(fields, n_records, path):
    name_w = max([len(n) for n in fields] + [5]) + 2
    print(f"\nschemify report — {path}")
    print(f"records: {n_records}   fields: {len(fields)}\n")
    header = f"{'field'.ljust(name_w)}{'type'.ljust(10)}{'null?'.ljust(7)}{'missing%'.ljust(10)}{'unique'.ljust(8)}sample"
    print(header)
    print("-" * len(header))
    for name, stat in fields.items():
        sample_vals = ", ".join(str(s) for s in stat.samples)
        print(
            f"{name.ljust(name_w)}{stat.inferred_type.ljust(10)}"
            f"{('yes' if stat.nullable else 'no').ljust(7)}"
            f"{f'{stat.pct_missing:.1f}'.ljust(10)}"
            f"{str(len(stat.uniques)).ljust(8)}{sample_vals}"
        )
    print()


def write_cleaned(cleaned_records, fields, out_path, fmt):
    if fmt == "json":
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(cleaned_records, f, indent=2, default=str, ensure_ascii=False)
    elif fmt == "csv":
        with open(out_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(fields.keys()))
            writer.writeheader()
            for rec in cleaned_records:
                row = {}
                for k, v in rec.items():
                    if v is None:
                        row[k] = ""
                    elif isinstance(v, (list, dict)):
                        row[k] = json.dumps(v, default=str)
                    else:
                        row[k] = v
                writer.writerow(row)
    else:
        raise ValueError(f"Unknown clean format: {fmt}")


def main():
    parser = argparse.ArgumentParser(prog="schemify", description="Turn messy CSV/JSON into clean, typed schemas.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_infer = sub.add_parser("infer", help="Infer a schema from a CSV/JSON file")
    p_infer.add_argument("input", help="Path to input .csv, .tsv, or .json file")
    p_infer.add_argument("--name", default="Schema", help="Schema/class/interface name (default: Schema)")
    p_infer.add_argument(
        "--format", choices=["jsonschema", "pydantic", "dataclass", "typescript", "all"],
        default="jsonschema", help="Schema output format (default: jsonschema)",
    )
    p_infer.add_argument("--schema-out", help="Write schema to this file instead of stdout")
    p_infer.add_argument("--clean-out", help="Write cleaned records to this file")
    p_infer.add_argument("--clean-format", choices=["json", "csv"], default="json", help="Format for --clean-out")
    p_infer.add_argument("--quiet", action="store_true", help="Suppress the field report")

    args = parser.parse_args()

    try:
        records = load_records(args.input)
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

    if not records:
        print("error: no records found in input", file=sys.stderr)
        sys.exit(1)

    fields, cleaned_records = infer_schema(records)

    if not args.quiet:
        print_report(fields, len(records), args.input)

    generators = {
        "jsonschema": lambda: to_json_schema(fields, args.name),
        "pydantic": lambda: to_pydantic(fields, args.name),
        "dataclass": lambda: to_dataclass(fields, args.name),
        "typescript": lambda: to_typescript(fields, args.name),
    }

    if args.format == "all":
        combined = "\n\n".join(f"# ---- {k} ----\n{v()}" for k, v in generators.items())
    else:
        combined = generators[args.format]()

    if args.schema_out:
        with open(args.schema_out, "w", encoding="utf-8") as f:
            f.write(combined)
        print(f"schema written to {args.schema_out}")
    else:
        print(combined)

    if args.clean_out:
        write_cleaned(cleaned_records, fields, args.clean_out, args.clean_format)
        print(f"cleaned data written to {args.clean_out}")


if __name__ == "__main__":
    main()
