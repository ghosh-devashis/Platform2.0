# Floci setup guide and notes

Saved from a Claude Code conversation on 2026-10-05.

## 1. What is Floci?

Floci runs a **look-alike of Amazon Web Services (AWS)** on your own computer. It is not real AWS and
is not made by Amazon; it is an independent, free, open-source (MIT) project.

- Website: https://floci.io/aws/ (note: `floci.ai` does not exist; the real site is `floci.io`)
- Source code: https://github.com/floci-io/floci (latest release checked: 2.1.0)
- Address once running: `http://localhost:4566`

**Why use it:** test apps that use AWS without an AWS account, without cost, offline, and without any
risk to real data. Your code and tools (AWS CLI, AWS SDKs, Terraform) talk to it as if it were AWS.

**Services emulated** (about 68 in total), including: S3 (file storage), DynamoDB (NoSQL database),
SQS/SNS (queues and notifications), Lambda (serverless functions), API Gateway, Cognito, IAM, STS, KMS,
Secrets Manager, CloudFormation, Step Functions, EventBridge, Kinesis, RDS, ElastiCache, CloudWatch.

**Key facts**
- Data stays inside the Docker container on your PC; nothing goes to Amazon.
- No AWS account or real credentials needed; dummy values like `test` / `test` work.
- Never billed.
- Only reachable from your PC (`localhost`).
- It imitates AWS; rare features or edge cases may differ, so test against real AWS before production.
- A free alternative to LocalStack. The same team also makes `floci/floci-az` (Azure),
  `floci/floci-gcp` (Google Cloud) and `floci/floci-oci` (Oracle Cloud).

## 2. Why Docker is needed

Floci is **only published as a Docker image** (`floci/floci:latest`). Release 2.1.0 on GitHub has no
Windows `.exe` or installer.

- **Docker** runs packaged programs called *containers*. Each container holds a program plus everything
  it needs, so it runs the same on any computer. Docker is the player; the Floci image is the cartridge.
- Building Floci from source (Java 25 + Maven) is possible but harder, unsupported, and some features
  (Lambda, RDS, ElastiCache) likely still need Docker.

The chain: **Floci** runs inside **Docker**, which runs on **WSL 2**, which runs on **Windows**.

## 3. What is WSL 2?

**WSL 2 (Windows Subsystem for Linux, version 2)** is a free Microsoft feature in Windows 10/11 that runs
a real Linux system in a lightweight background virtual machine. Docker containers are Linux-based, so
Docker Desktop uses WSL 2 as its engine.

- May need a restart the first time it is turned on.
- Needs CPU **virtualization** enabled (usually on already). If Docker says it is disabled, enable
  SVM / AMD-V in the BIOS/UEFI settings.
- Uses some RAM and disk while Docker runs; freed when Docker quits.
- To set it up separately (admin terminal, then restart): `wsl --install`

## 4. AMD64 vs ARM64

These are processor families; programs must be built for the right one.

| Name | Also called | Found in |
|------|-------------|----------|
| **AMD64** | x86-64, x64 | Most Windows PCs (Intel and AMD) |
| **ARM64** | AArch64 | Phones, Apple Silicon Macs, Snapdragon Windows laptops |

**This PC:** AMD Ryzen 5 7520U → **AMD64**. Docker picks the right Floci image variant automatically.

## 5. Installation steps

### Step 1 — Download Docker Desktop (done)
- File: `Docker Desktop Installer.exe` in your **Downloads** folder
- Source: https://desktop.docker.com/win/main/amd64/Docker%20Desktop%20Installer.exe
- Size: 627,791,792 bytes (~599 MB)
- Verified: Authenticode signature **valid**, signed by **Docker Inc**

### Step 2 — Install Docker Desktop (you do this)
1. Open **Downloads**, double-click **Docker Desktop Installer.exe**.
2. Click **Yes** on the Windows admin prompt.
3. Keep **"Use WSL 2 instead of Hyper-V"** checked, click **OK**.
4. When done, click **Close and restart** (or restart if asked).
5. After restart, open **Docker Desktop** from Start. Accept the terms; signing in is optional.
6. Wait for **Engine running** in the bottom-left corner.

### Step 3 — Start Floci (Claude does this after you say Docker is running)
From this project folder:

```bash
docker pull floci/floci:latest
```

```bash
docker compose up -d
```

[compose.yaml](../compose.yaml) runs `floci/floci:latest` on port 4566 and restarts it automatically.

### Step 4 — Test it
**Done on 2026-10-05.** Docker Desktop 29.8.1, Floci 2.1.0 (image 323 MB), container `floci` healthy.
Tests passed: S3 bucket `my-test-bucket` with `hello.txt` (read back "Hello from Floci"), DynamoDB table `TestTable` (ACTIVE).

The AWS CLI v2 is already installed. **Don't run plain `aws configure`**: it would overwrite your default
profile, which may hold real AWS keys. Instead, create a separate profile named `floci` (one-time):

```bash
aws configure set aws_access_key_id test --profile floci
```

```bash
aws configure set aws_secret_access_key test --profile floci
```

```bash
aws configure set region us-east-1 --profile floci
```

```bash
aws configure set endpoint_url http://localhost:4566 --profile floci
```

Then every command just adds `--profile floci` and goes to Floci, never to real AWS:

```bash
aws --profile floci s3 ls
```

```bash
aws --profile floci dynamodb list-tables
```

Health check in a browser: http://localhost:4566/_floci/health

### Useful commands
| Task | Command |
|------|---------|
| See if Floci is running | `docker ps` |
| View Floci logs | `docker logs floci` |
| Stop Floci | `docker compose down` |
| Update Floci | `docker compose pull` then `docker compose up -d` |

### Web console (Floci UI)
[compose.yaml](../compose.yaml) also runs **Floci UI** (`floci/floci-ui:latest`, about 42 MB). Open
**http://localhost:4500** → **Open Storage** → click a bucket to see, upload or delete files. It also has
pages for other services. (Floci's own link `http://localhost:4566/_floci/ui` shows "unavailable" in this
setup; that's expected — use port 4500.)

## 6. Day-to-day

- Floci restarts automatically whenever Docker Desktop is running (`restart: unless-stopped`).
- Docker Desktop must be open for Floci to work; after a PC restart, start Docker Desktop first.
- Data lives inside the container; `docker compose down` removes the container and its data.

## Sources
- https://floci.io/aws/
- https://floci.io/llms.txt
- https://github.com/floci-io/floci
- https://themenonlab.blog/blog/floci-free-aws-local-emulator
- https://dev.co/devops/open-source/floci
