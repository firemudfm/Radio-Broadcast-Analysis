#!/usr/bin/env bash
set -euo pipefail

: "${BUCKET_NAME:?Set BUCKET_NAME}"
: "${ROLE_NAME:?Set ROLE_NAME}"
AWS_REGION="${AWS_REGION:-eu-north-1}"
POLICY_NAME="FireMudRadioApiOpenS3"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

policy_file="$(mktemp)"
trap 'rm -f "$policy_file"' EXIT
cat > "$policy_file" <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ListRadioApiPrefixes",
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::${BUCKET_NAME}",
      "Condition": {
        "StringLike": {
          "s3:prefix": [
            "raw-audio/*",
            "transcripts/*",
            "results/intelligence/*",
            "results/conversation-analysis/*",
            "results/semantic-matches/*",
            "clean-speech/*",
            "config/keywords/*"
          ]
        }
      }
    },
    {
      "Sid": "ReadRadioApiObjects",
      "Effect": "Allow",
      "Action": ["s3:GetObject"],
      "Resource": [
        "arn:aws:s3:::${BUCKET_NAME}/transcripts/*",
        "arn:aws:s3:::${BUCKET_NAME}/results/intelligence/*",
        "arn:aws:s3:::${BUCKET_NAME}/results/conversation-analysis/*",
        "arn:aws:s3:::${BUCKET_NAME}/results/semantic-matches/*",
        "arn:aws:s3:::${BUCKET_NAME}/clean-speech/*",
        "arn:aws:s3:::${BUCKET_NAME}/config/keywords/*"
      ]
    },
    {
      "Sid": "WriteRadioApiObjects",
      "Effect": "Allow",
      "Action": ["s3:PutObject"],
      "Resource": [
        "arn:aws:s3:::${BUCKET_NAME}/config/keywords/*",
        "arn:aws:s3:::${BUCKET_NAME}/results/conversation-analysis/*",
        "arn:aws:s3:::${BUCKET_NAME}/results/semantic-matches/*"
      ]
    }
  ]
}
JSON

aws iam put-role-policy \
  --role-name "$ROLE_NAME" \
  --policy-name "$POLICY_NAME" \
  --policy-document "file://$policy_file"

echo "[radio-api-aws] Role updated: $ROLE_NAME"
echo "[radio-api-aws] Bucket: $BUCKET_NAME"
echo "[radio-api-aws] Account: $ACCOUNT_ID"
echo "[radio-api-aws] Region: $AWS_REGION"
