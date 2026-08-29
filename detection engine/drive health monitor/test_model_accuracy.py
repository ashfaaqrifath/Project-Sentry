import argparse
import pandas as pd
import numpy as np
from sklearn.svm import OneClassSVM
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import (
    accuracy_score, precision_score, recall_score, f1_score, confusion_matrix, roc_auc_score
)

parser = argparse.ArgumentParser(description="Evaluate drive health OCSVM on real baseline.")
parser.add_argument("--training-csv", required=True, help="Real training CSV (healthy baseline)")
parser.add_argument("--test-csv", required=True, help="Test CSV with 'true_label' column")
args = parser.parse_args()

train_df = pd.read_csv(args.training_csv, comment='#')
test_df = pd.read_csv(args.test_csv)

if 'true_label' not in test_df.columns:
    raise SystemExit("Test CSV needs 'true_label' column with values 'normal' or 'anomaly'.")

feature_cols = [col for col in train_df.columns]
label_map = {"normal": 1, "anomaly": -1}

X_train = train_df[feature_cols].astype(float).values
X_test = test_df[feature_cols].astype(float).values
y_true = test_df["true_label"].map(label_map).values

print(f"Features: {feature_cols}\n")
print(f"Test data: {len(test_df)} rows ({(y_true == 1).sum()} normal, {(y_true == -1).sum()} anomaly)\n")

scaler = StandardScaler().fit(X_train)
ocsvm = OneClassSVM(kernel='rbf', gamma='scale', nu=0.05).fit(scaler.transform(X_train))

y_pred = ocsvm.predict(scaler.transform(X_test))
scores = ocsvm.decision_function(scaler.transform(X_test))

print("Confusion matrix (rows=true, cols=predicted), order=[normal, anomaly]:")
print(confusion_matrix(y_true, y_pred, labels=[1, -1]))
print()
print(f"Accuracy  : {accuracy_score(y_true, y_pred):.3f}")
print(f"Precision : {precision_score(y_true, y_pred, pos_label=-1, zero_division=0):.3f}")
print(f"Recall    : {recall_score(y_true, y_pred, pos_label=-1, zero_division=0):.3f}")
print(f"F1        : {f1_score(y_true, y_pred, pos_label=-1, zero_division=0):.3f}")
print(f"ROC-AUC   : {roc_auc_score((y_true == -1).astype(int), -scores):.3f}")