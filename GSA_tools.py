#!/usr/bin/env python3
"""
Downloads genome readsets and BioSample metadata from GSA (https://ngdc.cncb.ac.cn/)

Usage:
    python GSA_tools.py \
        --input species_list.txt \
        --download_dir DLs \
        --threads 8

Requirements:
    - conda packages:
        - mamba create -y -n gsa_tools
        - mamba install -y -n gsa_tools conda-forge::pandas
        - mamba install -y -n gsa_tools conda-forge::selenium
        - mamba install -y -n gsa_tools conda-forge::webdriver-manager
        - mamba install -y -n gsa_tools conda-forge::python-chromedriver-binary
    - Chrome v124.0
"""

import argparse
import os
import time
import glob
import shutil
import subprocess
from pathlib import Path
from multiprocessing.pool import ThreadPool
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from selenium.common.exceptions import TimeoutException


### Selenium functions

def parse_args():
    import argparse
    ap = argparse.ArgumentParser(description="GSA species downloader")
    ap.add_argument("-i", "--input", required=True, help="Path to species list")
    ap.add_argument("-d", "--download_dir", required=True, help="Download directory")
    ap.add_argument("-t", "--threads", type=int, default=8, help="Parallel threads")
    ap.add_argument("--no-headless", action="store_true", help="Run Chrome with GUI")
    ap.add_argument("--dry_run", action="store_true", help="Skip FASTQ downloads")
    return ap.parse_args()


def start_chrome(download_dir: str, headless: bool = True):
    """
    Start Chrome with Selenium using the chromedriver installed in PATH.
    No WebDriverManager needed.
    """
    print("[INFO] Starting Chromium")
    print(f"[INFO] Download dir: {download_dir}")
    print(f"[INFO] Headless: {headless}")

    opts = webdriver.ChromeOptions()

    if headless:
        opts.add_argument("--headless=new")

    # Required for HPC
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-software-rasterizer")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--disable-background-networking")
    opts.add_argument("--disable-background-timer-throttling")
    opts.add_argument("--disable-client-side-phishing-detection")
    opts.add_argument("--disable-default-apps")
    opts.add_argument("--disable-sync")
    opts.add_argument("--metrics-recording-only")
    opts.add_argument("--mute-audio")
    opts.add_argument("--no-first-run")
    opts.add_argument("--safebrowsing-disable-auto-update")
    opts.add_argument("--disable-features=VizDisplayCompositor")
    opts.add_argument("--remote-debugging-port=9222")

    # Download prefs
    prefs = {
        "download.default_directory": str(download_dir),
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "safebrowsing.enabled": True,
    }
    opts.add_experimental_option("prefs", prefs)

    # Use Service without specifying path; Selenium finds chromedriver in PATH
    service = Service()

    driver = webdriver.Chrome(service=service, options=opts)
    driver.set_page_load_timeout(90)

    print("[INFO] Chromium started successfully")
    return driver


def page_has_no_items(driver, timeout=5):
    try:
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((
                By.XPATH,
                "//div[contains(@class,'panel-heading')][contains(., 'No items found')]"
            ))
        )
        return True
    except TimeoutException:
        return False


def get_search_result_count(driver):
    try:
        text = driver.find_element(
            By.XPATH,
            "//*[contains(text(), 'Total Items')]"
        ).text
        # e.g. "Total Items: 5"
        m = re.search(r"Total\s+Items:\s*(\d+)", text)
        if m:
            return int(m.group(1))
        return 0
    except Exception:
        return 0


def find_latest_runinfo(download_dir: Path, timeout: int = 60):
    """
    Wait for RunInfo.csv to appear in the download directory.
    Returns Path object or None if not found.
    """
    print("[INFO] Waiting for RunInfo download...")
    start = time.time()

    while time.time() - start < timeout:
        # Search recursively for CSV/TXT/TSV
        candidates = [
            f for f in download_dir.rglob("*")
            if f.suffix.lower() in {".csv", ".txt", ".tsv"} and f.stat().st_size > 0
        ]
        if candidates:
            latest = max(candidates, key=lambda f: f.stat().st_mtime)
            # Check that no temp ".crdownload" exists for this file
            crdownload = latest.with_suffix(latest.suffix + ".crdownload")
            if not crdownload.exists():
                print(f"[INFO] Detected RunInfo file: {latest}")
                return latest
        time.sleep(1)

    print("[WARN] No RunInfo file detected (timeout)")
    return None


def wait_for_download(dl_dir, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        if not list(Path(dl_dir).glob("*.crdownload")):
            return True
        time.sleep(0.5)
    return False


def click_send_to_runinfo(driver):
    wait = WebDriverWait(driver, 30)

    # Send to
    wait.until(EC.element_to_be_clickable(
        (By.XPATH, "//a[contains(text(),'Send to')]"))
    ).click()
    time.sleep(0.5)

    # File radio
    wait.until(EC.element_to_be_clickable(
        (By.ID, "radio1"))
    ).click()
    time.sleep(0.5)

    # Select RunInfo
    select = wait.until(EC.presence_of_element_located(
        (By.ID, "downloadFile"))
    )
    driver.execute_script(
        "arguments[0].value='run'; arguments[0].dispatchEvent(new Event('change'));",
        select
    )
    time.sleep(0.5)

    # Create files
    wait.until(EC.element_to_be_clickable(
        (By.ID, "createFiles"))
    ).click()

def truncate_runinfo(runinfo_path, ncols=22):
    """
    Keep only the first `ncols` columns of a CSV in-place.
    This fixes issues with embedded commas in text fields.
    """
    tmp_path = runinfo_path.with_suffix(".tmp.csv")
    
    with open(runinfo_path, "r", encoding="utf-8-sig") as infile, \
         open(tmp_path, "w", encoding="utf-8") as outfile:
        
        for line in infile:
            parts = line.strip().split(",")
            outfile.write(",".join(parts[:ncols]) + "\n")
    
    shutil.move(tmp_path, runinfo_path)
    print(f"[INFO] Truncated RunInfo to first {ncols} columns: {runinfo_path}")


def filter_runinfo_by_scientific_name(runinfo_path, genome):
    """
    Filter RunInfo file in-place to keep only rows where
    ScientificName (column 22) contains the genome/species string.
    Header is always preserved. Search isn't optimal in browser clearly!
    """
    tmp_path = runinfo_path.with_suffix(".filtered.csv")

    genome_lc = genome.lower()

    with open(runinfo_path, "r", encoding="utf-8") as infile, \
         open(tmp_path, "w", encoding="utf-8") as outfile:

        header = infile.readline()
        outfile.write(header)

        for line in infile:
            cols = line.rstrip("\n").split(",")
            if len(cols) < 22:
                continue

            scientific_name = cols[21].lower()
            if genome_lc in scientific_name:
                outfile.write(line)

    shutil.move(tmp_path, runinfo_path)
    print(f"[INFO] Filtered RunInfo by ScientificName for '{genome}': {runinfo_path}")


def scrape_biosample_metadata(driver, biosample):
    """Scrape metadata for a single BioSample."""
    url = f"https://ngdc.cncb.ac.cn/biosample/browse/{biosample}"
    driver.get(url)

    try:
        WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.ID, "attribute_table"))
        )
    except TimeoutException:
        print(f"[WARN] BioSample page not found: {biosample}")
        return None

    record = {"BioSample": biosample}

    # Parse main attribute table
    rows = driver.find_elements(By.XPATH, "//table[@id='attribute_table']//tr")
    for row in rows:
        try:
            key = row.find_element(By.TAG_NAME, "th").text.strip()
            val = row.find_element(By.TAG_NAME, "td").text.strip()
            key = re.sub(r"\s+", "_", key)
            if val:  # only keep non-empty
                record[key] = val
        except Exception:
            continue

    # Parse extra metadata tables (Release date, Submitter, etc.)
    extra_rows = driver.find_elements(
        By.XPATH,
        "//tr[th and td and not(ancestor::table[@id='attribute_table'])]"
    )
    for row in extra_rows:
        try:
            key = row.find_element(By.TAG_NAME, "th").text.strip()
            val = row.find_element(By.TAG_NAME, "td").text.strip()
            key = re.sub(r"\s+", "_", key)
            if val:
                record[key] = val
        except Exception:
            continue

    return record


def write_biosample_metadata_parallel(runinfo_csv, output_tsv, threads=4, headless=True):
    """Scrape BioSample metadata and write clean TSV with no empty columns."""
    biosamples = get_biosamples_from_runinfo(runinfo_csv)
    print(f"[INFO] Scraping metadata for {len(biosamples)} BioSamples using {threads} threads...")

    # Start one Chrome driver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.chrome.options import Options
    service = Service()
    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--remote-debugging-port=9222")
    driver = webdriver.Chrome(service=service, options=opts)
    driver.set_page_load_timeout(90)

    # Scrape in threads (shared driver)
    records = []
    from concurrent.futures import ThreadPoolExecutor, as_completed
    def _task(bs):
        try:
            rec = scrape_biosample_metadata(driver, bs)
            if rec:
                # Remove empty columns
                rec = {k: v for k, v in rec.items() if v.strip()}
                # Ensure BioSample first
                rec = {"BioSample": rec.pop("BioSample"), **rec}
            return rec
        except Exception as e:
            print(f"[ERROR] Failed {bs}: {e}")
            return None

    with ThreadPoolExecutor(max_workers=threads) as executor:
        futures = {executor.submit(_task, bs): bs for bs in biosamples}
        for future in as_completed(futures):
            res = future.result()
            if res:
                records.append(res)

    driver.quit()

    if not records:
        print("[INFO] No BioSample metadata retrieved")
        return

    # Build DataFrame with union of all keys
    all_keys = set()
    for rec in records:
        all_keys.update(rec.keys())
    all_keys = ["BioSample"] + sorted(k for k in all_keys if k != "BioSample")

    df = pd.DataFrame([{k: r.get(k, "") for k in all_keys} for r in records])
    # Drop nested Attributes column
    if "Attributes" in df.columns:
        df = df.drop(columns=["Attributes"])
    df.to_csv(output_tsv, sep="\t", index=False)
    print(f"[INFO] BioSample metadata written to {output_tsv}")


### Read downloading functions

def wget_download(url, outpath):
    if os.path.exists(outpath):
        return

    os.makedirs(os.path.dirname(outpath), exist_ok=True)
    cmd = [
        "wget",
        "-c",
        "-O", outpath,
        url,
        "--tries=10",
        "--waitretry=5",
        "--read-timeout=30"
    ]
    subprocess.call(cmd)


def download_from_runinfo(runinfo_file, species_dir, threads):
    # Only read the columns we actually need
    df = pd.read_csv(
        runinfo_file,
        usecols=["Run", "BioSample", "Download_path"],
        dtype=str,
        keep_default_na=False,
        on_bad_lines='warn',   # Warn instead of skipping silently
        engine='python'
    )

    # Strip whitespace from all string cells
    df = df.applymap(lambda x: x.strip() if isinstance(x, str) else x)

    # Debug: first few rows fully
    print("[DEBUG] First 5 rows:")
    print(df.head(5).to_string())

    tasks = []

    for _, row in df.iterrows():
        run = row["Run"]
        biosample = row["BioSample"]
        download_paths = row["Download_path"]

        if not biosample or not download_paths:
            print(f"[WARN] Skipping Run {run}: missing BioSample or Download_path")
            continue

        # Filter only URLs
        urls = [u for u in download_paths.split("|") if u.startswith("ftp://") or u.startswith("http://")]
        if not urls:
            print(f"[WARN] Skipping Run {run}: no valid URL in Download_path")
            continue

        # Create BioSample folder
        biosample_dir = species_dir / biosample
        biosample_dir.mkdir(parents=True, exist_ok=True)

        for url in urls:
            fname = os.path.basename(url)
            outpath = biosample_dir / fname
            tasks.append((url, str(outpath)))

    print(f"[INFO] Downloading {len(tasks)} FASTQs...")
    if tasks:
        pool = ThreadPool(threads)
        pool.starmap(wget_download, tasks)
        pool.close()
        pool.join()


def write_read_manifest(input_dir: Path, output_tsv: Path, depth: int = 1):
    """
    Build a read manifest TSV for a single species directory.

    depth=1 means:
      species_dir / BioSample / *.fastq.gz
    """
    # Catch-all but safe paired-end regexes
    R1_RE = re.compile(
        r"(?:^|[_\.-])(?:[RrFf]?1)(?=[_\.-]?\.(?:f(ast)?q)\.gz$)",
        re.IGNORECASE
    )
    R2_RE = re.compile(
        r"(?:^|[_\.-])(?:[RrFf]?2)(?=[_\.-]?\.(?:f(ast)?q)\.gz$)",
        re.IGNORECASE
    )

    glob_pattern = "/".join(["*"] * depth)

    biosample_dirs = [
        p for p in input_dir.glob(glob_pattern)
        if p.is_dir()
    ]

    if not biosample_dirs:
        print(f"[INFO] No BioSample directories found under {input_dir}, skipping manifest")
        return

    with output_tsv.open("w") as fh:
        fh.write(
            "biosample_path\tfastq_count\tstatus\tshort_read_1\tshort_read_2\t"
            "long_read_primary\tlong_read_extra\n"
        )

        for biosample in sorted(biosample_dirs):

            files = list(biosample.glob("*.f*q.gz"))
            fastq_count = len(files)

            short1, short2, long_reads = [], [], []

            for f in files:
                name = f.name
                if R1_RE.search(name):
                    short1.append(f)
                elif R2_RE.search(name):
                    short2.append(f)
                else:
                    long_reads.append(f)

            has_short = bool(short1 and short2)
            has_long = bool(long_reads)

            if has_short and has_long:
                status = "hybrid"
            elif has_short:
                status = "short_only"
            elif has_long:
                status = "long_only"
            else:
                status = "unknown"

            sr1 = str(sorted(short1)[0]) if short1 else ""
            sr2 = str(sorted(short2)[0]) if short2 else ""
            long_primary = str(sorted(long_reads)[0]) if long_reads else ""
            long_extra = ";".join(str(f) for f in sorted(long_reads)[1:]) if len(long_reads) > 1 else ""

            fh.write(
                f"{biosample}\t{fastq_count}\t{status}\t"
                f"{sr1}\t{sr2}\t{long_primary}\t{long_extra}\n"
            )

    print(f"[INFO] Read manifest written to {output_tsv}")


def get_biosamples_from_runinfo(runinfo_csv):
    df = pd.read_csv(runinfo_csv, dtype=str, keep_default_na=False)
    biosamples = sorted(set(df["BioSample"]))
    return [b for b in biosamples if b]


def scrape_biosample_metadata(driver, biosample):
    url = f"https://ngdc.cncb.ac.cn/biosample/browse/{biosample}"
    driver.get(url)

    try:
        WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.ID, "attribute_table"))
        )
    except TimeoutException:
        print(f"[WARN] BioSample page not found: {biosample}")
        return None

    # Start record with BioSample ID as first column
    record = {"BioSample": biosample}

    # Extract rows from the attributes table
    rows = driver.find_elements(By.XPATH, "//table[@id='attribute_table']//tr")
    for row in rows:
        try:
            key = row.find_element(By.TAG_NAME, "th").text.strip()
            val = row.find_element(By.TAG_NAME, "td").text.strip()
            key = re.sub(r"\s+", "_", key)
            record[key] = val
        except Exception:
            continue

    # Extra metadata outside attribute_table (Release date, Submitter, etc.)
    extra_rows = driver.find_elements(
        By.XPATH,
        "//table[@class='table2 table2-border']//tr[th and td and not(ancestor::table[@id='attribute_table'])]"
    )
    for row in extra_rows:
        try:
            key = row.find_element(By.TAG_NAME, "th").text.strip()
            val = row.find_element(By.TAG_NAME, "td").text.strip()
            key = re.sub(r"\s+", "_", key)
            # Avoid overwriting BioSample
            if key != "Accession":
                record[key] = val
        except Exception:
            continue

    return record


### Per-genome workflow

def process_genome(driver, genome, download_dir, threads, dry_run=False):
    genome_raw = genome
    genome_url = genome.replace(" ", "+")
    genome_fs  = genome.replace(" ", "_")
    species_dir = Path(download_dir) / genome_fs
    species_dir.mkdir(parents=True, exist_ok=True)

    driver.execute_cdp_cmd(
        "Page.setDownloadBehavior",
        {"behavior": "allow", "downloadPath": str(species_dir)}
    )

    url = (
        "https://ngdc.cncb.ac.cn/gsa/search?searchTerm="
        "%28%28%28%22NGDC%22%5Bcenter%5D%29+AND+"
        "%22fastq%22%5BfileType%5D+AND+"
        "%22WGS%22%5Bstrategy%5D%29+AND+"
        "%22GENOMIC%22%5Bsource%5D%29+AND+"
        f"%22{genome_url}%22+NOT+%22PCR%22"
    )

    print(f"\n=== {genome} ===")
    driver.get(url)
    time.sleep(2)

    if page_has_no_items(driver):
        print(f"[INFO] No items found for {genome}, skipping")
        return genome, 0, False

    try:
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.ID, "downloadContainer"))
        )
    except TimeoutException:
        print(f"[WARN] downloadContainer not found for {genome}, skipping")
        return genome, 0, False

    result_count = get_search_result_count(driver)
    print(f"[INFO] Total Items: {result_count}")
    if result_count == 0:
        print(f"[INFO] No results for {genome}, skipping RunInfo")
        return genome, 0, False

    click_send_to_runinfo(driver)
    wait_for_download(species_dir, timeout=60)

    runinfo = find_latest_runinfo(species_dir, timeout=60)
    if runinfo is None:
        print(f"[WARN] No RunInfo generated for {genome} — skipping")
        return genome, 0, False

    final_csv = species_dir / f"{genome_fs}_RunInfo.csv"
    if runinfo != final_csv:
        shutil.move(runinfo, final_csv)

    # Remove default "RunInfo.csv" if it exists
    default_runinfo = species_dir / "RunInfo.csv"
    if default_runinfo.exists() and default_runinfo != final_csv:
        try:
            default_runinfo.unlink()
            print(f"[INFO] Removed default RunInfo.csv")
        except Exception as e:
            print(f"[WARN] Could not delete default RunInfo.csv: {e}")

    print(f"[INFO] RunInfo saved to {final_csv}")

    truncate_runinfo(final_csv, ncols=22)
    filter_runinfo_by_scientific_name(final_csv, genome)

    with open(final_csv, "r", encoding="utf-8") as f:
        n_rows = sum(1 for _ in f) - 1  # exclude header

    if n_rows <= 0:
        print(f"[INFO] No matching ScientificName rows for {genome}, skipping downloads")
        return genome, 0, False

    biosample_meta_tsv = species_dir / f"{genome_fs}_biosample_metadata.tsv"
    write_biosample_metadata_parallel(final_csv, biosample_meta_tsv, threads=threads, headless=True)

    if dry_run:
        print(f"[INFO] Dry run enabled: skipping FASTQ downloads and manifest generation for {genome}")
    else:
        download_from_runinfo(final_csv, species_dir, threads)
        manifest_path = species_dir / f"{genome_fs}_read_manifest.tsv"
        write_read_manifest(input_dir=species_dir, output_tsv=manifest_path, depth=1)

    return genome, n_rows, True


### Main

def main():
    args = parse_args()

    dl = Path(args.download_dir).absolute()
    dl.mkdir(parents=True, exist_ok=True)

    headless = not args.no_headless
    driver = start_chrome(dl, headless=headless)

    with open(args.input) as f:
        genomes = [l.strip() for l in f if l.strip()]

    try:
        for g in genomes:
            # Process RunInfo & metadata (downloads skipped if dry_run)
            result = process_genome(driver, g, dl, args.threads, dry_run=args.dry_run)

    finally:
        driver.quit()

if __name__ == "__main__":
    main()
