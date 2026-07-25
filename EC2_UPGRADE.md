# Upgrading the EC2 backend to v0.4.0 (radio catalogue + monitoring)

Baseline required: v0.3.1 running and healthy (`/healthz` reports `0.3.1`,
`llm: ok`). The upgrade preserves campaigns, mentions, models, S3 data, the
audio-token secret, and every running station pipeline. `hertz879` is never
restarted and is imported as a pinned legacy managed station.

## 1. Upload the package (CloudShell)

```bash
export AWS_REGION='eu-north-1'
export BUCKET_NAME='<YOUR_S3_BUCKET>'

aws s3 cp \
  ~/firemud-radio-catalog-backend-amazonlinux2023-v0.4.0.zip \
  "s3://${BUCKET_NAME}/bootstrap/firemud-radio-catalog-backend-amazonlinux2023-v0.4.0.zip" \
  --region "$AWS_REGION"
```

## 2. Download and verify on EC2

```bash
export AWS_REGION='eu-north-1'
export BUCKET_NAME='<YOUR_S3_BUCKET>'

aws s3 cp \
  "s3://${BUCKET_NAME}/bootstrap/firemud-radio-catalog-backend-amazonlinux2023-v0.4.0.zip" \
  /tmp/firemud-radio-catalog-backend-amazonlinux2023-v0.4.0.zip --region "$AWS_REGION"

cd /tmp
sha256sum firemud-radio-catalog-backend-amazonlinux2023-v0.4.0.zip
# compare with the shipped .sha256 value

rm -rf firemud-radio-catalog-backend-amazonlinux2023-v0.4.0
unzip -q -o firemud-radio-catalog-backend-amazonlinux2023-v0.4.0.zip
cd firemud-radio-catalog-backend-amazonlinux2023-v0.4.0
```

## 3. Upgrade

```bash
chmod +x deploy/*.sh
sudo ./deploy/upgrade-to-v0.4.0-amazon-linux.sh
```

The script backs up `/opt/firemud/radio-intelligence-api`, the SQLite database
(online backup API), and the env file to `/var/backups/firemud/pre-v0.4.0-*`,
installs the new source, merges new env defaults without touching existing
values, applies the idempotent migration, installs and starts
`radio-station-reconciler.service`, restarts only the API + reconciler, health
checks, and rolls back automatically when the health check fails.

## 4. Audit

```bash
./deploy/audit-v040.sh
```

Every line must print PASS, including the three `hertz879` pipeline units.

## 5. Manual rollback (later)

```bash
sudo systemctl stop radio-station-reconciler radio-intelligence-api
sudo rsync -a --delete --exclude venv \
  /var/backups/firemud/pre-v0.4.0-<stamp>/radio-intelligence-api/ \
  /opt/firemud/radio-intelligence-api/
sudo cp /var/backups/firemud/pre-v0.4.0-<stamp>/radio.db \
  /var/lib/firemud/radio-intelligence-api/radio.db
sudo cp /var/backups/firemud/pre-v0.4.0-<stamp>/radio-intelligence.env \
  /etc/firemud/radio-intelligence.env
sudo systemctl disable radio-station-reconciler
sudo systemctl start radio-intelligence-api
```

## Security reminder (unchanged no-auth pilot)

- Port 8788 must stay restricted to the tester's public IP `/32`.
- The EC2 public IP changes on stop/start; prefer an Elastic IP, a domain with
  HTTPS, or an SSM tunnel over a raw changing IP.
- The API still has no authentication; the reconciler is the only root
  component and executes only fixed, validated commands.
