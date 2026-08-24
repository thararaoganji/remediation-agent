# Deploying to Google Cloud

Two independent pieces, in order:

1. **SonarQube** — a long-running server, on a Compute Engine VM (Docker Compose).
2. **The agent** — a run-to-completion job, as a **Cloud Run Job** (not a Cloud
   Run *Service* — the agent isn't a request/response server; it fetches,
   fixes, verifies, and exits, which is exactly what a Job is for).

Everything below uses placeholders in `ALL_CAPS` — replace them with your own
values. Commands are `gcloud`/`docker`, run from your own machine (both are
already installed and on `PATH` here).

---

## 0. Prerequisites

```bash
gcloud auth login
gcloud config set project PROJECT_ID
```

You'll also need, from your existing `.env` (never commit or paste these
anywhere public):
- `GOOGLE_API_KEY` — Gemini access
- A GitHub token with `Contents: Read & write` on the target repo(s)

---

## 1. SonarQube on a Compute Engine VM

### 1.1 Create the VM

```bash
gcloud services enable compute.googleapis.com

gcloud compute instances create sonarqube-vm \
  --zone=ZONE \
  --machine-type=e2-standard-2 \
  --image-family=ubuntu-2204-lts \
  --image-project=ubuntu-os-cloud \
  --boot-disk-size=30GB \
  --tags=sonarqube
```

`e2-standard-2` (2 vCPU / 8 GB) is SonarQube's realistic minimum — it runs an
embedded Elasticsearch alongside the web/compute-engine processes and is
noticeably unhappy on anything smaller. Check the
[GCP pricing calculator](https://cloud.google.com/products/calculator) for a
current cost estimate for your region rather than trusting a number here —
pricing changes.

### 1.2 Firewall — allow port 9000

```bash
gcloud compute firewall-rules create allow-sonarqube \
  --allow=tcp:9000 \
  --target-tags=sonarqube \
  --source-ranges=YOUR_IP/32
```

Start with `--source-ranges` scoped to your own IP (`curl ifconfig.me` to find
it), not `0.0.0.0/0` — widen it later only if you specifically need the
Cloud Run Job to reach this VM over the public internet (see §3).

### 1.3 Stopping/starting the VM to save cost

An always-on `e2-standard-2` is the dominant cost of this whole setup (~$50/mo
for compute alone). If usage is bursty rather than continuous, stop the VM
between sessions — cost scales roughly with uptime. Two scripts are provided:

```bash
export SONARQUBE_VM_ZONE=ZONE   # once, so you don't need to repeat it below

deploy/gcp/stop-sonarqube-vm.sh
deploy/gcp/start-sonarqube-vm.sh
```

Docker's `restart: unless-stopped` policy on both compose services means
SonarQube and Postgres come back up on their own after a start — no manual
`docker compose up` needed — as long as the Docker daemon itself is enabled
to start on boot (`sudo systemctl enable docker`, §1.4).

One gotcha: a VM's default **ephemeral** external IP changes on every
stop/start cycle. `start-sonarqube-vm.sh` prints the new IP and flags this,
but if `SONAR_BASE_URL` (agent `.env` / Cloud Run job env var) is pointing at
the old one, update it — or reserve a static IP once so the address never
moves:

```bash
gcloud compute addresses create sonarqube-vm-ip --region=REGION
gcloud compute instances add-access-config sonarqube-vm --zone=ZONE \
  --access-config-name="External NAT" --address=RESERVED_IP
```

### 1.4 SSH in and prep the host

```bash
gcloud compute ssh sonarqube-vm --zone=ZONE
```

On the VM:

```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-plugin
sudo systemctl enable docker
sudo usermod -aG docker "$USER"
# log out and back in (or `newgrp docker`) for the group change to apply

# SonarQube's embedded Elasticsearch refuses to start below these --
# the single most common reason a fresh install crash-loops.
echo "vm.max_map_count=262144" | sudo tee -a /etc/sysctl.conf
echo "fs.file-max=131072" | sudo tee -a /etc/sysctl.conf
sudo sysctl -p
```

### 1.5 Bring SonarQube up

From your own machine, copy the compose file up:

```bash
gcloud compute scp deploy/gcp/docker-compose.sonarqube.yml sonarqube-vm:~/docker-compose.yml --zone=ZONE
```

Back on the VM, create a **separate** `.env` next to it (this is the VM's own
file, unrelated to the agent's `.env`) with a real password:

```bash
echo "SONAR_DB_PASSWORD=$(openssl rand -base64 24)" > .env
docker compose up -d
docker compose logs -f sonarqube   # wait for "SonarQube is operational"
```

### 1.6 First login

Visit `http://EXTERNAL_IP:9000` (get the IP via
`gcloud compute instances describe sonarqube-vm --zone=ZONE --format='get(networkInterfaces[0].accessConfigs[0].natIP)'`).
Log in with `admin` / `admin` — you'll be forced to set a real password
immediately.

Then **My Account → Security → Generate Token** — this is the value for
`SONAR_TOKEN` later.

**Important, easy to miss**: `SetupStep`'s preflight check requires the
target project to already have *at least one* prior analysis under its
resolved project key before the agent will touch it — a key that resolves
cleanly from `pom.xml`/`build.gradle` but has never actually been scanned
fails with a clear error rather than silently finding 0 issues. Run one
manual scan per target repo first:

```bash
./mvnw sonar:sonar -Dsonar.projectKey=YOUR_KEY -Dsonar.host.url=http://EXTERNAL_IP:9000 -Dsonar.token=YOUR_TOKEN
# or
./gradlew sonar -Dsonar.projectKey=YOUR_KEY -Dsonar.host.url=http://EXTERNAL_IP:9000 -Dsonar.token=YOUR_TOKEN
```

`CE_EDITION=true` remains the right setting for the agent's `.env` regardless
of which SonarQube build you're running — `_scanned_branch()` (in
`agents/maintainability.py`) probes the server directly for whether a branch
actually exists rather than assuming based on this flag, so getting it
slightly "wrong" no longer breaks a run the way it used to.

### 1.7 Branch analysis on Community Build

`docker-compose.sonarqube.yml` uses
`mc1arke/sonarqube-with-community-branch-plugin:latest` rather than the
stock `sonarqube:lts-community` image — that plugin
([source](https://github.com/mc1arke/sonarqube-community-branch-plugin))
patches branch analysis (normally a Commercial-edition-only feature) onto the
free Community Build. It's a straight drop-in: the image already sets the
required `SONAR_WEB_JAVAADDITIONALOPTS`/`SONAR_CE_JAVAADDITIONALOPTS`
`javaagent` flags as its own defaults, so nothing else needs configuring —
*unless* you later add your own overrides for those two env vars for some
other reason, in which case you must re-include the `-javaagent:...=web` /
`...=ce` line yourself or the plugin silently stops loading.

With this in place, each agent run's per-branch analysis actually shows up as
its own branch in the SonarQube UI's branch dropdown, instead of always
folding back into `main`.

`:latest` tracks whatever SonarQube version the plugin maintainer currently
supports; pin a specific tag (e.g. `26.5.0.122743-community` — check
[Docker Hub](https://hub.docker.com/r/mc1arke/sonarqube-with-community-branch-plugin/tags)
for what's current) once you want a reproducible, won't-change-under-you
deployment rather than a moving target.

---

## 2. Containerize and deploy the agent

### 2.1 Build and push the image

```bash
gcloud services enable artifactregistry.googleapis.com run.googleapis.com secretmanager.googleapis.com

gcloud artifacts repositories create sonar-autofix-repo \
  --repository-format=docker \
  --location=REGION

gcloud auth configure-docker REGION-docker.pkg.dev

docker build -t REGION-docker.pkg.dev/PROJECT_ID/sonar-autofix-repo/sonar-autofix-agent:latest .
docker push REGION-docker.pkg.dev/PROJECT_ID/sonar-autofix-repo/sonar-autofix-agent:latest
```

The `Dockerfile` at the repo root installs Java, Maven, *and* Gradle
alongside Python — the agent's adapters shell out to whichever of those the
checked-out **target** project actually uses, so both need to be present
regardless of which one any single run happens to need.

### 2.2 Store secrets

```bash
printf '%s' 'YOUR_GOOGLE_API_KEY' | gcloud secrets create google-api-key --data-file=-
printf '%s' 'YOUR_SONAR_TOKEN'    | gcloud secrets create sonar-token    --data-file=-
printf '%s' 'YOUR_GITHUB_TOKEN'  | gcloud secrets create github-token  --data-file=-
```

### 2.3 Create the Cloud Run Job

```bash
gcloud run jobs create sonar-autofix-job \
  --image=REGION-docker.pkg.dev/PROJECT_ID/sonar-autofix-repo/sonar-autofix-agent:latest \
  --region=REGION \
  --set-env-vars=SONAR_BASE_URL=http://SONARQUBE_VM_IP:9000,SOURCE_TYPE=github,GITHUB_REPO=OWNER/REPO,LANGUAGE=java,CE_EDITION=true \
  --set-secrets=GOOGLE_API_KEY=google-api-key:latest,SONAR_TOKEN=sonar-token:latest,GITHUB_TOKEN=github-token:latest \
  --max-retries=0 \
  --task-timeout=3600 \
  --memory=2Gi \
  --cpu=2
```

`--max-retries=0` is deliberate: a failed run has already committed whatever
it got done to its own branch (see the report/limitations discussion in the
briefing deck) — an automatic retry would start a *second*, independent fresh
branch on top, not resume the first. Re-run by hand once you've looked at
why it failed.

### 2.4 Run it

```bash
gcloud run jobs execute sonar-autofix-job --region=REGION
```

Watch it live:

```bash
gcloud run jobs executions list --job=sonar-autofix-job --region=REGION
gcloud run jobs executions logs read EXECUTION_NAME --region=REGION
```

or via **Cloud Console → Cloud Run → Jobs → sonar-autofix-job → Logs**.

---

## 3. Networking: Cloud Run reaching the SonarQube VM

Cloud Run Jobs, by default, egress through Google's shared internet IPs — not
a fixed address you can allowlist on the VM's firewall. Two options:

- **Simple (pilot-only)**: open the VM's firewall rule to `0.0.0.0/0` on port
  9000. Fast to set up; the tradeoff is SonarQube's login page becomes
  reachable by anyone on the internet (still gated by the admin password and
  `SONAR_TOKEN` for API access, but a weak admin password is now a real
  exposure, not a theoretical one).
- **Recommended**: create a
  [Serverless VPC Access connector](https://cloud.google.com/run/docs/configuring/vpc-connectors)
  in the same VPC as the SonarQube VM, attach it to the Cloud Run Job
  (`--vpc-connector`, `--vpc-egress=all-traffic`), and point `SONAR_BASE_URL`
  at the VM's **internal** IP instead. The firewall rule then only needs to
  allow the VPC's own internal range, not the public internet.

Start with the simple path to get the first end-to-end run working, then
move to the VPC connector before this touches anything you'd call
production.

---

## 4. Continuous deployment via GitHub Actions

`.github/workflows/deploy-gcp.yml` builds this repo's own agent image on
every push to `main` (that touches agent code) and rolls it out to the
Cloud Run Job created in §2.3. It authenticates via **Workload Identity
Federation** — GitHub's own OIDC token is exchanged for short-lived GCP
credentials, so there's no long-lived service-account key sitting in GitHub
secrets to leak or rotate.

### 4.1 One-time GCP-side setup

```bash
PROJECT_ID="PROJECT_ID"
REPO="OWNER/REPO"          # this GitHub repo, e.g. acme/remediation-agent
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"

# Pool + OIDC provider, scoped to only this repo via attribute-condition
gcloud iam workload-identity-pools create "github-pool" \
  --project="$PROJECT_ID" --location="global" \
  --display-name="GitHub Actions"

gcloud iam workload-identity-pools providers create-oidc "github-provider" \
  --project="$PROJECT_ID" --location="global" \
  --workload-identity-pool="github-pool" \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
  --attribute-condition="assertion.repository=='${REPO}'"

# Dedicated deploy service account -- least privilege: build/push + update
# the one Cloud Run Job, nothing else.
gcloud iam service-accounts create github-deployer \
  --project="$PROJECT_ID" --display-name="GitHub Actions deployer"

SA="github-deployer@${PROJECT_ID}.iam.gserviceaccount.com"

for role in roles/run.developer roles/artifactregistry.writer roles/iam.serviceAccountUser; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${SA}" --role="$role"
done

# Let only this specific repo impersonate that service account
gcloud iam service-accounts add-iam-policy-binding "$SA" \
  --project="$PROJECT_ID" \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github-pool/attribute.repository/${REPO}"
```

### 4.2 GitHub-side setup

In the repo's **Settings → Secrets and variables → Actions → Variables**
tab (these are **Variables**, not Secrets — a WIF provider path and a
service-account email aren't sensitive on their own):

| Variable | Value |
|---|---|
| `GCP_PROJECT_ID` | `PROJECT_ID` |
| `GCP_REGION` | the region you used in §2 |
| `GCP_WIF_PROVIDER` | `projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/github-pool/providers/github-provider` |
| `GCP_DEPLOY_SERVICE_ACCOUNT` | `github-deployer@PROJECT_ID.iam.gserviceaccount.com` |

Push to `main` (or run the workflow manually via **Actions → Deploy agent
to Cloud Run → Run workflow**) and it builds, pushes, and updates the job in
place. The job's env vars/secrets (`SONAR_BASE_URL`, `GITHUB_REPO`, the
Secret Manager bindings, etc.) are untouched by this — `gcloud run jobs
update --image=...` only swaps the image, so §2.3's one-time `create` is
still what defines everything else about the job.

Executing the job (actually running the agent against a target repo) stays
a separate, deliberate action — either `gcloud run jobs execute` by hand, or
the optional Cloud Scheduler wiring in §5 — this workflow only keeps the
job's *image* current.

---

## 5. Optional: scheduled runs

```bash
gcloud iam service-accounts create sonar-autofix-invoker

gcloud run jobs add-iam-policy-binding sonar-autofix-job \
  --region=REGION \
  --member="serviceAccount:sonar-autofix-invoker@PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/run.invoker"

gcloud scheduler jobs create http sonar-autofix-nightly \
  --schedule="0 2 * * *" \
  --uri="https://REGION-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/PROJECT_ID/jobs/sonar-autofix-job:run" \
  --http-method=POST \
  --oauth-service-account-email="sonar-autofix-invoker@PROJECT_ID.iam.gserviceaccount.com"
```

This is genuinely optional for a first pass — get one manual
`gcloud run jobs execute` working end to end before automating *when* it
runs.

---

## Recap of what's real vs. still manual

- **Automated by this setup**: build/push the image, the run itself, secret
  injection, logs.
- **Still manual, by design**: re-running after a failure (no auto-retry —
  see §2.3), rotating the SonarQube admin password, and the one-time initial
  scan per target project (§1.6) before the agent can touch it the first
  time.
