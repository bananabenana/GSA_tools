# GSA_tools
GSA_tools is simple script which performs multi-threaded genome downloads from the Genome Sequence Archive (GSA). It not only downloads the reads, but will also download the associated BioSample metadata, along with a read manifest which is useful for downstream applications.


## Motivation
I could not find the API (and I imagine someone will hopefully point this out to me soon) for GSA, so I decided to write this little scraper type tool to download reads for specific species and genera. 

The Genome Sequence Archive ([GSA](https://ngdc.cncb.ac.cn/gsa/search/)) is an incredible and massively underutilised genomic data resource which is compatible with but not part of the INSDC international collaboration. The INSDC  includes NCBI [Genbank](https://www.ncbi.nlm.nih.gov/genbank/), the European Nucleotide Archive ([ENA](https://www.ebi.ac.uk/ena/browser/)), and the DNA DataBank of Japan ([DDBJ](https://www.ddbj.nig.ac.jp/index-e.html)).

My hope is that GSA_tools will allow researchers and public health folk to make use of this underutilised resource.


## Installation
```bash
# Clone repository
git clone https://github.com/bananabenana/GSA_tools

# Move to directory
cd GSA_tools

# Use mamba (or optionally conda to install the required packages)
mamba env create -f environment.yaml

# Test installation
python GSA_tools.py -h
```

## Quick start
Download all readsets from the Proteus genus of bacteria.
```bash
python GSA_tools.py \
  --input test_data/input/species_list.txt \
  --download_dir reads_output \
  --threads 8 # Do not exceed 8 concurrent threads, as GSA will start blocking your download attempts
```

## Inputs
All you need is a list of species or genera (one per line) in a .txt file. See example species_list.txt(test_data/input/species_list.txt) This will be used to search GSA and download reads from those taxa.

## Outputs
GSA_tools will output the following directory structure:
```
reads_output/
├── Genera1/
│   ├── Genera1_biosample_metadata.tsv
│   ├── Genera1_read_manifest.tsv
│   ├── Genera1_RunInfo.csv
│   ├── BioSample_1/
│   │   ├── read_f1.fq.gz
│   │   └── read_r2.fq.gz
│   └── BioSample_2/
│       ├── read_f1.fq.gz
│       └── read_r2.fq.gz
├── Species1/
│   ├── Species1_biosample_metadata.tsv
│   ├── Species1_read_manifest.tsv
│   ├── Species1_RunInfo.csv
│   └── BioSample_3/
│       └── read_ONT.fq.gz
└── Species2/
    ├── Species2_biosample_metadata.tsv
    ├── Species2_read_manifest.tsv
    ├── Species2_RunInfo.csv
    └── BioSample_4/
        ├── read_f1.fq.gz
        ├── read_r2.fq.gz
        └── read_ONT.fq.gz
```

Specifically, aside from the reads, there are 3 key output tables:
- [taxa_biosample_metadata.tsv](#taxa_biosample_metadata)
- [taxa_read_manifest.tsv](#taxa_read_manifest)
- [taxa_RunInfo.csv](#taxa_runinfo)


### taxa_biosample_metadata
Contains the BioSample metadata scraped from GSA. This is a subset of the columns shown for brevity. Very useful for analysis
| BioSample   | BioProject_Accession | Collected_by | Collection_date | Description                        | Geographic_location | Host         | Host_disease | Host_sex       | Isolation_source | Latitude_and_longitude | Organism          |
| ----------- | -------------------- | ------------ | --------------- | ---------------------------------- | ------------------- | ------------ | ------------ | -------------- | ---------------- | ---------------------- | ----------------- |
| SAMC4472544 | PRJCA030348          | China        | 3/10/2020       | Tianjin Proteus mirabilis isolates | China: Tianjin      | Homo sapiens | infection    | not applicable | biospecimen      | 39.08 N 117.20 E       | Proteus mirabilis |
| SAMC4472829 | PRJCA030348          | China        | 3/10/2020       | Tianjin Proteus mirabilis isolates | China: Tianjin      | Homo sapiens | infection    | not applicable | biospecimen      | 39.08 N 117.20 E       | Proteus mirabilis |
| SAMC6212547 | PRJCA030348          | China        | 3/10/2020       | Tianjin Proteus mirabilis isolates | China: Tianjin      | Homo sapiens | infection    | not applicable | biospecimen      | 39.08 N 117.20 E       | Proteus mirabilis |


### taxa_read_manifest
Contains read path locations, as well as whether they are (`short_only`|`long_only`|`hybrid`). Useful for triaging genomes for assembly (i.e. short_only for Unicycler, long_only and hybrid for Hybracter/Autocycler)

| biosample_path                  | fastq_count | status     | short_read_1                                      | short_read_2                                      | long_read_primary                                  | long_read_extra |
| ------------------------------- | ----------- | ---------- | ------------------------------------------------- | ------------------------------------------------- | -------------------------------------------------- | --------------- |
| reads/Salmonella/SAMC3057931    | 2           | short_only | reads/Salmonella/SAMC3057931/CRR875244_f1.fq.gz   | reads/Salmonella/SAMC3057931/CRR875244_r2.fq.gz   |
| reads/Salmonella/SAMC3057932    | 2           | short_only | reads/Salmonella/SAMC3057932/CRR875245_f1.fq.gz   | reads/Salmonella/SAMC3057932/CRR875245_r2.fq.gz   |
| reads/Acinetobacter/SAMC1032809 | 1           | long_only  |                                                   |                                                   | reads/Acinetobacter/SAMC1032809/CRR633853.fastq.gz |
| reads/Acinetobacter/SAMC1032810 | 1           | long_only  |                                                   |                                                   | reads/Acinetobacter/SAMC1032810/CRR633854.fastq.gz |
| reads/Acinetobacter/SAMC797124  | 3           | hybrid     | reads/Acinetobacter/SAMC797124/CRR513476_f1.fq.gz | reads/Acinetobacter/SAMC797124/CRR513476_r2.fq.gz | reads/Acinetobacter/SAMC797124/CRR513640.fq.gz     |
| reads/Acinetobacter/SAMC797125  | 3           | hybrid     | reads/Acinetobacter/SAMC797125/CRR513477_f1.fq.gz | reads/Acinetobacter/SAMC797125/CRR513477_r2.fq.gz | reads/Acinetobacter/SAMC797125/CRR513641.fq.gz     |


### taxa_RunInfo
The original downloaded RunInfo from GSA (clipped for parsing). Not that useful but what GSA_tools uses to obtain a list of taxa-specific BioSample IDs and Download paths

| Run        | Center | ReleaseDate | FileType | FileName                                 | FileSize             | Download_path                                                                                                                                  | Experiment | Title  | LibraryName | LibraryStrategy | LibrarySelection | LibrarySource | LibraryLayout | InsertSize | InsertDev | Platform | BioProject  | BioSample   | SampleType                           | TaxID | ScientificName    |
| ---------- | ------ | ----------- | -------- | ---------------------------------------- | -------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- | ---------- | ------ | ----------- | --------------- | ---------------- | ------------- | ------------- | ---------- | --------- | -------- | ----------- | ----------- | ------------------------------------ | ----- | ----------------- |
| CRR2420253 | NGDC   | 8/12/2025    | fastq    | CRR2420253_r1.fq.gz\|CRR2420253_r2.fq.gz | 203658530\|213383300 | ftp://download.big.ac.cn/gsa5/CRA034827/CRR2420253/CRR2420253_r1.fq.gz\|ftp://download.big.ac.cn/gsa5/CRA034827/CRR2420253/CRR2420253_r2.fq.gz | CRX2257325 | pm1151 |             | Genome          | RANDOM PCR       | GENOMIC       | Paired        |            |           | ILLUMINA | PRJCA030348 | SAMC6212548 | Clinical or host-associated pathogen | 584   | Proteus mirabilis |
| CRR2420256 | NGDC   | 8/12/2025    | fastq    | CRR2420256_r1.fq.gz\|CRR2420256_r2.fq.gz | 230507410\|245128551 | ftp://download.big.ac.cn/gsa5/CRA034827/CRR2420256/CRR2420256_r1.fq.gz\|ftp://download.big.ac.cn/gsa5/CRA034827/CRR2420256/CRR2420256_r2.fq.gz | CRX2257328 | pm117  |             | Genome          | RANDOM PCR       | GENOMIC       | Paired        |            |           | ILLUMINA | PRJCA030348 | SAMC6212551 | Clinical or host-associated pathogen | 584   | Proteus mirabilis |
| CRR2420257 | NGDC   | 8/12/2025    | fastq    | CRR2420257_r1.fq.gz\|CRR2420257_r2.fq.gz | 244096749\|256854538 | ftp://download.big.ac.cn/gsa5/CRA034827/CRR2420257/CRR2420257_r1.fq.gz\|ftp://download.big.ac.cn/gsa5/CRA034827/CRR2420257/CRR2420257_r2.fq.gz | CRX2257329 | pm1537 |             | Genome          | RANDOM PCR       | GENOMIC       | Paired        |            |           | ILLUMINA | PRJCA030348 | SAMC6212552 | Clinical or host-associated pathogen | 584   | Proteus mirabilis |


## Other options
- `--dry_run`: allows you to obtain the taxa_biosample_metadata.tsv and taxa_RunInfo.csv prior to downloading reads. Useful to see what you're getting yourself into IF you were to download the reads. Remember to delete these files before re-attempting without `--dry_run`
- `--no-headless`: Runs Chrome headed. Not really useful for a command line situation.


## Requirements
For specific packages, see [environment.yaml](environment.yml)
- Chrome v124.0
- Python 3.14.2
- conda/mamba


## Author
- Ben Vezina


## Citation
To come. For now, please cite the github repo (https://github.com/bananabenana/GSA_tools)


## Notices
GSA_tools currently works as of `2025-12-23`. Note that updates to the GSA website will likely break this script.
