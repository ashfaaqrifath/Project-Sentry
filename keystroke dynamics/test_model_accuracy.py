import argparse
import joblib
import pandas as pd
from sklearn.metrics import (
    accuracy_score, precision_score, recall_score, f1_score, confusion_matrix, roc_auc_score
)

parser = argparse.ArgumentParser(description="Evaluate ML model.")
parser.add_argument("--model", required=True, help="path to the .pkl model file")
parser.add_argument("--scaler", default=None, help="path to the .pkl scaler file")
parser.add_argument("--test-csv", required=True, help="Labeled CSV with true_label column")
args = parser.parse_args()

df = pd.read_csv(args.test_csv)
if "true_label" not in df.columns:
    raise SystemExit("no true_label column")

label_map = {"normal": 1, "anomaly": -1}
y_true = df["true_label"].map(label_map).values

feature_cols = [col for col in df.columns if col != "true_label"]
X = df[feature_cols].astype(float).values

model = joblib.load(args.model)
if args.scaler:
    X = joblib.load(args.scaler).transform(X)

y_pred = model.predict(X)
scores = model.decision_function(X) if hasattr(model, "decision_function") else None

print("Confusion matrix (rows=true, cols=predicted), order=[normal, anomaly]:")
print(confusion_matrix(y_true, y_pred, labels=[1, -1]))
print()
print(f"Accuracy  : {accuracy_score(y_true, y_pred):.3f}")
print(f"Precision : {precision_score(y_true, y_pred, pos_label=-1, zero_division=0):.3f}")
print(f"Recall    : {recall_score(y_true, y_pred, pos_label=-1, zero_division=0):.3f}")
print(f"F1        : {f1_score(y_true, y_pred, pos_label=-1, zero_division=0):.3f}")
if scores is not None:
    print(f"ROC-AUC   : {roc_auc_score((y_true == -1).astype(int), -scores):.3f}")