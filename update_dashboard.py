"""
Sonatype Learn LMS Dashboard Updater
=====================================
Downloads the Docebo enrollment report, recomputes all metrics,
and updates both the HTML dashboard and the master CSV.

Usage:
  python update_dashboard.py              # Pull fresh data from Docebo API
  python update_dashboard.py --local      # Use existing local CSV (for testing)
  python update_dashboard.py --no-deploy  # Skip git commit/push (CI handles it)

Requirements:
  pip install pandas requests
"""

import io
import os
import re
import sys
import json
import time
import zipfile
import requests
import pandas as pd
from datetime import datetime

# ── CONFIG ────────────────────────────────────────────────────────────────────
# Credentials come from environment variables (GitHub Actions secrets in CI).
# For local runs: set DOCEBO_CLIENT_SECRET, DOCEBO_USERNAME, DOCEBO_PASSWORD.
DOMAIN        = "learn.sonatype.com"
CLIENT_ID     = os.environ.get("DOCEBO_CLIENT_ID", "sonatype-dashboard-updater")
CLIENT_SECRET = os.environ.get("DOCEBO_CLIENT_SECRET", "")
API_USERNAME  = os.environ.get("DOCEBO_USERNAME", "")
API_PASSWORD  = os.environ.get("DOCEBO_PASSWORD", "")
REPORT_ID     = os.environ.get("DOCEBO_REPORT_ID", "619bf79c-970a-4a8e-be8a-dde48a05c652")

MIGRATION_DATE  = "2024-05-12"   # Platform go-live / bulk-enrollment date
INTERNAL_DOMAIN = "sonatype.com" # Email domain for internal Sonatype users


# Consumer/free email domains — not treated as companies in leaderboards
CONSUMER_DOMAINS = {
    "gmail.com", "googlemail.com",
    "yahoo.com", "yahoo.co.uk", "yahoo.fr", "yahoo.de", "yahoo.es",
    "yahoo.it", "yahoo.co.in", "yahoo.com.au", "yahoo.ca",
    "ymail.com", "rocketmail.com",
    "hotmail.com", "hotmail.co.uk", "hotmail.fr", "hotmail.de",
    "outlook.com", "outlook.fr", "outlook.de",
    "live.com", "live.co.uk", "live.fr",
    "msn.com", "icloud.com", "me.com", "mac.com", "aol.com",
    "protonmail.com", "protonmail.ch", "pm.me",
    "mail.com", "gmx.com", "gmx.net",
}

# File paths (update these to match your folder locations)
HTML_PATH = "Sonatype_Learn_LMS_Dashboard.html"
CSV_PATH  = "Fletcher2025_-_EOY.csv"
# ─────────────────────────────────────────────────────────────────────────────


# ── API HELPERS ───────────────────────────────────────────────────────────────

def get_token():
    """
    Authenticate via Resource Owner Password Credentials grant.
    This is required for the analytics/v1 report export API.
    Falls back to client_credentials if password grant fails.
    """
    if not CLIENT_SECRET:
        raise RuntimeError(
            "DOCEBO_CLIENT_SECRET is not set. Export DOCEBO_CLIENT_SECRET, "
            "DOCEBO_USERNAME and DOCEBO_PASSWORD (or configure them as "
            "GitHub Actions secrets) before running."
        )
    for grant, extra in [
        ("password", {"username": API_USERNAME, "password": API_PASSWORD}),
        ("client_credentials", {}),
    ]:
        try:
            resp = requests.post(
                f"https://{DOMAIN}/oauth2/token",
                data={
                    "client_id":     CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                    "grant_type":    grant,
                    "scope":         "api",
                    **extra,
                },
                timeout=30,
            )
            if resp.status_code != 200:
                # Surface Docebo's error body — it says WHY (e.g.
                # unsupported_grant_type = grant not enabled on the OAuth app,
                # invalid_client = wrong client id/secret,
                # invalid_grant = wrong username/password.
                print(f"  ✗ {grant} grant failed: HTTP {resp.status_code} — {resp.text.strip()[:300]}")
                continue
            token = resp.json().get("access_token")
            if token:
                print(f"  ✓ Token obtained (grant: {grant})")
                return token
            print(f"  ✗ {grant} grant returned no access_token: {resp.text.strip()[:300]}")
        except Exception as e:
            print(f"  ✗ {grant} grant failed: {e}")
    raise RuntimeError("All authentication methods failed.")


def download_report(token):
    """
    Export the saved Docebo custom report and return its CSV text.
    Uses the analytics/v1 API (async): kick off → poll → download ZIP → extract CSV.
    """
    headers = {"Authorization": f"Bearer {token}"}

    # --- kick off CSV export ---
    kick = requests.get(
        f"https://{DOMAIN}/analytics/v1/reports/{REPORT_ID}/export/csv",
        headers=headers,
        timeout=30,
    )
    kick.raise_for_status()
    exec_id = kick.json().get("data", {}).get("executionId")
    if not exec_id:
        raise ValueError(f"No executionId in export response: {kick.text}")
    print(f"  Export queued. executionId: {exec_id}")

    # --- poll for completion (up to 5 minutes) ---
    for attempt in range(30):
        time.sleep(10)
        poll = requests.get(
            f"https://{DOMAIN}/analytics/v1/reports/{REPORT_ID}/exports/{exec_id}",
            headers=headers,
            timeout=30,
        )
        poll.raise_for_status()
        pdata = poll.json().get("data", {})
        status = pdata.get("status", "").upper()
        print(f"  Export status: {status} (attempt {attempt+1}/30)")
        if status == "SUCCEEDED":
            break
        if status in ("ERROR", "FAILED"):
            raise RuntimeError(f"Report export failed: {poll.text}")
    else:
        raise TimeoutError("Report export did not complete within 5 minutes.")

    # --- download the ZIP and extract the CSV ---
    dl = requests.get(
        f"https://{DOMAIN}/analytics/v1/reports/{REPORT_ID}/exports/{exec_id}/download",
        headers=headers,
        timeout=120,
    )
    dl.raise_for_status()

    # Response is a ZIP archive containing the CSV
    with zipfile.ZipFile(io.BytesIO(dl.content)) as zf:
        csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not csv_names:
            raise ValueError(f"No CSV found in downloaded ZIP. Contents: {zf.namelist()}")
        with zf.open(csv_names[0]) as f:
            return f.read().decode("utf-8")


# ── DATA LOADING & CLEANING ───────────────────────────────────────────────────

COL_MAP = {
    "Username":                                "username",
    "Branch Name":                             "branch",
    "company_name":                            "company",
    "Email":                                   "email",
    "First Name":                              "first_name",
    "job_title":                               "job_title",
    "Last Name":                               "last_name",
    "sfdc_user_type":                          "sfdc_type",
    "User Last Access Date":                   "last_access",
    "Course title":                            "course",
    "Completion Date":                         "completion_date",  # legacy CSV
    "Course Last Access Date":                 "completion_date",  # API CSV (best proxy)
    "Enrollment Date":                         "enrollment_date",
    "Course Enrollment Status":                "status",
    "Final Score":                             "final_score",
    "Course Progress (%)":                     "progress",
    "Training Material Access from Mobile App":"mobile",
    # Rename raw 'status' (user account status) to avoid conflict with enrollment status
    "status":                                  "user_status",
}


def load_and_clean(csv_path):
    """Load the CSV and return a cleaned, enriched DataFrame."""
    df = pd.read_csv(csv_path, dtype=str, low_memory=False)
    df.columns = [c.strip() for c in df.columns]
    df = df.rename(columns=COL_MAP)

    # Parse dates
    for col in ["completion_date", "enrollment_date", "last_access"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")

    df["progress"] = pd.to_numeric(df.get("progress", pd.Series()), errors="coerce").fillna(0)
    df["mobile"]   = df.get("mobile", pd.Series(dtype=str)).fillna("No").str.strip().str.upper()
    df["status"]   = df.get("status", pd.Series(dtype=str)).fillna("").str.strip()
    df["branch"]   = df.get("branch", pd.Series(dtype=str)).fillna("Unspecified").str.strip()
    df["job_title"]= df.get("job_title", pd.Series(dtype=str)).fillna("").str.strip()

    # Internal vs external
    df["is_internal"] = df["email"].str.lower().str.strip().str.endswith(f"@{INTERNAL_DOMAIN}").fillna(False)

    # Active vs legacy
    # "Active" = user has a recorded last-access date AFTER the migration date.
    # "Legacy" = user's last access is null (never logged in) or pre-migration.
    migration_dt = pd.to_datetime(MIGRATION_DATE)
    user_max_access = df.groupby("username")["last_access"].max()

    active_set = set(
        user_max_access[user_max_access > migration_dt].index
    )
    df["is_active"] = df["username"].isin(active_set)

    # Company enrichment: fill blank company from email domain
    # Consumer email domains (gmail, yahoo, etc.) are left blank — not grouped as companies
    def enrich_company(row):
        co = str(row.get("company", "")).strip()
        if co in ("", "nan", "None"):
            em = str(row.get("email", ""))
            if "@" in em:
                domain = em.split("@")[1].lower()
                if domain in CONSUMER_DOMAINS:
                    return ""   # do not use consumer domain as company name
                return domain
        return co if co not in ("nan", "None") else ""

    df["company_enriched"] = df.apply(enrich_company, axis=1)

    return df


# ── DIMENSION HELPERS ─────────────────────────────────────────────────────────

def build_months(df):
    """Return ordered list of 'Mon YYYY' labels from first to last enrollment."""
    min_d = df["enrollment_date"].dropna().min()
    max_d = df["enrollment_date"].dropna().max()
    if pd.isna(min_d) or pd.isna(max_d):
        return []
    months, cur = [], min_d.replace(day=1)
    end = max_d.replace(day=1)
    while cur <= end:
        months.append(cur.strftime("%b %Y"))
        cur = (cur + pd.offsets.MonthBegin(1))
    return months


def monthly_enroll(df, months):
    out = []
    for m in months:
        dt = pd.to_datetime(f"01 {m}", format="%d %b %Y")
        mask = (df["enrollment_date"].dt.year == dt.year) & (df["enrollment_date"].dt.month == dt.month)
        out.append(int(mask.sum()))
    return out


def monthly_complete(df, months):
    out = []
    for m in months:
        dt = pd.to_datetime(f"01 {m}", format="%d %b %Y")
        mask = (df["status"] == "Completed") & \
               (df["completion_date"].dt.year == dt.year) & \
               (df["completion_date"].dt.month == dt.month)
        out.append(int(mask.sum()))
    return out


def monthly_inprog(df, months):
    out = []
    for m in months:
        dt = pd.to_datetime(f"01 {m}", format="%d %b %Y")
        mask = (df["status"] == "In Progress") & \
               (df["enrollment_date"].dt.year == dt.year) & \
               (df["enrollment_date"].dt.month == dt.month)
        out.append(int(mask.sum()))
    return out


def monthly_logins(df, months):
    """Count unique users whose last_access date falls in each month."""
    user_last = df.dropna(subset=["last_access"]).groupby("username")["last_access"].max().reset_index()
    out = []
    for m in months:
        dt = pd.to_datetime(f"01 {m}", format="%d %b %Y")
        mask = (user_last["last_access"].dt.year == dt.year) & \
               (user_last["last_access"].dt.month == dt.month)
        out.append(int(mask.sum()))
    return out


# Job title → role family
def title_group(raw):
    t = str(raw).lower().strip()
    if not t or t in ("nan", "none", ""):
        return "Not Specified"
    checks = [
        ("Developer / Engineer",   ["developer","engineer","sde","swe","programmer","coder"]),
        ("Security",               ["security","appsec","devsec","infosec","cyber","soc analyst"]),
        ("Manager / Director",     ["manager","director","head of","vp ","vice president","team lead"]),
        ("Architect",              ["architect"]),
        ("Platform / Cloud / Ops", ["platform","cloud","devops","sre","infrastructure","ops"]),
        ("Consultant / Sales",     ["consultant","sales","account","presale","business development"]),
        ("Executive / C-Suite",    ["executive","cto","ceo","ciso","chief","president","founder"]),
        ("IT Support / Ops",       ["support","helpdesk","it ops","sysadmin"]),
        ("Data / Analytics",       ["analyst","data ","bi ","intelligence","scientist"]),
    ]
    for group, patterns in checks:
        if any(p in t for p in patterns):
            return group
    return "Other"


# Internal dept from email local-part heuristic
DEPT_PATTERNS = {
    "CE Team":           ["ce.", "ceteam", "customer.eng"],
    "Customer Success":  ["cs.", "customer.success", ".csm", "custsucc"],
    "G&A":               ["finance", "legal", "hr.", "people.", "recruit", "talent", "ga."],
    "Marketing":         ["marketing", "mktg", "content.", "demand"],
    "P&T":               ["product.", "p&t", ".pt.", "engineering", "eng.", "r&d"],
    "Sales":             ["sales", ".ae.", "sdr", "bdr", "revenue", "account.exec"],
}

def get_dept(email):
    local = email.split("@")[0].lower() if "@" in email else email.lower()
    for dept, patterns in DEPT_PATTERNS.items():
        if any(p in local for p in patterns):
            return dept
    return "Sonatype Internal"


# ── SECTION BUILDERS ──────────────────────────────────────────────────────────

def build_ext(df, months):
    ext = df[~df["is_internal"]].copy()
    comp_df = ext[ext["status"] == "Completed"]
    total    = len(ext)
    completed  = len(comp_df)
    in_prog    = int((ext["status"] == "In Progress").sum())
    enrolled   = int((ext["status"] == "Enrolled").sum())
    comp_rate  = round(completed / total * 100, 1) if total else 0
    n_cos      = int(ext["company_enriched"].nunique())

    # Top courses (by completions)
    top_c = comp_df.groupby("course").size().sort_values(ascending=False).head(10)

    # Top companies
    co_e = ext.groupby("company_enriched").size().rename("e")
    co_c = comp_df.groupby("company_enriched").size().rename("c")
    co   = pd.concat([co_e, co_c], axis=1).fillna(0).astype(int)
    co   = co.sort_values("e", ascending=False).head(15)
    top_cos = [[str(r.Index), int(r.e), int(r.c)] for r in co.itertuples()]

    return {
        "total":            total,
        "completed":        completed,
        "in_progress":      in_prog,
        "enrolled_only":    enrolled,
        "completion_rate":  comp_rate,
        "unique_companies": n_cos,
        "monthly_enroll":   monthly_enroll(ext, months),
        "monthly_complete": monthly_complete(ext, months),
        "monthly_inprog":   monthly_inprog(ext, months),
        "top_course_labels":[str(x) for x in top_c.index.tolist()],
        "top_course_vals":  [int(x) for x in top_c.values.tolist()],
        "top_companies":    top_cos,
    }


DEPT_COLORS = ["#2D36EC","#FE572A","#DAFF02","#5058EF","#969AF5","#B9BCF9","#D5D7FB"]

def build_int(df, months):
    idf = df[df["is_internal"]].copy()
    idf["dept"] = idf["email"].apply(get_dept)

    comp_df  = idf[idf["status"] == "Completed"]
    total    = len(idf)
    completed  = len(comp_df)
    n_users  = int(idf["username"].nunique())
    comp_rate  = round(completed / total * 100, 1) if total else 0
    avg_comp   = round(completed / n_users, 2) if n_users else 0

    depts = sorted(idf["dept"].unique().tolist())
    d_enr  = [int((idf["dept"] == d).sum()) for d in depts]
    d_cmp  = [int(((idf["dept"] == d) & (idf["status"] == "Completed")).sum()) for d in depts]
    d_rate = [round(d_cmp[i] / d_enr[i] * 100, 1) if d_enr[i] else 0.0 for i in range(len(depts))]

    dept_monthly = []
    for i, d in enumerate(depts):
        dept_monthly.append({
            "label":           d,
            "data":            monthly_enroll(idf[idf["dept"] == d], months),
            "backgroundColor": DEPT_COLORS[i % len(DEPT_COLORS)],
            "stack":           "s",
        })

    top_c = comp_df.groupby("course").size().sort_values(ascending=False).head(10)

    return {
        "total":            total,
        "completed":        completed,
        "completion_rate":  comp_rate,
        "unique_users":     n_users,
        "avg_comp":         avg_comp,
        "dept_names":       depts,
        "dept_enroll":      d_enr,
        "dept_complete":    d_cmp,
        "dept_rates":       d_rate,
        "dept_monthly":     dept_monthly,
        "top_course_labels":[str(x) for x in top_c.index.tolist()],
        "top_course_vals":  [int(x) for x in top_c.values.tolist()],
    }


def build_profiles(df):
    ext = df[~df["is_internal"]].copy()
    n_users = int(ext["username"].nunique())

    user_comp  = ext[ext["status"] == "Completed"].groupby("username").size()
    user_enr   = ext.groupby("username").size()

    avg_comp = round(user_comp.sum() / n_users, 2) if n_users else 0

    mobile_yes = int((ext["mobile"] == "YES").sum())
    mobile_pct = round(mobile_yes / n_users * 100, 1) if n_users else 0

    # Completion distribution
    dist_labels = ["0 courses","1 course","2–3 courses","4–5 courses","6–10 courses","11+ courses"]
    dist_vals   = [0]*6
    for u in ext["username"].unique():
        c = int(user_comp.get(u, 0))
        if   c == 0:  dist_vals[0] += 1
        elif c == 1:  dist_vals[1] += 1
        elif c <= 3:  dist_vals[2] += 1
        elif c <= 5:  dist_vals[3] += 1
        elif c <= 10: dist_vals[4] += 1
        else:         dist_vals[5] += 1

    # Title groups
    udf = ext.drop_duplicates("username")[["username","job_title","company_enriched"]].copy()
    udf["tg"] = udf["job_title"].apply(title_group)
    gc = udf.groupby("tg")["username"].count().sort_values(ascending=False)
    tg_labels = gc.index.tolist()
    tg_users  = [int(v) for v in gc.values.tolist()]

    tg_rates = []
    tg_full  = []
    for g in tg_labels:
        gusers  = set(udf[udf["tg"] == g]["username"])
        gdf     = ext[ext["username"].isin(gusers)]
        ge      = len(gdf)
        gc_val  = int((gdf["status"] == "Completed").sum())
        rate    = round(gc_val / ge * 100, 1) if ge else 0.0
        tg_rates.append(rate)
        tg_full.append([g, int(len(gusers)), ge, gc_val, rate])

    # Top users (by completions)
    top_users_raw = []
    for u in ext["username"].unique():
        c = int(user_comp.get(u, 0))
        e = int(user_enr.get(u, 0))
        urow = ext[ext["username"] == u].iloc[0]
        co  = str(urow["company_enriched"]) if str(urow["company_enriched"]) not in ("nan","None","") else ""
        jt  = str(urow["job_title"])        if str(urow["job_title"])        not in ("nan","None","") else ""
        top_users_raw.append([str(u), co, jt, c, e])
    top_users_raw.sort(key=lambda x: x[3], reverse=True)

    # Branch distribution
    br = ext["branch"].value_counts()

    return {
        "total_users":        n_users,
        "avg_completions":    avg_comp,
        "mobile_pct":         mobile_pct,
        "mobile_yes":         mobile_yes,
        "dist_labels":        dist_labels,
        "dist_vals":          dist_vals,
        "title_group_labels": tg_labels,
        "title_group_users":  tg_users,
        "title_group_rates":  tg_rates,
        "title_groups_full":  tg_full,
        "top_users":          top_users_raw[:10],
        "branch_labels":      br.index.tolist(),
        "branch_vals":        [int(v) for v in br.values.tolist()],
    }


def build_all(df, months):
    comp_df = df[df["status"] == "Completed"]
    total   = len(df)
    completed = len(comp_df)
    n_users   = int(df["username"].nunique())
    comp_rate = round(completed / total * 100, 1) if total else 0
    top_c     = comp_df.groupby("course").size().sort_values(ascending=False).head(12)
    return {
        "total":            total,
        "completed":        completed,
        "unique_users":     n_users,
        "completion_rate":  comp_rate,
        "monthly_enroll":   monthly_enroll(df, months),
        "monthly_complete": monthly_complete(df, months),
        "monthly_logins":   monthly_logins(df, months),
        "top_course_labels":[str(x) for x in top_c.index.tolist()],
        "top_course_vals":  [int(x) for x in top_c.values.tolist()],
        "ext_total":        int((~df["is_internal"]).sum()),
        "int_total":        int(df["is_internal"].sum()),
    }


def build_zp(df):
    """Zero-progress enrollment anomaly analysis."""
    ns  = df[(df["status"] == "Enrolled") & (df["progress"] == 0)].copy()
    total   = len(ns)
    mig_dt  = pd.to_datetime(MIGRATION_DATE)
    mig_cnt = int((ns["enrollment_date"].dt.date == mig_dt.date()).sum())
    mig_pct = round(mig_cnt / total * 100, 1) if total else 0
    bulk    = int((ns.groupby("username").size() >= 5).sum())
    top_c   = ns.groupby("course").size().sort_values(ascending=False).head(12)
    top_dt  = ns.groupby(ns["enrollment_date"].dt.date).size().sort_values(ascending=False).head(10)
    return {
        "total":     total,
        "migDay":    mig_cnt,
        "migPct":    mig_pct,
        "bulkUsers": bulk,
        "crsLabels": [str(x) for x in top_c.index.tolist()],
        "crsVals":   [int(x) for x in top_c.values.tolist()],
        "dtLabels":  [str(d) for d in top_dt.index.tolist()],
        "dtVals":    [int(v) for v in top_dt.values.tolist()],
    }


def build_company_view(df, days=None):
    """
    Company leaderboard for the By Company dashboard tab.
    Excludes internal Sonatype users and consumer email domains.
    days=None → all time; otherwise last N days from the dataset's max enrollment date.
    """
    ext = df[~df["is_internal"]].copy()
    ext = ext[ext["company_enriched"].str.strip() != ""]

    if days is not None:
        max_date = df["enrollment_date"].dropna().max()
        if pd.notna(max_date):
            cutoff = max_date - pd.Timedelta(days=days)
            ext = ext[ext["enrollment_date"] >= cutoff]

    if len(ext) == 0:
        return {"n_cos": 0, "total_users": 0, "total_enroll": 0,
                "total_complete": 0, "rate": 0,
                "by_users": [], "by_vol": [], "by_rate": [],
                "by_intensity": [], "recent": [], "insights": []}

    grp        = ext.groupby("company_enriched")
    co_users   = grp["username"].nunique().rename("users")
    co_enroll  = grp.size().rename("enroll")
    co_complete = (ext[ext["status"] == "Completed"]
                   .groupby("company_enriched").size().rename("complete"))
    co = pd.concat([co_users, co_enroll, co_complete], axis=1).fillna(0)
    co["complete"] = co["complete"].astype(int)
    co["rate"]     = (co["complete"] / co["enroll"].replace(0, 1) * 100).round(1)
    co["intens"]   = (co["enroll"]   / co["users"].replace(0, 1)).round(1)

    # Recent-90d table — always relative to full dataset max, regardless of days filter
    full_max  = df["enrollment_date"].dropna().max()
    r_cutoff  = (full_max - pd.Timedelta(days=90)) if pd.notna(full_max) else pd.Timestamp("2000-01-01")
    r_ext     = df[~df["is_internal"]].copy()
    r_ext     = r_ext[r_ext["company_enriched"].str.strip() != ""]
    r_ext     = r_ext[r_ext["enrollment_date"] >= r_cutoff]
    if len(r_ext):
        rg        = r_ext.groupby("company_enriched")
        r_users   = rg["username"].nunique()
        r_enroll  = rg.size()
        r_comp    = (r_ext[r_ext["status"] == "Completed"]
                     .groupby("company_enriched").size())
        r_last    = rg["enrollment_date"].max()
        rdf = pd.concat([r_users.rename("u"), r_enroll.rename("e"),
                         r_comp.rename("c"), r_last.rename("l")], axis=1).fillna(0)
        rdf["rate"] = (rdf["c"] / rdf["e"].replace(0, 1) * 100).round(1)
        rdf = rdf.sort_values("u", ascending=False).head(10)
        recent = [[str(i), int(r.u),
                   r.l.strftime("%Y-%m-%d") if hasattr(r.l, "strftime") else "",
                   float(r.rate)] for i, r in rdf.iterrows()]
    else:
        recent = []

    by_users     = co.sort_values("users",  ascending=False).head(10)
    by_vol       = co.sort_values("enroll", ascending=False).head(10)
    by_rate      = co[co["enroll"] >= 15].sort_values("rate",   ascending=False).head(10)
    by_intensity = co[co["users"]  >= 5 ].sort_values("intens", ascending=False).head(10)

    # Auto-generated insights
    insights = []
    if len(by_users):
        r = by_users.iloc[0]
        insights.append(f"<strong>{by_users.index[0]}</strong> leads in unique learners with "
                        f"{int(r['users']):,}, contributing {int(r['enroll']):,} enrollments.")
    if len(by_rate):
        r = by_rate.iloc[0]
        insights.append(f"<strong>{by_rate.index[0]}</strong> tops completion efficiency at "
                        f"{r['rate']}% across {int(r['enroll']):,} enrollments.")
    if len(by_vol) >= 5:
        top5 = int(by_vol.head(5)["enroll"].sum())
        tot  = int(co["enroll"].sum())
        pct  = round(top5 / tot * 100, 1) if tot else 0
        insights.append(f"The top 5 companies by volume account for "
                        f"<strong>{pct}%</strong> of all tracked external enrollments in this window.")
    if len(by_intensity):
        r = by_intensity.iloc[0]
        insights.append(f"<strong>{by_intensity.index[0]}</strong> shows deepest catalog exploration "
                        f"at {r['intens']} enrollments per user.")

    tot_e = int(co["enroll"].sum())
    tot_c = int(co["complete"].sum())
    return {
        "n_cos":          int(len(co)),
        "total_users":    int(co["users"].sum()),
        "total_enroll":   tot_e,
        "total_complete": tot_c,
        "rate":           round(tot_c / tot_e * 100, 1) if tot_e else 0,
        "by_users":     [[str(i), int(r["users"]),  float(r["rate"])]   for i, r in by_users.iterrows()],
        "by_vol":       [[str(i), int(r["enroll"]), int(r["complete"]), int(r["users"])] for i, r in by_vol.iterrows()],
        "by_rate":      [[str(i), float(r["rate"]), int(r["enroll"])]   for i, r in by_rate.iterrows()],
        "by_intensity": [[str(i), float(r["intens"]),int(r["users"])]   for i, r in by_intensity.iterrows()],
        "recent":       recent,
        "insights":     insights,
    }


# ── PERIOD COMPARISON ─────────────────────────────────────────────────────────

def build_cmp(df, today=None):
    """
    Compute WoW / MoM / YoY comparison stats using actual calendar date ranges.
    today defaults to the current date at script run-time.
    Logins = unique users whose most-recent last_access date falls in the period.
    Completions = records where status=='Completed' and completion_date (proxy) is in the period.
    """
    if today is None:
        today = datetime.now().date()
    today_ts = pd.Timestamp(today)

    # Most recent login date for this audience slice
    max_login = df["last_access"].dropna().max()
    last_login_str = max_login.strftime("%b %d, %Y") if pd.notna(max_login) else "—"

    def _d(ts):
        """Cross-platform date label: 'Apr 8', 'Mar 31', etc."""
        return f"{ts.strftime('%b')} {ts.day}"

    def _enr(s, e):
        mask = (df["enrollment_date"] >= pd.Timestamp(s)) & \
               (df["enrollment_date"] < pd.Timestamp(e))
        return int(mask.sum())

    def _cmp(s, e):
        mask = ((df["status"] == "Completed") &
                (df["completion_date"] >= pd.Timestamp(s)) &
                (df["completion_date"] < pd.Timestamp(e)))
        return int(mask.sum())

    def _lgn(s, e):
        user_max = (df.dropna(subset=["last_access"])
                     .groupby("username")["last_access"].max())
        return int(((user_max >= pd.Timestamp(s)) &
                    (user_max < pd.Timestamp(e))).sum())

    def _period(cs, ce, ps, pe, lbl_cur, lbl_prior):
        return {
            "range_cur":   lbl_cur,
            "range_prior": lbl_prior,
            "enr_cur":     _enr(cs, ce),
            "enr_prior":   _enr(ps, pe),
            "cmp_cur":     _cmp(cs, ce),
            "cmp_prior":   _cmp(ps, pe),
            "login_cur":   _lgn(cs, ce),
            "login_prior": _lgn(ps, pe),
        }

    # ── WoW: rolling last-7 days vs prior-7 days ──
    wce = today_ts + pd.Timedelta(days=1)
    wcs = today_ts - pd.Timedelta(days=6)
    wpe = wcs
    wps = today_ts - pd.Timedelta(days=13)

    # ── MoM: current calendar month vs prior calendar month ──
    mcs = today_ts.replace(day=1)
    mce = today_ts + pd.Timedelta(days=1)
    mps = (mcs - pd.Timedelta(days=1)).replace(day=1)
    mpe = mcs

    # ── YoY: 12-month rolling (from start of current month) vs prior 12 months ──
    ycs = mcs - pd.DateOffset(years=1)
    yce = mce
    yps = mcs - pd.DateOffset(years=2)
    ype = pd.Timestamp(ycs)

    return {
        "last_login": last_login_str,
        "wow": _period(
            wcs, wce, wps, wpe,
            f"{_d(wcs)} – {_d(today_ts)}",
            f"{_d(wps)} – {_d(wpe - pd.Timedelta(days=1))}",
        ),
        "mom": _period(
            mcs, mce, mps, mpe,
            today_ts.strftime("%B %Y"),
            mps.strftime("%B %Y"),
        ),
        "yoy": _period(
            ycs, yce, yps, ype,
            f"{ycs.strftime('%b %Y')} – {today_ts.strftime('%b %Y')}",
            f"{yps.strftime('%b %Y')} – {(ype - pd.Timedelta(days=1)).strftime('%b %Y')}",
        ),
    }


def build_cmp_all(df):
    """Build comparison data for all three audience segments (all / active / legacy)."""
    active_df = df[df["is_active"]].copy()
    legacy_df = df[~df["is_active"]].copy()
    return {
        "all":    build_cmp(df),
        "active": build_cmp(active_df),
        "legacy": build_cmp(legacy_df),
    }


# ── MASTER BUILDERS ───────────────────────────────────────────────────────────

def build_ds(df):
    """Produce the full DS = {active, legacy, all} object."""
    active_df = df[df["is_active"]].copy()
    legacy_df = df[~df["is_active"]].copy()

    months_a = build_months(active_df) if len(active_df) else []
    months_l = build_months(legacy_df) if len(legacy_df) else []
    months_all = build_months(df)

    return {
        "active": {
            "months": months_a,
            "ext":      build_ext(active_df, months_a),
            "int":      build_int(active_df, months_a),
            "profiles": build_profiles(active_df),
            "all":      build_all(active_df, months_a),
        },
        "legacy": {
            "months": months_l,
            "ext":      build_ext(legacy_df, months_l),
            "int":      build_int(legacy_df, months_l),
            "profiles": build_profiles(legacy_df),
            "all":      build_all(legacy_df, months_l),
        },
        "all": {
            "months": months_all,
            "ext":      build_ext(df, months_all),
            "int":      build_int(df, months_all),
            "profiles": build_profiles(df),
            "all":      build_all(df, months_all),
        },
    }


def build_meta(df):
    a = int(df[df["is_active"]]["username"].nunique())
    l = int(df[~df["is_active"]]["username"].nunique())
    t = int(df["username"].nunique())
    return {"lUsers": l, "aUsers": a, "tot": t, "lPct": round(l/t*100, 1) if t else 0}


# ── HTML UPDATER ──────────────────────────────────────────────────────────────

def update_html(html_path, ds, meta, zp, co, cmp_data, date_range):
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()

    ds_js   = json.dumps(ds,       ensure_ascii=False, separators=(",",":"))
    meta_js = json.dumps(meta,     ensure_ascii=False, separators=(",",":"))
    zp_js   = json.dumps(zp,       ensure_ascii=False, separators=(",",":"))
    co_js   = json.dumps(co,       ensure_ascii=False, separators=(",",":"))
    cmp_js  = json.dumps(cmp_data, ensure_ascii=False, separators=(",",":"))

    html = re.sub(r"const DS\s*=\s*\{.*?\};",
                  f"const DS = {ds_js};",   html, flags=re.DOTALL)
    html = re.sub(r"const META\s*=\s*\{.*?\};",
                  f"const META = {meta_js};", html)
    html = re.sub(r"const ZP\s*=\s*\{.*?\};",
                  f"const ZP = {zp_js};",   html, flags=re.DOTALL)
    html = re.sub(r"const CO\s*=\s*\{.*?\};",
                  f"const CO = {co_js};",   html, flags=re.DOTALL)
    html = re.sub(r"const CMP\s*=\s*\{.*?\};",
                  f"const CMP = {cmp_js};", html, flags=re.DOTALL)
    html = re.sub(r'<div class="gnav-date">.*?</div>',
                  f'<div class="gnav-date">{date_range}</div>', html)
    last_updated = datetime.now().strftime("%b %d, %Y")
    html = re.sub(r'<div class="gnav-updated">.*?</div>',
                  f'<div class="gnav-updated">Last update: <b>{last_updated}</b></div>', html)

    # Ensure iframe-detection override is present (strips 100vh blank space when embedded)
    iframe_snippet = """<style>
html[data-iframe] body    { min-height: 0 !important; }
html[data-iframe] .landing{ min-height: 0 !important; height: auto !important; }
</style>
<script>(function(){if(window!==window.top){document.documentElement.setAttribute('data-iframe','1');}}());</script>"""
    if "data-iframe" not in html:
        html = html.replace("</head>", iframe_snippet + "\n</head>", 1)

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"  ✓ HTML updated: {html_path}")


# ── GITHUB PAGES DEPLOY ───────────────────────────────────────────────────────

def deploy_to_github(html_path):
    """Commit the updated dashboard HTML and push to GitHub Pages."""
    import subprocess
    repo_dir = os.path.dirname(os.path.abspath(html_path))
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    try:
        subprocess.run(
            ["git", "-C", repo_dir, "add", os.path.basename(html_path)],
            check=True, capture_output=True, text=True,
        )
        result = subprocess.run(
            ["git", "-C", repo_dir, "commit", "-m", f"Dashboard update {ts}"],
            capture_output=True, text=True,
        )
        # exit code 1 = "nothing to commit" — not an error
        if result.returncode not in (0, 1):
            raise subprocess.CalledProcessError(result.returncode, "git commit", result.stderr)
        subprocess.run(
            ["git", "-C", repo_dir, "push"],
            check=True, capture_output=True, text=True,
        )
        print("  ✓ Deployed to GitHub Pages")
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.strip() if e.stderr else str(e)
        print(f"  ✗ GitHub deploy failed: {stderr}")


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    use_local = "--local" in sys.argv
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"\n{'='*55}")
    print(f"  Sonatype Learn Dashboard Updater — {ts}")
    print(f"{'='*55}")

    if use_local:
        print(f"[LOCAL] Reading CSV: {CSV_PATH}")
    else:
        print("[API] Authenticating with Docebo...")
        try:
            token = get_token()
            print("  ✓ Token obtained")
        except Exception as e:
            print(f"  ✗ Auth failed: {e}")
            print("  → Falling back to local CSV. Fix credentials to enable live data.")
            use_local = True

    if not use_local:
        print(f"[API] Downloading report {REPORT_ID}...")
        try:
            csv_text = download_report(token)
            with open(CSV_PATH, "w", encoding="utf-8", newline="") as f:
                f.write(csv_text)
            print(f"  ✓ CSV saved: {CSV_PATH}")
        except Exception as e:
            print(f"  ✗ Report download failed: {e}")
            print("  → Falling back to local CSV.")
            use_local = True

    if use_local and not os.path.exists(CSV_PATH):
        print(f"\n  ✗ Cannot continue: no local CSV fallback found ({CSV_PATH}).")
        print("    Fix the Docebo credentials/API access and re-run.")
        sys.exit(1)

    print(f"[DATA] Loading {'local' if use_local else 'fresh'} CSV...")
    df = load_and_clean(CSV_PATH)
    n_rows = len(df)
    n_users = df["username"].nunique()
    print(f"  ✓ {n_rows:,} rows / {n_users:,} unique users")

    print("[DATA] Computing metrics...")
    ds   = build_ds(df)
    meta = build_meta(df)
    zp   = build_zp(df)
    co   = {
        "all":    build_company_view(df),
        "last24": build_company_view(df, days=730),
        "last90": build_company_view(df, days=90),
    }
    cmp  = build_cmp_all(df)

    min_d = df["enrollment_date"].dropna().min()
    max_d = df["enrollment_date"].dropna().max()
    date_range = (
        f"{min_d.strftime('%b %Y')} \u2013 {max_d.strftime('%b %Y')}"
        if pd.notna(min_d) and pd.notna(max_d) else "")

    print("[HTML] Updating dashboard...")
    update_html(HTML_PATH, ds, meta, zp, co, cmp, date_range)

    if "--no-deploy" in sys.argv:
        print("[GITHUB] Skipping deploy (--no-deploy).")
    else:
        print("[GITHUB] Deploying dashboard...")
        deploy_to_github(HTML_PATH)

    print(f"\n{'='*55}")
    print(f"  Done!  Active users: {meta['aUsers']:,}  |  Legacy: {meta['lUsers']:,}")
    print(f"  Date range: {date_range}")
    print(f"{'='*55}\n")


if __name__ == "__main__":
    main()
