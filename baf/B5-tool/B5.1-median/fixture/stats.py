import csv
import sys
from statistics import median

with open(sys.argv[1], encoding="utf-8", newline="") as source:
    print(median(int(row["value"]) for row in csv.DictReader(source)))
