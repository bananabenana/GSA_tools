#!/usr/bin/env python3
"""
Downloads genome readsets and BioSample metadata from GSA (https://ngdc.cncb.ac.cn/)

Usage:
    # Download reads and metadata from a list of species/genera
    python GSA_tools.py \
        --taxa species_list.txt \
        --download_dir DLs \
        --threads 4

    # Download metadata from a list of BioSample IDs
    python GSA_tools.py \
        --biosample biosample_list.txt \
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

__version__ = "1.0.3"

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
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from selenium.common.exceptions import TimeoutException


### Selenium functions

def parse_args():
    ap = argparse.ArgumentParser(description="GSA downloader")

    ap.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}"
    )

    ap.add_argument("-t", "--taxa", help="Path to taxa/species list. One line per taxa. Do not use with `--biosample` option")
    ap.add_argument("-b", "--biosample", help="Path to BioSample list. One line per BioSample ID. Allows you to get metadata for BioSamples. Do not use with `--taxa` option. Will use max of 2 threads")
    ap.add_argument("-d", "--download_dir", required=True, help="Download directory")
    ap.add_argument("--threads", type=int, default=8, help="Parallel threads. Do not use more than 8 or you will get connection refused from GSA")
    ap.add_argument("--no-headless", action="store_true", help="Run Chrome with GUI")
    ap.add_argument("--dry_run", action="store_true", help="Skip FASTQ downloads")

    return ap.parse_args()


def safe_start_driver(headless=True):
    # print(f"[DEBUG] Starting Chrome driver (headless={headless})")
    opts = Options()
    if headless:
        opts.add_argument("--headless=new")

    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")

    try:
        driver = webdriver.Chrome(service=Service(), options=opts)
        driver.set_page_load_timeout(90)
        # print("[DEBUG] Chrome driver started successfully")
        return driver
    except Exception as e:
        print(f"[WARN] Chrome failed to start: {e}")
        return None


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


def write_biosample_metadata_parallel(runinfo_csv, output_tsv, threads=4, headless=True):
    biosamples = get_biosamples_from_runinfo(runinfo_csv)
    biosamples = sorted(set(biosamples))  # extra safety

    print(f"[INFO] Scraping metadata for {len(biosamples)} BioSamples using {threads} threads...")

    records = []

    from concurrent.futures import ThreadPoolExecutor, as_completed
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.chrome.options import Options

    def _scrape_task(bs):
        print(f"[DEBUG] Thread starting scrape for {bs}")
        driver = safe_start_driver(headless=headless)
        if driver is None:
            print(f"[WARN] Driver unavailable for {bs}")
            return None
    
        try:
            rec = scrape_biosample_metadata(driver, bs)
            if rec:
                rec = {k: v for k, v in rec.items() if isinstance(v, str) and v.strip()}
                rec = {"BioSample": rec.pop("BioSample"), **rec}
                print(f"[INFO] Metadata collected for {bs}")
            else:
                print(f"[WARN] No metadata returned for {bs}")
            return rec
        except Exception as e:
            print(f"[ERROR] Failed {bs}: {e}")
            return None
        finally:
            driver.quit()
            print(f"[DEBUG] Driver closed for {bs}")

    with ThreadPoolExecutor(max_workers=threads) as executor:
        futures = [executor.submit(_scrape_task, bs) for bs in biosamples]
        for future in as_completed(futures):
            res = future.result()
            if res:
                records.append(res)

    print(f"[INFO] Finished scraping BioSamples: {len(records)} successful / {len(biosamples)} total")

    if not records:
        print("[INFO] No BioSample metadata retrieved")
        return

    # Stable column union
    all_keys = {"BioSample"}
    for r in records:
        all_keys.update(r.keys())

    all_keys = ["BioSample"] + sorted(k for k in all_keys if k != "BioSample")
    df = pd.DataFrame([{k: r.get(k, "") for k in all_keys} for r in records])

    # Explicitly drop nested garbage
    df = df.drop(columns=[c for c in df.columns if c.lower() == "attributes"], errors="ignore")

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
    # print("[DEBUG] First 5 rows:")
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
    print(f"[INFO] Accessing BioSample page: {biosample}")
    url = f"https://ngdc.cncb.ac.cn/biosample/browse/{biosample}"
    driver.get(url)

    try:
        WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.ID, "attribute_table"))
        )
        print(f"[INFO] BioSample page loaded: {biosample}")
    except TimeoutException:
        print(f"[INFO] BioSample page loaded: {biosample}")
        return None

    record = {"BioSample": biosample}

    rows = driver.find_elements(By.XPATH, "//table[@id='attribute_table']//tr")
    for row in rows:
        try:
            key = row.find_element(By.TAG_NAME, "th").text.strip()
            val = row.find_element(By.TAG_NAME, "td").text.strip()
            key = re.sub(r"\s+", "_", key)
            record[key] = val
        except Exception:
            continue

    extra_rows = driver.find_elements(
        By.XPATH,
        "//table[@class='table2 table2-border']//tr[th and td and not(ancestor::table[@id='attribute_table'])]"
    )
    # print(f"[DEBUG] Found {len(extra_rows)} extra metadata rows for {biosample}")
    for row in extra_rows:
        try:
            key = row.find_element(By.TAG_NAME, "th").text.strip()
            val = row.find_element(By.TAG_NAME, "td").text.strip()
            key = re.sub(r"\s+", "_", key)
            if key != "Accession":
                record[key] = val
        except Exception:
            continue

    print(f"[INFO] Successfully scraped BioSample {biosample} ({len(record) - 1} fields)")
    return record

def process_biosamples(biosample_txt, output_dir, threads=2, headless=True):
    """
    Scrape BioSample metadata from a list of BioSamples in a TXT file.
    Writes a combined TSV to output_dir / "biosample_metadata.tsv".
    Each thread gets its own Selenium driver to avoid overwriting issues.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    with open(biosample_txt) as f:
        biosamples = [l.strip() for l in f if l.strip()]

    print(f"[INFO] Scraping {len(biosamples)} BioSamples with {threads} threads...")

    records = []

    from concurrent.futures import ThreadPoolExecutor, as_completed
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.chrome.options import Options

    def _scrape_task(bs_id):
        # print(f"[DEBUG] Submitting BioSample scrape: {bs_id}")
        driver = safe_start_driver(headless=headless)
        if driver is None:
            return None
    
        try:
            rec = scrape_biosample_metadata(driver, bs_id)
            if rec:
                rec = {k: v for k, v in rec.items() if v.strip()}
                rec = {"BioSample": rec.pop("BioSample"), **rec}
            return rec
        except Exception as e:
            print(f"[ERROR] Failed scraping {bs_id}: {e}")
            return None
        finally:
            driver.quit()

    # Launch threads
    with ThreadPoolExecutor(max_workers=threads) as executor:
        futures = {executor.submit(_scrape_task, bs): bs for bs in biosamples}
        for future in as_completed(futures):
            res = future.result()
            if res:
                records.append(res)

    print(f"[INFO] Finished scraping BioSamples: {len(records)} successful / {len(biosamples)} total")

    if not records:
        print("[INFO] No BioSample metadata retrieved")
        return

    # Collect all keys for consistent DataFrame
    all_keys = set()
    for r in records:
        all_keys.update(r.keys())
    all_keys = ["BioSample"] + sorted(k for k in all_keys if k != "BioSample")
    print(f"[INFO] Completed BioSample scraping run")
    print(f"[INFO] Total records collected: {len(records)}")

    df = pd.DataFrame([{k: r.get(k, "") for k in all_keys} for r in records])

    # Drop Attributes column if present
    if "Attributes" in df.columns:
        df = df.drop(columns=["Attributes"])

    out_tsv = output_dir / "biosample_metadata.tsv"
    df.to_csv(out_tsv, sep="\t", index=False)
    print(f"[INFO] BioSample metadata written to {out_tsv}")


### Per-genome workflow

def process_genome(driver, genome, download_dir, threads, dry_run=False):
    print(f"[INFO] Starting genome workflow for {genome}")
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
    write_biosample_metadata_parallel(
        final_csv,
        biosample_meta_tsv,
        threads=2,   # hard cap Selenium
        headless=True
    )

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

    if not args.taxa and not args.biosample:
        print("[ERROR] Must provide at least --taxa or --biosample")
        return

    # Start a shared Chrome driver
    driver = start_chrome(dl, headless=headless)

    try:
        if args.taxa:
            with open(args.taxa) as f:
                genomes = [l.strip() for l in f if l.strip()]
            for g in genomes:
                process_genome(driver, g, dl, threads=args.threads, dry_run=args.dry_run)

        if args.biosample:
            process_biosamples(
                args.biosample,
                dl / "biosamples",
                threads=2,
                headless=headless
            )

    finally:
        driver.quit()

if __name__ == "__main__":
    main()
