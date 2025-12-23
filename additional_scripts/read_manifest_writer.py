#!/usr/bin/env python3
"""
Writes a manifest file which describes download GSA readsets.
Used for downstream processing of reads and whether BioSamples contain "short read", "long read" or "hybrid" (both short and long read) files.
Already implemented into GSA_tools.py, but standalone version is also useful.

Usage:
    python scripts/biosample_assembly_manifest_writer.py \
        --input reads \
        --output all_species_read_manifest.tsv \
        --depth 2

Requirements:
    - conda packages:
        - mamba create -y -n gsa_tools
        - mamba install -y -n gsa_tools conda-forge::pandas
        - mamba install -y -n gsa_tools conda-forge::selenium
        - mamba install -y -n gsa_tools conda-forge::webdriver-manager
        - mamba install -y -n gsa_tools conda-forge::python-chromedriver-binary
    - Chrome v124.0
"""

from pathlib import Path
import argparse
import sys
import re


# Catch-all but safe paired-end regexes
R1_RE = re.compile(
    r"(?:^|[_\.-])(?:[RrFf]?1)(?=[_\.-]?\.(?:f(ast)?q)\.gz$)",
    re.IGNORECASE
)

R2_RE = re.compile(
    r"(?:^|[_\.-])(?:[RrFf]?2)(?=[_\.-]?\.(?:f(ast)?q)\.gz$)",
    re.IGNORECASE
)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Build read manifest for genome assembly routing"
    )
    parser.add_argument(
        "--input",
        required=True,
        help="Input root directory (e.g. reads)"
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Output TSV path"
    )
    parser.add_argument(
        "--depth",
        type=int,
        default=2,
        help="Directory depth below input that corresponds to biosample (default: 2)"
    )
    return parser.parse_args()


def main():
    args = parse_args()

    input_dir = Path(args.input).resolve()
    output_tsv = Path(args.output).resolve()
    depth = args.depth

    if not input_dir.is_dir():
        sys.exit(f"ERROR: input directory does not exist: {input_dir}")

    output_tsv.parent.mkdir(parents=True, exist_ok=True)

    glob_pattern = "/".join(["*"] * depth)

    biosample_dirs = [
        p for p in input_dir.glob(glob_pattern)
        if p.is_dir()
    ]

    if not biosample_dirs:
        sys.exit(
            f"ERROR: No biosample directories found at depth {depth} under {input_dir}"
        )

    with output_tsv.open("w") as fh:
        fh.write(
            "biosample_path\tfastq_count\tstatus\tshort_read_1\tshort_read_2\t"
            "long_read_primary\tlong_read_extra\n"
        )

        for biosample in sorted(biosample_dirs):

            files = list(biosample.glob("*.f*q.gz"))
            fastq_count = len(files)

            short1 = []
            short2 = []
            long_reads = []

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


if __name__ == "__main__":
    main()
