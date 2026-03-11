"""CSV/Excel parsing, hyperlink extraction, column mapping, and deduplication."""

import io
import re
import zipfile
import xml.etree.ElementTree as ET
from datetime import date, datetime
from typing import Any

import pandas as pd


# ---------------------------------------------------------------------------
# Column name mapping — Swedish export names → internal field names
# ---------------------------------------------------------------------------

COLUMN_MAP: dict[str, str] = {
    "BOLAGSNAMN": "bolagsnamn",
    "ORG.NR": "orgnr",
    "BOLAGSTYP": "bolagstyp",
    "REGISTRERINGSDATUM": "registreringsdatum",
    "ANTAL ANSTÄLLDA": "antal_anstallda",
    "OMSÄTTNING": "omsattning",
    "ÅRETS RESULTAT": "arets_resultat",
    "VINSTMARGINAL I %": "vinstmarginal",
    "SOLIDITET I %": "soliditet",
    "HEMSIDA": "hemsida",
    "ORDFÖRANDE": "ordforande",
    "VERKSTÄLLANDE DIREKTÖR": "vd",
    "ORT (BESÖK)": "ort",
    "LÄN": "lan",
    "BOKSLUTSPERIOD SLUT": "bokslutsperiod_slut",
    "BOKSLUTSPERIOD START": "bokslutsperiod_start",
    "AKTIEKAPITAL": "aktiekapital",
    "EGET KAPITAL": "eget_kapital",
    "SUMMA TILLGÅNGAR": "summa_tillgangar",
    "KASSA OCH BANK": "kassa_och_bank",
    "LÖNER STYRELSE OCH VD": "loner_styrelse_vd",
    "RESULTAT FÖRE SKATT": "resultat_fore_skatt",
    "RÖRELSERESULTAT EFTER AVSKRIVNINGAR": "rorelsresultat",
    "KASSALIKVIDITET I %": "kassalikviditet",
    "SKULDSÄTTNINGSGRAD": "skuldsattningsgrad",
}

# SNI columns (up to 5 codes + names)
SNI_CODE_COLS = [f"SNI {i}" for i in range(1, 6)]
SNI_NAME_COLS = [f"SNI NAMN {i}" for i in range(1, 6)]

# Monetary columns (convert SEK to öre = multiply by 100)
MONEY_COLS = {
    "omsattning", "arets_resultat", "aktiekapital", "eget_kapital",
    "summa_tillgangar", "kassa_och_bank", "loner_styrelse_vd",
    "resultat_fore_skatt", "rorelsresultat",
}

REQUIRED_COLS = {"bolagsnamn", "orgnr"}


def _normalise_col(col: str) -> str:
    """Strip whitespace and normalise column names for matching."""
    return col.strip()


def detect_sheet(wb_sheet_names: list[str]) -> str:
    """Return the preferred sheet name from an Excel workbook."""
    for name in wb_sheet_names:
        if "allabolag lista" in name.lower():
            return name
    return wb_sheet_names[0]


# ---------------------------------------------------------------------------
# Hyperlink extraction from .xlsx ZIP
# ---------------------------------------------------------------------------

def _extract_hyperlinks_from_xlsx(file_bytes: bytes, sheet_index: int = 0) -> dict[int, str]:
    """
    Extract hyperlinks from an .xlsx file by parsing the relationship XML.

    Returns a dict mapping {row_index (0-based): url} for the first column
    (BOLAGSNAMN column).
    """
    links: dict[int, str] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
            names = zf.namelist()

            # Find the sheet file
            sheet_pattern = re.compile(r"xl/worksheets/sheet\d+\.xml")
            sheet_files = sorted([n for n in names if sheet_pattern.match(n)])
            if not sheet_files or sheet_index >= len(sheet_files):
                return links
            sheet_file = sheet_files[sheet_index]

            # Corresponding rels file
            sheet_basename = sheet_file.split("/")[-1]
            rels_path = f"xl/worksheets/_rels/{sheet_basename}.rels"
            if rels_path not in names:
                return links

            # Parse rels XML to build id → url map
            rels_xml = zf.read(rels_path)
            rels_tree = ET.fromstring(rels_xml)
            ns_rels = {"r": "http://schemas.openxmlformats.org/package/2006/relationships"}
            id_to_url: dict[str, str] = {}
            for rel in rels_tree.findall("r:Relationship", ns_rels):
                rid = rel.attrib.get("Id", "")
                target = rel.attrib.get("Target", "")
                if target.startswith("http"):
                    id_to_url[rid] = target

            if not id_to_url:
                return links

            # Parse sheet XML to find hyperlink elements with r:id and cell ref
            sheet_xml = zf.read(sheet_file)
            sheet_tree = ET.fromstring(sheet_xml)

            ns_sheet = {
                "ws": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
                "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
            }

            # Hyperlinks are in <worksheet><hyperlinks><hyperlink ...>
            hyperlinks_el = sheet_tree.find("ws:hyperlinks", ns_sheet)
            if hyperlinks_el is None:
                return links

            for hl in hyperlinks_el.findall("ws:hyperlink", ns_sheet):
                ref = hl.attrib.get("ref", "")  # e.g. "A2"
                rid = hl.attrib.get(
                    "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id", ""
                )
                if not ref or not rid:
                    continue
                url = id_to_url.get(rid, "")
                if not url:
                    continue
                # Parse cell ref → row (A2 → col A, row 2)
                col_match = re.match(r"([A-Za-z]+)(\d+)", ref)
                if not col_match:
                    continue
                col_letters = col_match.group(1).upper()
                row_num = int(col_match.group(2))  # 1-based
                # Only care about column A (BOLAGSNAMN)
                if col_letters == "A":
                    # row_num - 2 = 0-based data row (row 1 is header)
                    links[row_num - 2] = url
    except Exception:
        pass
    return links


def _fallback_url(orgnr: str) -> str:
    """Construct fallback Allabolag URL from ORG.NR."""
    orgnr_clean = orgnr.replace("-", "").strip()
    return f"https://www.allabolag.se/{orgnr_clean}/bokslut"


# ---------------------------------------------------------------------------
# Main parse function
# ---------------------------------------------------------------------------

def parse_file(
    file_bytes: bytes,
    filename: str,
    sheet_name: str | None = None,
) -> tuple[pd.DataFrame, list[str]]:
    """
    Parse an uploaded CSV or Excel file into a DataFrame.

    Returns:
        (df, warnings)  where df has normalised column names and
        warnings is a list of human-readable warning strings.
    """
    warnings: list[str] = []
    is_excel = filename.lower().endswith((".xlsx", ".xls"))

    if is_excel:
        # Load workbook to get sheet names
        xl = pd.ExcelFile(io.BytesIO(file_bytes), engine="openpyxl")
        available_sheets = xl.sheet_names

        if sheet_name is None:
            sheet_name = detect_sheet(available_sheets)
        elif sheet_name not in available_sheets:
            sheet_name = available_sheets[0]
            warnings.append(f"Sheet '{sheet_name}' not found; using '{available_sheets[0]}'.")

        sheet_index = available_sheets.index(sheet_name)

        df = pd.read_excel(
            io.BytesIO(file_bytes),
            sheet_name=sheet_name,
            dtype=str,
            engine="openpyxl",
        )

        # Extract hyperlinks BEFORE anything else
        hyperlinks = _extract_hyperlinks_from_xlsx(file_bytes, sheet_index=sheet_index)
    else:
        # CSV — try multiple encodings
        for enc in ("utf-8-sig", "utf-8", "latin-1", "cp1252"):
            try:
                df = pd.read_csv(
                    io.BytesIO(file_bytes),
                    dtype=str,
                    encoding=enc,
                    sep=None,
                    engine="python",
                )
                break
            except Exception:
                continue
        else:
            raise ValueError("Could not decode CSV file. Try UTF-8 or Latin-1 encoding.")
        hyperlinks = {}
        sheet_name = None

    # Strip whitespace from column names
    df.columns = [_normalise_col(c) for c in df.columns]

    # Build reverse map from normalised column name to internal name
    col_rename: dict[str, str] = {}
    for raw_col, internal in COLUMN_MAP.items():
        norm = _normalise_col(raw_col)
        if norm in df.columns:
            col_rename[norm] = internal

    # Check for SNI columns
    sni_cols_found: list[str] = []
    for i in range(1, 6):
        code_col = f"SNI {i}"
        name_col = f"SNI NAMN {i}"
        if code_col in df.columns:
            sni_cols_found.append(code_col)

    df = df.rename(columns=col_rename)

    # Check required columns
    missing = REQUIRED_COLS - set(df.columns)
    if missing:
        raise ValueError(
            f"Required columns not found: {', '.join(missing)}. "
            "Make sure you're uploading an Allabolag export file."
        )

    # Attach Allabolag URLs
    def get_allabolag_url(idx: int, row_orgnr: str) -> str:
        if idx in hyperlinks:
            return hyperlinks[idx]
        return _fallback_url(row_orgnr)

    df["allabolag_url"] = [
        get_allabolag_url(i, str(row.get("orgnr", "")))
        for i, row in enumerate(df.to_dict("records"))
    ]

    # Combine SNI codes (up to 5) into single comma-separated fields
    sni_code_parts: list[pd.Series] = []
    sni_name_parts: list[pd.Series] = []
    for i in range(1, 6):
        code_col = f"SNI {i}"
        name_col = f"SNI NAMN {i}"
        if code_col in df.columns:
            sni_code_parts.append(df[code_col].fillna(""))
        if name_col in df.columns:
            sni_name_parts.append(df[name_col].fillna(""))

    if sni_code_parts:
        df["sni_codes"] = pd.concat(sni_code_parts, axis=1).apply(
            lambda r: ",".join(v for v in r if v.strip()), axis=1
        )
    else:
        df["sni_codes"] = ""
        warnings.append("No SNI columns found in file.")

    if sni_name_parts:
        df["sni_names"] = pd.concat(sni_name_parts, axis=1).apply(
            lambda r: ",".join(v for v in r if v.strip()), axis=1
        )
    else:
        df["sni_names"] = ""

    # Clean ORG.NR
    df["orgnr"] = df["orgnr"].astype(str).str.strip()

    # Parse monetary columns — strip non-numeric chars, convert to int öre
    for col in MONEY_COLS:
        if col in df.columns:
            df[col] = (
                df[col]
                .astype(str)
                .str.replace(r"[^\d\-\.]", "", regex=True)
                .replace("", None)
                .pipe(pd.to_numeric, errors="coerce")
                .mul(100_000)  # KSEK → SEK → öre
                .round(0)
                .astype("Int64")
            )

    # Parse float percentage columns
    for col in ("vinstmarginal", "soliditet", "kassalikviditet", "skuldsattningsgrad"):
        if col in df.columns:
            df[col] = (
                df[col]
                .astype(str)
                .str.replace(r"[^\d\-\.,]", "", regex=True)
                .str.replace(",", ".", regex=False)
                .replace("", None)
                .pipe(pd.to_numeric, errors="coerce")
            )

    # Parse integer columns
    for col in ("antal_anstallda",):
        if col in df.columns:
            df[col] = (
                df[col]
                .astype(str)
                .str.replace(r"[^\d]", "", regex=True)
                .replace("", None)
                .pipe(pd.to_numeric, errors="coerce")
                .astype("Int64")
            )

    # Normalise dates to ISO 8601 strings
    for date_col in ("registreringsdatum", "bokslutsperiod_slut", "bokslutsperiod_start"):
        if date_col in df.columns:
            df[date_col] = pd.to_datetime(
                df[date_col], errors="coerce", dayfirst=False
            ).dt.strftime("%Y-%m-%d")

    # Deduplicate: keep row with most recent bokslutsperiod_slut per orgnr
    if "bokslutsperiod_slut" in df.columns:
        before = len(df)
        df = df.sort_values("bokslutsperiod_slut", ascending=False, na_position="last")
        df = df.drop_duplicates(subset=["orgnr"], keep="first")
        dupes = before - len(df)
        if dupes > 0:
            warnings.append(f"Removed {dupes} duplicate ORG.NR rows (kept most recent fiscal year).")
    else:
        df = df.drop_duplicates(subset=["orgnr"], keep="first")

    # Drop rows with empty orgnr
    df = df[df["orgnr"].notna() & (df["orgnr"] != "") & (df["orgnr"] != "nan")]

    return df, warnings


def get_sheet_names(file_bytes: bytes, filename: str) -> list[str]:
    """Return list of sheet names from an Excel file."""
    if not filename.lower().endswith((".xlsx", ".xls")):
        return []
    xl = pd.ExcelFile(io.BytesIO(file_bytes), engine="openpyxl")
    return xl.sheet_names


def df_row_to_company_dict(row: dict[str, Any]) -> dict[str, Any]:
    """
    Convert a DataFrame row dict to a dict suitable for creating/updating a Company model.
    Captures extra columns in the 'extra_data' JSON field.
    """
    known_cols = set(COLUMN_MAP.values()) | {
        "orgnr", "allabolag_url", "sni_codes", "sni_names",
    }
    company_dict: dict[str, Any] = {}
    extra: dict[str, Any] = {}

    for k, v in row.items():
        # Skip NaN / pd.NA
        if pd.isna(v) if not isinstance(v, (list, dict)) else False:
            v = None
        if isinstance(v, float) and v != v:  # NaN check
            v = None

        if k in known_cols:
            company_dict[k] = v
        elif k not in ("sni_codes", "sni_names"):
            extra[k] = v

    company_dict["extra_data"] = extra if extra else None
    return company_dict
