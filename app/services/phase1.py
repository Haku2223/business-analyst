"""Phase 1 filtering engine — hard + soft filters using pandas vectorised operations."""

from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd


# ---------------------------------------------------------------------------
# Default filter configuration
# ---------------------------------------------------------------------------

DEFAULT_FILTER_CONFIG: dict[str, Any] = {
    # Hard filters (enabled by default)
    "hard_company_type_enabled": True,
    "hard_company_type_value": "Aktiebolag",

    "hard_age_enabled": True,
    "hard_age_min_years": 15,

    "hard_revenue_enabled": True,
    "hard_revenue_min": 3_000_000,   # SEK
    "hard_revenue_max": 30_000_000,  # SEK

    "hard_employees_enabled": True,
    "hard_employees_min": 3,
    "hard_employees_max": 30,

    "hard_sni_enabled": True,
    "sni_codes": [
        "33", "43", "62", "71", "81",
        "25", "26", "27", "28", "46",
        "52", "69", "74", "78", "80",
        "85", "37", "38", "49",
    ],

    "hard_profitability_enabled": True,

    "hard_exclude_publikt_aktiebolag_enabled": True,

    # Soft filters (enabled by default)
    "soft_margin_enabled": True,
    "soft_margin_min_pct": 10.0,

    "soft_soliditet_enabled": True,
    "soft_soliditet_min_pct": 50.0,

    "soft_recency_enabled": True,
    "soft_recency_months": 18,

    "soft_county_enabled": False,
    "soft_county_values": [],  # list of county names

    # Per-filter type overrides: "hard" = exclude on fail, "soft" = −1 score on fail
    "filter_types": {
        "company_type": "hard",
        "company_age": "hard",
        "revenue": "hard",
        "employees": "hard",
        "sni_code": "hard",
        "profitability": "hard",
        "exclude_publikt_aktiebolag": "hard",
        "profit_margin": "soft",
        "soliditet": "soft",
        "data_recency": "soft",
        "county": "soft",
    },

    # Display helpers
    "revenue_min_msek": 3,
    "revenue_max_msek": 30,
    "employees_min": 3,
    "employees_max": 30,
    "min_age_years": 15,
}


# ---------------------------------------------------------------------------
# Phase 1 engine
# ---------------------------------------------------------------------------

def run_phase1(df: pd.DataFrame, config: dict[str, Any]) -> dict[str, Any]:
    """
    Run Phase 1 hard + soft filters on a DataFrame of companies.

    Monetary columns in df are stored in öre (int). The filter thresholds
    in config are in SEK, so we multiply thresholds by 100 internally.

    Returns:
        {
            "passed": DataFrame of companies that passed all hard filters,
            "all_results": list of row dicts with _phase1_passed and _failed_filters keys,
        }
    """
    today = date.today()

    # Work on a copy to avoid mutating caller's data
    df = df.copy()
    n = len(df)

    # Initialise tracking columns
    df["_hard_fail"] = False
    df["_failed_filters"] = [[] for _ in range(n)]
    df["_soft_score"] = 0  # penalty (negative)

    # Per-filter type config: "hard" excludes, "soft" scores
    filter_types: dict[str, str] = config.get("filter_types", {})

    # ------------------------------------------------------------------ #
    # HARD FILTER 1: Company type (BOLAGSTYP must contain "Aktiebolag")   #
    # ------------------------------------------------------------------ #
    if config.get("hard_company_type_enabled", True) and "bolagstyp" in df.columns:
        val = str(config.get("hard_company_type_value", "Aktiebolag"))
        mask = ~df["bolagstyp"].astype(str).str.contains(val, case=False, na=False)
        df = _apply_filter(df, mask, "company_type", filter_types, "hard")

    # ------------------------------------------------------------------ #
    # FILTER 2: Company age (REGISTRERINGSDATUM ≥ N years ago)            #
    # ------------------------------------------------------------------ #
    if config.get("hard_age_enabled", True) and "registreringsdatum" in df.columns:
        min_years = int(config.get("hard_age_min_years", 15))
        cutoff = today - timedelta(days=min_years * 365.25)
        reg_dates = pd.to_datetime(df["registreringsdatum"], errors="coerce").dt.date
        mask = reg_dates.isna() | (reg_dates > cutoff)
        df = _apply_filter(df, mask, "company_age", filter_types, "hard")

    # ------------------------------------------------------------------ #
    # FILTER 3: Revenue (OMSÄTTNING in öre)                               #
    # ------------------------------------------------------------------ #
    if config.get("hard_revenue_enabled", True) and "omsattning" in df.columns:
        rev_min = int(config.get("hard_revenue_min", 3_000_000)) * 100  # → öre
        rev_max = int(config.get("hard_revenue_max", 30_000_000)) * 100
        rev = pd.to_numeric(df["omsattning"], errors="coerce")
        mask = rev.isna() | (rev < rev_min) | (rev > rev_max)
        df = _apply_filter(df, mask, "revenue", filter_types, "hard")

    # ------------------------------------------------------------------ #
    # FILTER 4: Employees                                                  #
    # ------------------------------------------------------------------ #
    if config.get("hard_employees_enabled", True) and "antal_anstallda" in df.columns:
        emp_min = int(config.get("hard_employees_min", 3))
        emp_max = int(config.get("hard_employees_max", 30))
        emp = pd.to_numeric(df["antal_anstallda"], errors="coerce")
        mask = emp.isna() | (emp < emp_min) | (emp > emp_max)
        df = _apply_filter(df, mask, "employees", filter_types, "hard")

    # ------------------------------------------------------------------ #
    # FILTER 5: SNI code match (prefix-based)                             #
    # ------------------------------------------------------------------ #
    if config.get("hard_sni_enabled", True) and "sni_codes" in df.columns:
        sni_prefixes = [str(c).strip() for c in config.get("sni_codes", []) if str(c).strip()]
        if sni_prefixes:
            def _sni_match(codes_str: str) -> bool:
                """True if any code in codes_str starts with any prefix."""
                if not codes_str or pd.isna(codes_str):
                    return False
                for code in str(codes_str).split(","):
                    code = code.strip()
                    for prefix in sni_prefixes:
                        if code.startswith(prefix):
                            return True
                return False

            sni_match = df["sni_codes"].apply(_sni_match)
            mask = ~sni_match
            df = _apply_filter(df, mask, "sni_code", filter_types, "hard")

    # ------------------------------------------------------------------ #
    # FILTER 6: Exclude Publikt Aktiebolag (publicly listed)              #
    # ------------------------------------------------------------------ #
    if config.get("hard_exclude_publikt_aktiebolag_enabled", True) and "bolagstyp" in df.columns:
        mask = df["bolagstyp"].astype(str).str.contains("Publikt aktiebolag", case=False, na=False)
        df = _apply_filter(df, mask, "exclude_publikt_aktiebolag", filter_types, "hard")

    # ------------------------------------------------------------------ #
    # FILTER 7: Profitability (RESULTAT EFTER FINANSNETTO > 0)            #
    # Falls back to ÅRETS RESULTAT if the column is not present.          #
    # ------------------------------------------------------------------ #
    if config.get("hard_profitability_enabled", True):
        result_col = None
        if "resultat_efter_finansnetto" in df.columns:
            result_col = "resultat_efter_finansnetto"
        elif "arets_resultat" in df.columns:
            result_col = "arets_resultat"
        if result_col:
            result = pd.to_numeric(df[result_col], errors="coerce")
            mask = result.isna() | (result <= 0)
            df = _apply_filter(df, mask, "profitability", filter_types, "hard")

    # ------------------------------------------------------------------ #
    # FILTER 8: Profit margin ≥ N%                                        #
    # ------------------------------------------------------------------ #
    if config.get("soft_margin_enabled", True) and "vinstmarginal" in df.columns:
        min_margin = float(config.get("soft_margin_min_pct", 10.0))
        margin = pd.to_numeric(df["vinstmarginal"], errors="coerce")
        mask = margin.isna() | (margin < min_margin)
        df = _apply_filter(df, mask, "profit_margin", filter_types, "soft")

    # ------------------------------------------------------------------ #
    # FILTER 9: Soliditet ≥ N%                                            #
    # ------------------------------------------------------------------ #
    if config.get("soft_soliditet_enabled", True) and "soliditet" in df.columns:
        min_soliditet = float(config.get("soft_soliditet_min_pct", 50.0))
        soliditet = pd.to_numeric(df["soliditet"], errors="coerce")
        mask = soliditet.isna() | (soliditet < min_soliditet)
        df = _apply_filter(df, mask, "soliditet", filter_types, "soft")

    # ------------------------------------------------------------------ #
    # FILTER 10: Data recency (BOKSLUTSPERIOD SLUT within N months)       #
    # ------------------------------------------------------------------ #
    if config.get("soft_recency_enabled", True) and "bokslutsperiod_slut" in df.columns:
        months = int(config.get("soft_recency_months", 18))
        cutoff = today - timedelta(days=months * 30.5)
        bokslut_dates = pd.to_datetime(df["bokslutsperiod_slut"], errors="coerce").dt.date
        mask = bokslut_dates.isna() | (bokslut_dates < cutoff)
        df = _apply_filter(df, mask, "data_recency", filter_types, "soft")

    # ------------------------------------------------------------------ #
    # FILTER 11: County (optional multi-select)                           #
    # ------------------------------------------------------------------ #
    if config.get("soft_county_enabled", False) and "lan" in df.columns:
        counties = config.get("soft_county_values", [])
        if counties:
            counties_lower = [c.lower().strip() for c in counties]
            lan = df["lan"].astype(str).str.lower().str.strip()
            mask = ~lan.isin(counties_lower)
            df = _apply_filter(df, mask, "county", filter_types, "soft")

    # ------------------------------------------------------------------ #
    # Compute age (years) for display                                      #
    # ------------------------------------------------------------------ #
    if "registreringsdatum" in df.columns:
        reg_dates = pd.to_datetime(df["registreringsdatum"], errors="coerce").dt.date
        df["_age_years"] = reg_dates.apply(
            lambda d: (today - d).days // 365 if pd.notna(d) else None
        )

    # Mark phase1_passed
    df["_phase1_passed"] = ~df["_hard_fail"]

    passed_df = df[df["_phase1_passed"]].copy()

    all_results = df.to_dict("records")

    return {
        "passed": passed_df,
        "all_results": all_results,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _apply_filter(
    df: pd.DataFrame,
    fail_mask: pd.Series,
    filter_name: str,
    filter_types: dict[str, str],
    default_type: str,
) -> pd.DataFrame:
    """Apply a filter as hard or soft based on filter_types config."""
    ftype = filter_types.get(filter_name, default_type)
    if ftype == "hard":
        return _apply_hard(df, fail_mask, filter_name)
    return _apply_soft(df, fail_mask, filter_name)


def _apply_hard(df: pd.DataFrame, fail_mask: pd.Series, filter_name: str) -> pd.DataFrame:
    """Mark rows where fail_mask is True as hard-failed with the given filter name."""
    df.loc[fail_mask, "_hard_fail"] = True
    # Append filter name to _failed_filters list for failing rows
    indices = df.index[fail_mask]
    for idx in indices:
        df.at[idx, "_failed_filters"] = df.at[idx, "_failed_filters"] + [filter_name]
    return df


def _apply_soft(df: pd.DataFrame, fail_mask: pd.Series, filter_name: str) -> pd.DataFrame:
    """Add −1 soft penalty and record soft filter name for failing rows."""
    df.loc[fail_mask, "_soft_score"] = df.loc[fail_mask, "_soft_score"] - 1
    indices = df.index[fail_mask]
    for idx in indices:
        df.at[idx, "_failed_filters"] = df.at[idx, "_failed_filters"] + [f"soft:{filter_name}"]
    return df
