import numpy as np
import pandas as pd
from scipy.stats import pearsonr
import json

# BUG 1 (PATH_HARDCODED): absolute path from the author's own machine
DATA_PATH = "/home/researcher/project/data/measurements.csv"

def main():
    df = pd.read_csv(DATA_PATH)
    # BUG 2 (DEP_API_CHANGE): np.float removed in numpy >= 1.24
    x = df["x"].astype(np.float)
    y = df["y"].astype(np.float)
    group = df["group"].values

    r_xy, p = pearsonr(x, y)
    treatment_effect = y[group == 1].mean() - y[group == 0].mean()

    # BUG 3 (SEED_MISSING): bootstrap with no seed -> non-deterministic CI
    boot = []
    n = len(x)
    for _ in range(2000):
        idx = np.random.randint(0, n, n)
        boot.append(pearsonr(x.values[idx], y.values[idx])[0])
    ci = [float(np.percentile(boot, 2.5)), float(np.percentile(boot, 97.5))]

    results = {"r_xy": float(r_xy),
               "treatment_effect": float(treatment_effect),
               "r_ci95": ci}
    with open("results.json", "w") as f:
        json.dump(results, f, indent=2)
    print(json.dumps(results, indent=2))

if __name__ == "__main__":
    main()
